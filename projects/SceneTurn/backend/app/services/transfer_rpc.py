import asyncio
import logging
import subprocess
import zipfile
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.models import new_id
from app.schemas import CanvasSnapshot, ProjectSnapshot, WireModel
from app.services.cloudbase_database import call_cloudbase_rpc, gateway_enabled
from app.services.cloudbase_imports import TransferRecord
from app.services.deployment_queue import DeploymentQueueError, DispatchMarker, epoch_milliseconds
from app.services.project_transfers import build_archive, export_asset_record, game_snapshot_from_records
from app.services.projects import payload_digest
from app.services.transfer_archives import without_credentials
from app.services.transfer_dispatch import transfer_queue
from app.transfer_schemas import BACKUP_SOURCE_FORMAT, ExportCreate


logger = logging.getLogger("sceneturn")
_ACCEPT_WINDOW_MS = 45_000
_RECOVERY_DUE_MS = 65_000


class ClaimedTransfer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    kind: str
    project_id: str | None
    owner_id: str
    identity_sha256: str
    private_json: dict
    request_json: dict
    source_revision: int | None
    lease_owner: str


class ExportAsset(BaseModel):
    id: str
    relpath: str
    poster_relpath: str | None
    name: str
    mime_type: str
    bytes: int
    sha256: str
    duration_ms: int | None
    width: int | None
    height: int | None


class ExportSource(WireModel):
    project: ProjectSnapshot
    canvases: list[CanvasSnapshot]
    assets: list[ExportAsset]
    history: dict
    conflicts: list[dict]


class ExistingExport(WireModel):
    existing: TransferRecord


class ExportAcceptance(BaseModel):
    job: TransferRecord
    replayed: bool
    obsolete: dict | None = None


def export_artifact(settings: Settings, owner_id: str, identifier: str) -> TransferRecord:
    try:
        valid_id = str(UUID(identifier)) == identifier
    except ValueError:
        valid_id = False
    if not valid_id:
        raise AppError(404, "TRANSFER_NOT_FOUND", "导入或导出记录不存在或无权访问")
    value = call_cloudbase_rpc(settings, "sceneturn_transfer_artifact", {"p_owner_id": owner_id, "p_job_id": identifier})
    try:
        return TransferRecord.model_validate(value, strict=True)
    except ValidationError:
        raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "导出文件记录暂时无法读取") from None


