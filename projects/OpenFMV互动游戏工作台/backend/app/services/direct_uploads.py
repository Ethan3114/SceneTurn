import math
import tempfile
import time
from pathlib import Path

from pydantic import Field
from sqlalchemy import func, select

from app.core.errors import AppError
from app.core.security import token_hash
from app.models import Asset, new_id
from app.schemas import WireModel
from app.services.asset_ingest import uploaded_asset_view
from app.services.media import TEXT_EXTENSIONS, TEXT_LIMIT_BYTES, _filename, _store_text, _text_header, _validate_and_store
from app.services.object_storage import private_storage
from app.services.project_transfers import matching_job, transfer_root, transfer_view
from app.services.projects import lock_write, payload_digest, require_project
from app.services.transfer_dispatch import commit_transfer
from app.transfer_models import TransferJob


UPLOAD_TTL = 3600
MAX_PENDING_UPLOADS = 4


class DirectUploadCreate(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    filename: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n\x00]+$")
    bytes: int = Field(gt=0, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def validate_import_filename(filename):
    if (not filename or not filename.lower().endswith(".zip") or Path(filename).name != filename
            or len(filename) > 200 or any(value in filename for value in ("\\", "\x00", ":"))):
        raise AppError(422, "IMPORT_FILENAME_INVALID", "请选择包含完整项目与媒体的ZIP文件")


def upload_identity(kind, project_id, body):
    if kind == "import":
        return payload_digest({"sha256": body.sha256})
    return payload_digest({"projectId": project_id, **body.model_dump(by_alias=True)})


def validate_upload_request(settings, kind, body):
    if kind == "import":
        validate_import_filename(body.filename)
    else:
        _filename(body.filename)
    suffix = Path(body.filename).suffix.lower()
    is_text = kind == "asset_upload" and suffix in TEXT_EXTENSIONS
    limit = min(settings.max_upload_bytes, TEXT_LIMIT_BYTES) if is_text else settings.max_upload_bytes
    if body.bytes > limit:
        code = "IMPORT_TOO_LARGE" if kind == "import" else "MEDIA_TOO_LARGE"
        raise AppError(413, code, "文件超过允许的上传大小")
    if is_text:
        _text_header(body.content_type, suffix)


def reserve_upload(session, settings, owner_id, project_id, kind, login_token, body):
    validate_upload_request(settings, kind, body)
    identity = upload_identity(kind, project_id, body)
    now = time.time()
    session_hash = token_hash(login_token)
    lock_write(session)
    if project_id is not None:
        require_project(session, project_id, owner_id)
    value = matching_job(session, owner_id, kind, body.operation_id, identity)
    if value is not None and value.status != "awaiting_upload":
        session.commit()
        return value, True
    if value is not None:
        prior = value.request_json["directUpload"]
        if (prior["bytes"], prior["sha256"]) != (body.bytes, body.sha256):
            raise AppError(409, "OPERATION_CONFLICT", "同一上传已绑定其他文件")
        if prior["expiresAt"] > now:
            if prior["sessionHash"] != session_hash:
                raise AppError(409, "UPLOAD_SESSION_CHANGED", "该文件已在另一登录会话准备，请在原会话完成上传")
            session.commit()
            return value, True

    pending_count = session.scalar(select(func.count()).select_from(TransferJob).where(
        TransferJob.owner_id == owner_id, TransferJob.status == "awaiting_upload",
        TransferJob.request_json["directUpload"]["expiresAt"].as_float() > now))
    if pending_count >= MAX_PENDING_UPLOADS:
        raise AppError(429, "TOO_MANY_PENDING_UPLOADS", "请先完成已开始的上传，再添加新文件")
    reused = value is not None
    if not reused:
        value = TransferJob(id=new_id(), owner_id=owner_id, project_id=project_id, kind=kind,
                            operation_id=body.operation_id, identity_sha256=identity, status="awaiting_upload")
        session.add(value)
    key = f"transfers/{value.id}/source.zip" if kind == "import" else f"incoming/{value.id}/source"
    value.request_json = {
        "phase": "preview" if kind == "import" else "ingest",
        **({"archiveSha256": body.sha256} if kind == "import" else {}),
        "directUpload": {"key": key, "filename": body.filename, "contentType": body.content_type,
                         "bytes": body.bytes, "sha256": body.sha256, "expiresAt": now + UPLOAD_TTL,
                         "sessionHash": session_hash},
    }
    value.updated_at = now
    session.commit()
    return value, reused


def require_upload(session, identifier, owner_id, project_id, kind):
    query = select(TransferJob).where(TransferJob.id == identifier, TransferJob.owner_id == owner_id, TransferJob.kind == kind)
    if project_id is not None:
        query = query.where(TransferJob.project_id == project_id)
        require_project(session, project_id, owner_id)
    value = session.scalar(query)
    if value is None or "directUpload" not in value.request_json:
        raise AppError(404, "UPLOAD_NOT_FOUND", "上传记录不存在或无权访问")
    return value


def check_upload_session(session, identifier, owner_id, project_id, kind, login_token):
    value = require_upload(session, identifier, owner_id, project_id, kind)
    slot = value.request_json["directUpload"]
    if slot["sessionHash"] != token_hash(login_token):
        raise AppError(404, "UPLOAD_NOT_FOUND", "上传记录不存在或无权访问")
    if value.status == "awaiting_upload" and slot["expiresAt"] <= time.time():
        raise AppError(409, "UPLOAD_EXPIRED", "上传授权已过期，请重新选择文件")
    return value


def uploaded_bytes_match(storage, slot):
    try:
        size = storage.object_size(slot["key"])
    except AppError as error:
        if error.code == "STORAGE_OBJECT_MISSING":
            return False
        raise
    if size != slot["bytes"]:
        raise AppError(422, "DIRECT_UPLOAD_LENGTH_MISMATCH", "上传的文件大小与原文件不一致")
    return True


def finish_upload(session, settings, identifier, owner_id, project_id, kind, login_token):
    value = check_upload_session(session, identifier, owner_id, project_id, kind, login_token)
    if value.status != "awaiting_upload":
        return value
    slot = value.request_json["directUpload"]
    session.commit()
    storage = private_storage(settings)
    if storage is None:
        raise AppError(503, "DIRECT_STORAGE_UNAVAILABLE", "私有存储尚未连接")
    if not uploaded_bytes_match(storage, slot):
        raise AppError(409, "UPLOAD_OBJECT_MISSING", "尚未收到完整的上传文件，请重试")
    lock_write(session)
    session.expire(value)
    value = check_upload_session(session, identifier, owner_id, project_id, kind, login_token)
    if value.status == "awaiting_upload":
        value.status, value.updated_at = "queued", time.time()
    commit_transfer(session, value, settings)
    return value


def upload_result(session, value):
    if value.kind == "import":
        return {"transfer": transfer_view(value)}
    result = {"status": value.status, "errorCode": value.error_code, "errorMessage": value.error_message}
    if value.status == "ready":
        project = require_project(session, value.project_id, value.owner_id)
        asset = session.get(Asset, value.result_json["assetId"])
        if asset is None or (asset.owner_id, asset.project_id) != (value.owner_id, project.id):
            raise AppError(404, "ASSET_NOT_FOUND", "素材不存在或无权访问")
        result["result"] = uploaded_asset_view(session, project, asset)
    return result


def prepare_upload(session, settings, owner_id, project_id, kind, login_token, body):
    storage = private_storage(settings)
    if storage is None:
        if project_id is not None:
            require_project(session, project_id, owner_id)
        return {"mode": "multipart"}
    value, reused = reserve_upload(session, settings, owner_id, project_id, kind, login_token, body)
    if value.status != "awaiting_upload":
        return {"mode": "existing", "id": value.id, "result": upload_result(session, value)}

    slot = value.request_json["directUpload"]
    if reused and uploaded_bytes_match(storage, slot):
        return {"mode": "uploaded", "id": value.id}
    expires_in = math.floor(slot["expiresAt"] - time.time())
    if expires_in <= 0:
        raise AppError(409, "UPLOAD_EXPIRED", "上传授权已过期，请重新选择文件")
    signed = storage.signed_upload(slot["key"], expires_in=expires_in)
    return {"mode": "direct", "id": value.id, "expiresAt": slot["expiresAt"], "upload": signed}


def materialize_upload(settings, value, destination):
    slot = value.request_json["directUpload"]
    storage = private_storage(settings)
    if storage is None:
        raise AppError(503, "DIRECT_STORAGE_UNAVAILABLE", "私有存储尚未连接")
    path = storage.materialize(slot["key"], destination, max_bytes=slot["bytes"], sha256=slot["sha256"])
    if path.stat().st_size != slot["bytes"]:
        raise AppError(422, "DIRECT_UPLOAD_LENGTH_MISMATCH", "上传的文件大小与原文件不一致")
    return path


def ensure_import_source(settings, value):
    source = transfer_root(settings, value.id) / "source.zip"
    source.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return materialize_upload(settings, value, source)


def inspect_upload(settings, value):
    slot = value.request_json["directUpload"]
    parent = settings.tmp_dir / "incoming"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=f"{value.id}-", dir=parent) as directory:
        path = materialize_upload(settings, value, Path(directory) / "source.part")
        name = _filename(slot["filename"])
        suffix = Path(name).suffix.lower()
        mime = TEXT_EXTENSIONS.get(suffix)
        if mime:
            return _store_text(path, settings, name, suffix, mime, _text_header(slot["contentType"], suffix), source_key=slot["key"])
        return _validate_and_store(path, settings, name, False, source_key=slot["key"])


def delete_asset_staging(settings, value):
    storage = private_storage(settings)
    if storage is not None:
        storage.delete(value.request_json["directUpload"]["key"])
