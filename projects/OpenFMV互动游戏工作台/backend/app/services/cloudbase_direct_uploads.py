import logging
import math
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Literal, Protocol

from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import token_hash
from app.models import new_id
from app.schemas import WireModel
from app.services.cloudbase_assets import CloudBaseAssetRepository
from app.services.cloudbase_database import call_cloudbase_rpc
from app.services.cloudbase_projects import CloudBaseProjectRepository
from app.services.deployment_queue import DispatchMarker
from app.services.direct_uploads import (
    DirectUploadCreate, delete_asset_staging, inspect_upload, upload_identity, uploaded_bytes_match, validate_upload_request,
)
from app.services.media import StoredMedia, asset_view
from app.services.object_storage import private_storage


logger = logging.getLogger("openfmv")


class AssetUploadSlot(WireModel):
    key: str
    filename: str
    content_type: str
    bytes: int
    sha256: str
    expires_at: float


class AssetUploadJob(WireModel):
    id: str
    status: Literal["awaiting_upload", "queued", "processing", "ready", "failed"]
    direct_upload: AssetUploadSlot
    error_code: str | None
    error_message: str | None
    asset_id: str | None


class UploadReservation(WireModel):
    job: AssetUploadJob
    reused: bool


class UploadAcceptance(WireModel):
    job: AssetUploadJob
    queued: bool


class UploadSettlement(WireModel):
    state: Literal["lease_lost", "settled"]
    obsolete: dict | None = None


class ClaimedAssetUpload(Protocol):
    id: str
    request_json: dict


def _upload_reply[T: WireModel](settings: Settings, operation: str, parameters: dict, schema: type[T]) -> T:
    value = call_cloudbase_rpc(settings, f"sceneturn_asset_upload_{operation}", parameters)
    try:
        return schema.model_validate(value, strict=True)
    except ValidationError:
        raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "素材上传状态暂时无法读取") from None


class CloudBaseDirectAssetUploads:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _result(self, owner_id: str, project_id: str, job: AssetUploadJob) -> dict:
        result = {"status": job.status, "errorCode": job.error_code, "errorMessage": job.error_message}
        if job.status == "ready":
            if job.asset_id is None:
                raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "已完成的上传缺少素材标识")
            asset = CloudBaseAssetRepository(self.settings).get(owner_id, job.asset_id)
            if asset.project_id != project_id:
                raise AppError(404, "ASSET_NOT_FOUND", "素材不存在或无权访问")
            envelope = CloudBaseProjectRepository(self.settings).get(owner_id, project_id)
            result["result"] = {"asset": asset_view(asset), **envelope}
        return result

    def prepare(self, owner_id: str, project_id: str, login_token: str, body: DirectUploadCreate) -> dict:
        storage = private_storage(self.settings)
        if storage is None:
            CloudBaseAssetRepository(self.settings).check_upload_target(owner_id, project_id)
            return {"mode": "multipart"}
        validate_upload_request(self.settings, "asset_upload", body)
        reservation = _upload_reply(self.settings, "reserve", {
            "p_owner_id": owner_id, "p_project_id": project_id, "p_job_id": new_id(), "p_operation_id": body.operation_id,
            "p_identity_sha256": upload_identity("asset_upload", project_id, body), "p_session_hash": token_hash(login_token),
            "p_filename": body.filename, "p_content_type": body.content_type, "p_bytes": body.bytes, "p_sha256": body.sha256,
        }, UploadReservation)
        job = reservation.job
        if job.status != "awaiting_upload":
            return {"mode": "existing", "id": job.id, "result": self._result(owner_id, project_id, job)}
        slot = job.direct_upload
        if reservation.reused and uploaded_bytes_match(storage, slot.model_dump(by_alias=True)):
            return {"mode": "uploaded", "id": job.id}
        expires_in = math.floor(slot.expires_at - time.time())
        if expires_in <= 0:
            raise AppError(409, "UPLOAD_EXPIRED", "上传授权已过期，请重新选择文件")
        return {"mode": "direct", "id": job.id, "expiresAt": slot.expires_at,
            "upload": storage.signed_upload(slot.key, expires_in=min(expires_in, 3600))}

    def status(self, owner_id: str, project_id: str, identifier: str) -> dict:
        job = _upload_reply(self.settings, "get", {
            "p_owner_id": owner_id, "p_project_id": project_id, "p_job_id": identifier, "p_session_hash": None,
        }, AssetUploadJob)
        return self._result(owner_id, project_id, job)

    def complete(self, owner_id: str, project_id: str, identifier: str, login_token: str,
                 publish_marker: Callable[[str], DispatchMarker]) -> tuple[dict, DispatchMarker | None]:
        scope = {"p_owner_id": owner_id, "p_project_id": project_id, "p_job_id": identifier, "p_session_hash": token_hash(login_token)}
        job = _upload_reply(self.settings, "get", scope, AssetUploadJob)
        if job.status != "awaiting_upload":
            return self._result(owner_id, project_id, job), None
        storage = private_storage(self.settings)
        if storage is None:
            raise AppError(503, "DIRECT_STORAGE_UNAVAILABLE", "私有存储尚未连接")
        if not uploaded_bytes_match(storage, job.direct_upload.model_dump(by_alias=True)):
            raise AppError(409, "UPLOAD_OBJECT_MISSING", "尚未收到完整的上传文件，请重试")
        marker = publish_marker(job.id)
        accepted = _upload_reply(self.settings, "complete", {
            **scope, "p_dispatch_revision": str(marker.revision), "p_due_ms": marker.due_ms,
        }, UploadAcceptance)
        return self._result(owner_id, project_id, accepted.job), marker if accepted.queued else None


def inspect_and_settle_asset_upload(settings: Settings, job: ClaimedAssetUpload, worker_id: str) -> dict:
    media: StoredMedia | None = None
    error_code, error_message = None, None
    try:
        media = inspect_upload(settings, job)
    except AppError as failure:
        error_code, error_message = failure.code, failure.message
    except (OSError, subprocess.TimeoutExpired):
        error_code, error_message = "TRANSFER_STORAGE_FAILED", "素材无法保存或校验超时，请检查后重试"
    settlement = _upload_reply(settings, "settle", {"p_job_id": job.id, "p_worker_id": worker_id,
        "p_media": asdict(media) if media is not None else None, "p_error_code": error_code, "p_error_message": error_message}, UploadSettlement)
    if settlement.state == "settled":
        try:
            delete_asset_staging(settings, job)
        except AppError as failure:
            logger.warning("清理已处理素材的上传暂存对象失败（%s）", failure.code)
    return settlement.model_dump(exclude_unset=True)
