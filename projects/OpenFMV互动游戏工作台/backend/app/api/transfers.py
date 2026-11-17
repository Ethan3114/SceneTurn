import hashlib
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import User, new_id
from app.services.project_transfers import confirm_transfer, create_export, file_sha256, matching_job, require_transfer, transfer_root, transfer_view
from app.services.projects import lock_write, payload_digest, require_project
from app.services.transfer_archives import checked_member
from app.transfer_models import TransferJob
from app.transfer_schemas import ExportCreate, SourceHistory, TransferConfirm, TransferList, TransferResult


router = APIRouter(tags=["project-transfers"])


@router.post("/transfers/imports", response_model=TransferResult, status_code=201)
async def upload_import(file: UploadFile = File(), operation_id: str = Form(alias="operationId", min_length=1, max_length=100), user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if not file.filename or not file.filename.lower().endswith(".zip") or Path(file.filename).name != file.filename or len(file.filename) > 200 or any(value in file.filename for value in ("\\", "\x00", ":")):
        raise AppError(422, "IMPORT_FILENAME_INVALID", "请选择包含完整项目与媒体的ZIP文件")
    identifier = new_id()
    directory = transfer_root(settings, identifier)
    directory.mkdir(parents=True, mode=0o700)
    source = directory / "source.zip"
    digest, size = hashlib.sha256(), 0
    with source.open("xb") as output:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                raise AppError(413, "IMPORT_TOO_LARGE", "项目压缩包超过允许的上传大小")
            digest.update(chunk)
            output.write(chunk)
    if not size:
        raise AppError(422, "IMPORT_EMPTY", "项目压缩包为空")
    sha256 = digest.hexdigest()
    identity = payload_digest({"sha256": sha256})
    lock_write(session)
    previous = matching_job(session, user.id, "import", operation_id, identity)
    if previous:
        session.commit()
        return {"transfer": transfer_view(previous)}
    value = TransferJob(id=identifier, owner_id=user.id, project_id=None, kind="import", operation_id=operation_id,
        identity_sha256=identity, source_revision=None, status="queued", request_json={"phase": "preview", "archiveSha256": sha256}, private_json={}, result_json={})
    session.add(value)
    session.commit()
    return {"transfer": transfer_view(value)}


@router.post("/projects/{project_id}/exports", response_model=TransferResult, status_code=201)
def export_project(project_id: str, body: ExportCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"transfer": transfer_view(create_export(session, user.id, project_id, body, get_settings()))}


@router.get("/transfers", response_model=TransferList)
def transfers(project_id: str | None = None, user: User = Depends(current_user), session: Session = Depends(get_db)):
    query = select(TransferJob).where(TransferJob.owner_id == user.id)
    if project_id:
        require_project(session, project_id, user.id)
        query = query.where(TransferJob.project_id == project_id)
    return {"transfers": [transfer_view(value) for value in session.scalars(query.order_by(TransferJob.created_at.desc()))]}


@router.get("/transfers/{identifier}", response_model=TransferResult)
def transfer(identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"transfer": transfer_view(require_transfer(session, identifier, user.id))}


@router.post("/transfers/{identifier}/confirm", response_model=TransferResult)
def confirm(identifier: str, body: TransferConfirm, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"transfer": transfer_view(confirm_transfer(session, user.id, identifier, body.confirmation_hash))}


def ready_artifact(session, identifier, owner_id, settings):
    value = require_transfer(session, identifier, owner_id)
    if value.status != "ready" or value.kind == "import":
        raise AppError(409, "TRANSFER_NOT_READY", "项目或游戏包尚未完成")
    archive = (settings.data_dir / value.private_json["archive"]).resolve()
    if not archive.is_relative_to(transfer_root(settings, identifier)) or not archive.is_file():
        raise AppError(404, "TRANSFER_FILE_MISSING", "成品包文件缺失，原项目与导出记录保留")
    if archive.stat().st_size != value.private_json["archiveBytes"] or file_sha256(archive) != value.private_json["archiveSha256"]:
        raise AppError(409, "TRANSFER_FILE_CHANGED", "成品包摘要与保存记录不一致")
    return value, archive


@router.get("/transfers/{identifier}/download")
def download(identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    value, archive = ready_artifact(session, identifier, user.id, get_settings())
    name = "OpenFMV项目备份" if value.kind == "backup" else "OpenFMV互动游戏"
    return FileResponse(archive, media_type="application/zip", filename=f"{name}-v{value.source_revision}.zip", headers={"Cache-Control": "private, no-store"})


@router.post("/transfers/{identifier}/retry", response_model=TransferResult)
def retry(identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    lock_write(session)
    value = require_transfer(session, identifier, user.id)
    if value.status in {"queued", "processing"}:
        session.commit()
        return {"transfer": transfer_view(value)}
    if value.status == "ready" and value.kind != "import":
        archive = (settings.data_dir / value.private_json["archive"]).resolve()
        if not archive.is_relative_to(transfer_root(settings, identifier)):
            raise AppError(422, "TRANSFER_PATH_INVALID", "成品包目录无效")
        if archive.is_file() and archive.stat().st_size == value.private_json["archiveBytes"] and file_sha256(archive) == value.private_json["archiveSha256"]:
            raise AppError(409, "TRANSFER_ALREADY_READY", "当前成品包完整可用")
    elif value.status != "failed" or value.kind == "import" and value.request_json["phase"] != "apply":
        raise AppError(409, "TRANSFER_RETRY_NOT_ALLOWED", "请修复来源后重新上传完整项目文件")
    value.status, value.error_code, value.error_message = "queued", None, None
    value.updated_at = time.time()
    session.commit()
    return {"transfer": transfer_view(value)}


@router.get("/transfers/{identifier}/play/{name:path}")
def play(identifier: str, name: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    value = require_transfer(session, identifier, user.id)
    if value.status != "ready" or value.kind != "game":
        raise AppError(404, "GAME_VERSION_NOT_FOUND", "固定游戏版本不可用")
    root = (settings.data_dir / value.private_json["directory"]).resolve()
    name = checked_member(name)
    permitted = {"index.html", "game.json", *(item["path"] for item in value.private_json["files"])}
    target = (root / name).resolve()
    if name not in permitted or not target.is_relative_to(root) or not target.is_file():
        raise AppError(404, "GAME_FILE_NOT_FOUND", "该游戏版本缺少所需文件")
    expected = next((item for item in value.private_json.get("fileManifest", []) if item["path"] == name), None)
    if expected is None:
        raise AppError(409, "GAME_FILE_PROOF_MISSING", "该版本缺少播放文件的完整性记录，请重新导出")
    if target.stat().st_size != expected["bytes"] or file_sha256(target) != expected["sha256"]:
        raise AppError(409, "GAME_FILE_CHANGED", "该版本播放文件与固定内容不一致")
    return FileResponse(target, headers={"Cache-Control": "private, no-store"})


@router.get("/transfers/{identifier}/source-history", response_model=SourceHistory)
def source_history(identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    value = require_transfer(session, identifier, user.id)
    if value.kind != "import" or value.status != "ready":
        raise AppError(409, "IMPORT_NOT_READY", "来源历史需要在完整导入后读取")
    return {"sourceFormat": value.private_json["sourceFormat"], "history": value.private_json["history"]}
