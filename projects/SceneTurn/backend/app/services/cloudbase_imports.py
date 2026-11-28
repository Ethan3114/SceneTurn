import binascii
import logging
import lzma
import math
import subprocess
import time
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import token_hash
from app.models import new_id
from app.schemas import ProjectSnapshot
from app.services.cloudbase_database import call_cloudbase_rpc
from app.services.cloudbase_direct_uploads import UploadSettlement
from app.services.deployment_queue import DispatchMarker
from app.services.direct_uploads import (
    DirectUploadCreate, ensure_import_source, upload_identity, uploaded_bytes_match, validate_upload_request,
)
from app.services.durable_files import materialize_file, persist_file
from app.services.media import resolve_asset_path
from app.services.object_storage import private_storage
from app.services.project_transfers import file_sha256, transfer_root, transfer_view
from app.services.projects import payload_digest
from app.services.transfer_archives import import_preview


logger = logging.getLogger("sceneturn")


class TransferRecord(BaseModel):
    id: str
    kind: Literal["import", "backup", "game"]
    project_id: str | None
    status: Literal["awaiting_upload", "queued", "processing", "awaiting_confirmation", "blocked", "ready", "failed"]
    source_revision: int | None
    request_json: dict = Field(default_factory=dict)
    private_json: dict = Field(default_factory=dict)
    result_json: dict
    confirmation_hash: str | None
    error_code: str | None
    error_message: str | None
    created_at: float
    updated_at: float


class ImportReservation(BaseModel):
    job: TransferRecord
    reused: bool


class ImportAcceptance(BaseModel):
    job: TransferRecord
    queued: bool


class ClaimedImport(Protocol):
    id: str
    identity_sha256: str
    request_json: dict
    private_json: dict


def _transfer_reply[T](value, schema: TypeAdapter[T]) -> T:
    try:
        return schema.validate_python(value, strict=True)
    except ValidationError:
        raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "项目传输记录暂时无法读取") from None


def visible_transfer(settings: Settings, owner_id: str, identifier: str, *, include_private: bool = False) -> TransferRecord:
    try:
        valid_id = str(UUID(identifier)) == identifier
    except ValueError:
        valid_id = False
    if not valid_id:
        raise AppError(404, "TRANSFER_NOT_FOUND", "导入或导出记录不存在或无权访问")
    function = "sceneturn_transfer_read" if include_private else "sceneturn_transfer_summary"
    value = call_cloudbase_rpc(settings, function, {"p_owner_id": owner_id, "p_job_id": identifier})
    if isinstance(value, dict) and (value.get("kind") == "asset_upload" or value.get("status") == "awaiting_upload"):
        raise AppError(404, "TRANSFER_NOT_FOUND", "导入或导出记录不存在或无权访问")
    return _transfer_reply(value, TypeAdapter(TransferRecord))


def visible_transfers(settings: Settings, owner_id: str, project_id: str | None) -> list[TransferRecord]:
    value = call_cloudbase_rpc(settings, "sceneturn_transfer_list", {"p_owner_id": owner_id, "p_project_id": project_id})
    return _transfer_reply(value, TypeAdapter(list[TransferRecord]))