class TransferRpc:
    def __init__(self, settings: Settings):
        if not gateway_enabled(settings):
            raise AppError(503, "DATABASE_GATEWAY_UNAVAILABLE", "数据库网关尚未配置")
        self.settings = settings
        self.queue = transfer_queue(settings)
        if self.queue is None:
            raise AppError(503, "TRANSFER_QUEUE_UNAVAILABLE", "传输任务调度尚未配置")

    def _call(self, function: str, **parameters):
        return call_cloudbase_rpc(self.settings, f"sceneturn_transfer_{function}", parameters)

    def _publish(self, job_id: str, delay_ms: int) -> DispatchMarker:
        marker = DispatchMarker(epoch_milliseconds() + delay_ms, "transfer", UUID(job_id), uuid4())
        try:
            self.queue.publish(marker)
        except DeploymentQueueError:
            raise AppError(503, "TRANSFER_QUEUE_UNAVAILABLE", "传输任务暂未安排，请重试") from None
        return marker

    def publish_marker(self, job_id: str) -> DispatchMarker:
        return self._publish(job_id, _ACCEPT_WINDOW_MS)

    def _remove_obsolete(self, marker: DispatchMarker) -> None:
        try:
            self.queue.remove(marker)
        except DeploymentQueueError:
            logger.warning("传输任务旧调度记录将由下一次定时检查清理")

    def _remove_superseded(self, result: dict) -> None:
        if replaced := result.get("obsolete"):
            self._remove_obsolete(DispatchMarker(replaced["dueMs"], "transfer", UUID(replaced["jobId"]), UUID(replaced["revision"])))

    def create_export(self, owner_id: str, project_id: str, request: ExportCreate) -> tuple[TransferRecord, DispatchMarker | None]:
        try:
            valid_project = str(UUID(project_id)) == project_id
        except ValueError:
            valid_project = False
        if not valid_project:
            raise AppError(404, "PROJECT_NOT_FOUND", "项目不存在")
        identity = payload_digest({"projectId": project_id, "revision": request.expected_revision, "kind": request.kind})
        parameters = {"p_owner_id": owner_id, "p_project_id": project_id, "p_operation_id": request.operation_id,
            "p_kind": request.kind, "p_expected_revision": request.expected_revision, "p_identity_sha256": identity}
        result = self._call("prepare_export", **parameters)
        try:
            if isinstance(result, dict) and "existing" in result:
                return ExistingExport.model_validate(result, strict=True).existing, None
            source = ExportSource.model_validate(result, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "导出项目内容无法读取") from None

        project = source.project.model_dump(by_alias=True, exclude_unset=True)
        canvases = [canvas.model_dump(by_alias=True, exclude_unset=True) for canvas in source.canvases]
        if request.kind == "game":
            game, files, issues = game_snapshot_from_records(project, request.expected_revision, source.assets, source.conflicts,
                self.settings, verify_source=False)
            snapshot = {"game": game, "files": files}
        else:
            files, issues = [export_asset_record(asset) for asset in source.assets], []
            snapshot = {"project": without_credentials(project), "canvases": without_credentials(canvases),
                "revision": request.expected_revision, "files": files, "history": without_credentials(source.history)}
        preview = {"sourceFormat": BACKUP_SOURCE_FORMAT, "issues": issues, "projects": [{
            "id": project_id, "title": source.project.title,
            "sceneCount": sum(node["type"] == "scene" for node in project["graphData"]["nodes"]),
            "canvasNodeCount": sum(len(canvas.nodes) for canvas in source.canvases), "assetCount": len(files),
        }]}
        job_id = new_id()
        confirmation = payload_digest({"id": job_id, "revision": request.expected_revision, "private": snapshot})
        return self.accept_export(owner_id=owner_id, project_id=project_id, job_id=job_id, operation_id=request.operation_id,
            kind=request.kind, expected_revision=request.expected_revision, identity_sha256=identity,
            confirmation_hash=confirmation, snapshot=snapshot, preview=preview)

    def _write_export(self, function: str, marker: DispatchMarker | None, **parameters) -> tuple[TransferRecord, DispatchMarker | None]:
        try:
            result = self._call(function, **parameters, p_dispatch_revision=str(marker.revision) if marker else None,
                p_due_ms=marker.due_ms if marker else None)
        except AppError as error:
            if marker is not None and 400 <= error.status < 500:
                self._remove_obsolete(marker)
            raise
        try:
            accepted = ExportAcceptance.model_validate(result, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "导出任务回执无效") from None
        self._remove_superseded(result)
        if accepted.replayed and marker is not None:
            self._remove_obsolete(marker)
        return accepted.job, None if accepted.replayed else marker

    def accept_export(self, *, owner_id: str, project_id: str, job_id: str, operation_id: str, kind: str,
                      expected_revision: int, identity_sha256: str, confirmation_hash: str, snapshot: dict, preview: dict):
        marker = self._publish(job_id, _ACCEPT_WINDOW_MS) if kind == "backup" and not preview["issues"] else None
        return self._write_export("accept_export", marker, p_owner_id=owner_id, p_project_id=project_id, p_job_id=job_id,
            p_operation_id=operation_id, p_kind=kind, p_expected_revision=expected_revision, p_identity_sha256=identity_sha256,
            p_confirmation_hash=confirmation_hash, p_private_snapshot=snapshot, p_preview=preview)

    def resume_export(self, owner_id: str, job_id: str, *, confirmation_hash=None, missing_archive_sha256=None):
        marker = self._publish(job_id, _ACCEPT_WINDOW_MS)
        return self._write_export("resume_export", marker, p_owner_id=owner_id, p_job_id=job_id, p_confirmation_hash=confirmation_hash,
            p_missing_archive_sha256=missing_archive_sha256)

    def read(self, owner_id: str, job_id: str) -> dict:
        return self._call("read", p_owner_id=owner_id, p_job_id=job_id)

    def _claim(self, marker: DispatchMarker, worker_id: str, *, immediate: bool):
        recovery = self._publish(str(marker.job_id), _RECOVERY_DUE_MS)
        result = self._call("claim", p_job_id=str(marker.job_id), p_worker_id=worker_id,
            p_marker_revision=str(marker.revision), p_marker_due_ms=marker.due_ms, p_immediate=immediate,
            p_recovery_revision=str(recovery.revision), p_recovery_due_ms=recovery.due_ms)
        if result["state"] == "claimed":
            self._remove_superseded(result)
            return ClaimedTransfer.model_validate(result["job"])
        self._remove_obsolete(recovery)
        if result["state"] == "stale" and marker.due_ms <= epoch_milliseconds():
            self._remove_obsolete(marker)
        return None

    def _renew(self, job_id: str, worker_id: str) -> bool:
        return self._call("renew", p_job_id=job_id, p_worker_id=worker_id) is True

    def _settle(self, job_id: str, worker_id: str, archive: dict | None, error: tuple[str, str] | None) -> None:
        result = self._call("settle_export", p_job_id=job_id, p_worker_id=worker_id, p_archive=archive,
            p_error_code=error[0] if error else None, p_error_message=error[1] if error else None)
        self._remove_superseded(result)

    def _build(self, job: ClaimedTransfer) -> tuple[dict | None, tuple[str, str] | None]:
        try:
            if job.source_revision is None:
                raise AppError(500, "TRANSFER_STORAGE_FAILED", "导出的项目版本记录不完整")
            return build_archive(job, self.settings, job.lease_owner), None
        except AppError as error:
            return None, (error.code, error.message)
        except subprocess.TimeoutExpired:
            return None, ("GAME_RENDER_TIMEOUT", "游戏播放器生成超时，当前操作已终止")
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
            return None, ("TRANSFER_STORAGE_FAILED", "项目文件无法保存，请检查存储状态")

    async def run_transfer(self, marker: DispatchMarker, *, immediate: bool = False) -> None:
        if marker.kind != "transfer" or (not immediate and marker.due_ms > epoch_milliseconds()):
            return
        worker_id = str(uuid4())
        job = await asyncio.to_thread(self._claim, marker, worker_id, immediate=immediate)
        if job is None:
            return
        if job.kind == "asset_upload":
            from app.services.cloudbase_direct_uploads import inspect_and_settle_asset_upload

            processing = asyncio.create_task(asyncio.to_thread(inspect_and_settle_asset_upload, self.settings, job, worker_id))
        elif job.kind == "import":
            from app.services.cloudbase_imports import inspect_and_settle_import

            processing = asyncio.create_task(asyncio.to_thread(inspect_and_settle_import, self.settings, job, worker_id))
        else:
            processing = asyncio.create_task(asyncio.to_thread(self._build, job))
        active_lease = True
        while not processing.done():
            done, _ = await asyncio.wait({processing}, timeout=5)
            if done:
                break
            if not await asyncio.to_thread(self._renew, job.id, worker_id):
                active_lease = False
                break
        result = await processing
        if job.kind in {"asset_upload", "import"}:
            self._remove_superseded(result)
        elif active_lease:
            archive, error = result
            await asyncio.to_thread(self._settle, job.id, worker_id, archive, error)

    async def wake_exports(self, markers: tuple[DispatchMarker, ...]) -> None:
        for marker in markers:
            await self.run_transfer(marker)