class CloudBaseImports:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _call[T](self, action: str, schema: type[T], **parameters) -> T:
        return _transfer_reply(call_cloudbase_rpc(self.settings, f"sceneturn_import_{action}", parameters), TypeAdapter(schema))

    def prepare(self, owner_id: str, login_token: str, body: DirectUploadCreate) -> dict:
        storage = private_storage(self.settings)
        if storage is None:
            return {"mode": "multipart"}
        validate_upload_request(self.settings, "import", body)
        reservation = self._call("reserve", ImportReservation, p_owner_id=owner_id, p_job_id=new_id(),
            p_operation_id=body.operation_id, p_identity=upload_identity("import", None, body), p_session_hash=token_hash(login_token),
            p_filename=body.filename, p_content_type=body.content_type, p_bytes=body.bytes, p_sha256=body.sha256)
        job = reservation.job
        if job.status != "awaiting_upload":
            return {"mode": "existing", "id": job.id, "result": {"transfer": transfer_view(job)}}
        slot = job.request_json["directUpload"]
        if reservation.reused and uploaded_bytes_match(storage, slot):
            return {"mode": "uploaded", "id": job.id}
        expires_in = math.floor(slot["expiresAt"] - time.time())
        if expires_in <= 0:
            raise AppError(409, "UPLOAD_EXPIRED", "上传授权已过期，请重新选择文件")
        return {"mode": "direct", "id": job.id, "expiresAt": slot["expiresAt"],
            "upload": storage.signed_upload(slot["key"], expires_in=min(expires_in, 3600))}

    def _enqueue(self, owner_id: str, identifier: str, marker: DispatchMarker, *, session_hash: str | None = None,
                 operation_id: str | None = None, sha256: str | None = None) -> ImportAcceptance:
        return self._call("enqueue", ImportAcceptance, p_owner_id=owner_id, p_job_id=identifier,
            p_session_hash=session_hash, p_operation_id=operation_id, p_identity=payload_digest({"sha256": sha256}) if sha256 else None,
            p_archive_sha256=sha256, p_dispatch_revision=str(marker.revision), p_due_ms=marker.due_ms)

    def complete(self, owner_id: str, identifier: str, login_token: str,
                 publish: Callable[[str], DispatchMarker]) -> tuple[TransferRecord, DispatchMarker | None]:
        session_hash = token_hash(login_token)
        job = self._call("read_upload", TransferRecord, p_owner_id=owner_id, p_job_id=identifier, p_session_hash=session_hash)
        if job.status != "awaiting_upload":
            return job, None
        storage = private_storage(self.settings)
        if storage is None:
            raise AppError(503, "DIRECT_STORAGE_UNAVAILABLE", "私有存储尚未连接")
        if not uploaded_bytes_match(storage, job.request_json["directUpload"]):
            raise AppError(409, "UPLOAD_OBJECT_MISSING", "尚未收到完整的上传文件，请重试")
        marker = publish(job.id)
        accepted = self._enqueue(owner_id, job.id, marker, session_hash=session_hash)
        return accepted.job, marker if accepted.queued else None

    def accept_multipart(self, owner_id: str, identifier: str, operation_id: str, sha256: str, source: Path,
                         publish: Callable[[str], DispatchMarker]) -> tuple[TransferRecord, DispatchMarker | None]:
        persist_file(self.settings, source, "application/zip")
        marker = publish(identifier)
        accepted = self._enqueue(owner_id, identifier, marker, operation_id=operation_id, sha256=sha256)
        if accepted.job.id != identifier:
            source.unlink()
            storage = private_storage(self.settings)
            if storage is not None:
                try:
                    storage.delete(f"transfers/{identifier}/source.zip")
                except AppError as failure:
                    logger.warning("清理重复项目上传的暂存对象失败（%s）", failure.code)
        return accepted.job, marker if accepted.queued else None

    def resume(self, owner_id: str, identifier: str, confirmation_hash: str | None,
               publish: Callable[[str], DispatchMarker]) -> tuple[TransferRecord, DispatchMarker | None]:
        marker = publish(identifier)
        accepted = self._call("resume", ImportAcceptance, p_owner_id=owner_id, p_job_id=identifier, p_confirmation_hash=confirmation_hash,
            p_dispatch_revision=str(marker.revision), p_due_ms=marker.due_ms)
        return accepted.job, marker if accepted.queued else None


def _preview(settings: Settings, job: ClaimedImport) -> dict:
    sha256 = job.request_json["archiveSha256"]
    if payload_digest({"sha256": sha256}) != job.identity_sha256:
        raise AppError(409, "IMPORT_SOURCE_CHANGED", "导入源摘要与保存记录不一致")
    if "directUpload" in job.request_json:
        source = ensure_import_source(settings, job)
    else:
        source = materialize_file(settings, transfer_root(settings, job.id) / "source.zip", max_bytes=settings.max_upload_bytes, sha256=sha256)
    if file_sha256(source) != sha256:
        raise AppError(409, "IMPORT_SOURCE_CHANGED", "导入源文件发生变化")
    return import_preview(job.id, source, settings)


def _verify_preview_media(settings: Settings, job: ClaimedImport) -> str:
    for item in job.private_json["projects"]:
        ProjectSnapshot.model_validate(item["project"])
        for record in item["files"]:
            path = resolve_asset_path(settings, record["relpath"])
            if path.stat().st_size != record["bytes"] or file_sha256(path) != record["sha256"]:
                raise AppError(409, "IMPORT_MEDIA_CHANGED", "预览后的媒体文件发生变化，导入已终止")
            if record["poster_relpath"]:
                resolve_asset_path(settings, record["poster_relpath"])
    return job.private_json["sourceDigest"]


def inspect_and_settle_import(settings: Settings, job: ClaimedImport, worker_id: str) -> dict:
    preview, confirmation, source, error_code, error_message = None, None, None, None, None
    try:
        if job.request_json["phase"] == "preview":
            preview = _preview(settings, job)
            confirmation = payload_digest({"id": job.id, "source": preview["sourceDigest"], "preview": preview["view"]})
        else:
            source = _verify_preview_media(settings, job)
    except AppError as failure:
        if failure.code == "STORAGE_REQUEST_FAILED":
            raise
        error_code, error_message = failure.code, failure.message
    except ValidationError:
        error_code, error_message = "IMPORT_SCHEMA_INVALID", "项目结构未通过检查"
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, lzma.LZMAError, zlib.error):
        error_code, error_message = "IMPORT_ARCHIVE_INVALID", "压缩包结构或文件校验未通过"
    except (UnicodeDecodeError, ValueError, binascii.Error):
        error_code, error_message = "IMPORT_JSON_INVALID", "项目清单或内嵌媒体未能完整读取"
    except (OSError, subprocess.TimeoutExpired):
        error_code, error_message = "TRANSFER_STORAGE_FAILED", "项目文件无法保存或校验超时"
    if error_code is not None:
        preview, confirmation, source = None, None, None
    value = call_cloudbase_rpc(settings, "sceneturn_import_settle", {"p_job_id": job.id, "p_worker_id": worker_id,
        "p_preview": preview, "p_confirmation_hash": confirmation, "p_verified_source_digest": source,
        "p_error_code": error_code, "p_error_message": error_message[:800] if error_message else None})
    return _transfer_reply(value, TypeAdapter(UploadSettlement)).model_dump(exclude_unset=True)
