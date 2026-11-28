import hashlib
import mimetypes
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import current_user, get_db, login_token
from app.api.deployment_dispatch import start_accepted_transfer
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import User, new_id
from app.services.cloudbase_database import gateway_enabled
from app.services.cloudbase_imports import CloudBaseImports, TransferRecord, visible_transfer, visible_transfers
from app.services.direct_uploads import DirectUploadCreate, finish_upload, prepare_upload, upload_result, validate_import_filename
from app.services.durable_files import materialize_file, persist_file
from app.services.object_storage import private_storage
from app.services.project_transfers import confirm_transfer, create_export, file_sha256, matching_job, require_transfer, transfer_root, transfer_view
from app.services.projects import lock_write, payload_digest, require_project
from app.services.transfer_archives import checked_member
from app.services.transfer_dispatch import commit_transfer
from app.services.transfer_rpc import TransferRpc, export_artifact
from app.transfer_models import TransferJob
from app.transfer_schemas import ExportCreate, SourceHistory, TransferConfirm, TransferList, TransferResult


router = APIRouter(tags=["project-transfers"])


@router.post("/transfers/imports", response_model=TransferResult, status_code=201)
async def upload_import(request: Request, tasks: BackgroundTasks, file: UploadFile = File(), operation_id: str = Form(alias="operationId", min_length=1, max_length=100),
                        user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    validate_import_filename(file.filename)
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
    if gateway_enabled(settings):
        dispatch = TransferRpc(settings)
        value, marker = await run_in_threadpool(CloudBaseImports(settings).accept_multipart, user.id, identifier,
            operation_id, sha256, source, dispatch.publish_marker)
        if marker is not None:
            tasks.add_task(dispatch.run_transfer, marker, immediate=True)
        return {"transfer": transfer_view(value)}
    identity = payload_digest({"sha256": sha256})
    lock_write(session)
    previous = matching_job(session, user.id, "import", operation_id, identity)
    if previous:
        if previous.status == "awaiting_upload":
            raise AppError(409, "IMPORT_UPLOAD_IN_PROGRESS", "请通过已准备的上传授权继续导入")
        commit_transfer(session, previous, settings)
        start_accepted_transfer(request, tasks, previous)
        return {"transfer": transfer_view(previous)}
    persist_file(settings, source, "application/zip")
    value = TransferJob(id=identifier, owner_id=user.id, project_id=None, kind="import", operation_id=operation_id,
        identity_sha256=identity, source_revision=None, status="queued", request_json={"phase": "preview", "archiveSha256": sha256}, private_json={}, result_json={})
    session.add(value)
    commit_transfer(session, value, settings)
    start_accepted_transfer(request, tasks, value)
    return {"transfer": transfer_view(value)}


@router.post("/transfers/imports/direct-upload")
def prepare_import_upload(body: DirectUploadCreate, request: Request, response: Response,
                          user: User = Depends(current_user), session: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    token = login_token(request)
    if gateway_enabled(settings):
        return CloudBaseImports(settings).prepare(user.id, token, body)
    return prepare_upload(session, settings, user.id, None, "import", token, body)


@router.post("/transfers/imports/direct-upload/{identifier}/complete", response_model=TransferResult, status_code=202)
def complete_import_upload(identifier: str, request: Request, tasks: BackgroundTasks, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    token = login_token(request)
    if gateway_enabled(settings):
        dispatch = TransferRpc(settings)
        value, marker = CloudBaseImports(settings).complete(user.id, identifier, token, dispatch.publish_marker)
        if marker is not None:
            tasks.add_task(dispatch.run_transfer, marker, immediate=True)
        return {"transfer": transfer_view(value)}
    value = finish_upload(session, settings, identifier, user.id, None, "import", token)
    start_accepted_transfer(request, tasks, value)
    return upload_result(session, value)


@router.post("/projects/{project_id}/exports", response_model=TransferResult, status_code=201)
def export_project(project_id: str, body: ExportCreate, request: Request, tasks: BackgroundTasks,
                   user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        dispatch = TransferRpc(settings)
        value, marker = dispatch.create_export(user.id, project_id, body)
        if marker is not None:
            tasks.add_task(dispatch.run_transfer, marker, immediate=True)
        return {"transfer": transfer_view(value)}
    value = create_export(session, user.id, project_id, body, settings)
    start_accepted_transfer(request, tasks, value)
    return {"transfer": transfer_view(value)}


@router.get("/transfers", response_model=TransferList)
def transfers(project_id: str | None = None, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return {"transfers": [transfer_view(value) for value in visible_transfers(settings, user.id, project_id or None)]}
    query = select(TransferJob).where(TransferJob.owner_id == user.id,
                                      TransferJob.kind.in_(["import", "backup", "game"]), TransferJob.status != "awaiting_upload")
    if project_id:
        require_project(session, project_id, user.id)
        query = query.where(TransferJob.project_id == project_id)
    return {"transfers": [transfer_view(value) for value in session.scalars(query.order_by(TransferJob.created_at.desc()))]}


@router.get("/transfers/{identifier}", response_model=TransferResult)
def transfer(identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return {"transfer": transfer_view(visible_transfer(settings, user.id, identifier))}
    return {"transfer": transfer_view(require_transfer(session, identifier, user.id))}


@router.post("/transfers/{identifier}/confirm", response_model=TransferResult)
def confirm(identifier: str, body: TransferConfirm, request: Request, tasks: BackgroundTasks,
            user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        current = visible_transfer(settings, user.id, identifier)
        dispatch = TransferRpc(settings)
        if current.kind == "import":
            value, marker = CloudBaseImports(settings).resume(user.id, identifier, body.confirmation_hash, dispatch.publish_marker)
        elif current.confirmation_hash != body.confirmation_hash:
            raise AppError(409, "TRANSFER_CONFIRMATION_CHANGED", "确认内容已变化，请重新核对")
        elif current.status in {"queued", "processing", "ready"}:
            return {"transfer": transfer_view(current)}
        elif current.status != "awaiting_confirmation":
            raise AppError(409, "TRANSFER_NOT_CONFIRMABLE", "当前记录尚未通过完整检查")
        else:
            value, marker = dispatch.resume_export(user.id, identifier, confirmation_hash=body.confirmation_hash)
        if marker is not None:
            tasks.add_task(dispatch.run_transfer, marker, immediate=True)
        return {"transfer": transfer_view(value)}
    value = confirm_transfer(session, user.id, identifier, body.confirmation_hash, settings)
    start_accepted_transfer(request, tasks, value)
    return {"transfer": transfer_view(value)}


def ready_archive(value: TransferJob | TransferRecord, settings) -> Path:
    if value.status != "ready" or value.kind == "import":
        raise AppError(409, "TRANSFER_NOT_READY", "项目或游戏包尚未完成")
    key = value.private_json.get("archive")
    if not isinstance(key, str) or not key:
        raise AppError(404, "TRANSFER_FILE_MISSING", "成品包文件缺失，原项目与导出记录保留")
    archive = (settings.data_dir / key).resolve()
    if not archive.is_relative_to(transfer_root(settings, value.id)):
        raise AppError(404, "TRANSFER_FILE_MISSING", "成品包文件缺失，原项目与导出记录保留")
    return archive


def check_archive(value, archive):
    if not archive.is_file():
        raise AppError(404, "TRANSFER_FILE_MISSING", "成品包文件缺失，原项目与导出记录保留")
    if archive.stat().st_size != value.private_json["archiveBytes"] or file_sha256(archive) != value.private_json["archiveSha256"]:
        raise AppError(409, "TRANSFER_FILE_CHANGED", "成品包摘要与保存记录不一致")


@router.get("/transfers/{identifier}/download")
def download(identifier: str, request: Request, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    value = export_artifact(settings, user.id, identifier) if gateway_enabled(settings) else require_transfer(session, identifier, user.id)
    archive = ready_archive(value, settings)
    name = "SceneTurn 项目备份" if value.kind == "backup" else "SceneTurn 互动游戏"
    filename = f"{name}-v{value.source_revision}.zip"
    headers = {"Cache-Control": "private, no-store"}
    storage = private_storage(settings)
    if storage is not None:
        return storage.response(archive.relative_to(settings.data_dir.resolve()).as_posix(), media_type="application/zip",
                                headers=headers, range_header=request.headers.get("range"), filename=filename, disposition="attachment")
    check_archive(value, archive)
    return FileResponse(archive, media_type="application/zip", filename=filename, headers=headers)


@router.post("/transfers/{identifier}/retry", response_model=TransferResult)
def retry(identifier: str, request: Request, tasks: BackgroundTasks, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        current = export_artifact(settings, user.id, identifier)
        dispatch = TransferRpc(settings)
        if current.kind == "import":
            value, marker = CloudBaseImports(settings).resume(user.id, identifier, None, dispatch.publish_marker)
        elif current.status in {"queued", "processing"}:
            return {"transfer": transfer_view(current)}
        else:
            archive_sha = None
            if current.status == "ready":
                archive = ready_archive(current, settings)
                storage = private_storage(settings)
                if storage is None:
                    raise AppError(503, "TRANSFER_STORAGE_FAILED", "成品包存储尚未配置")
                key = archive.relative_to(settings.data_dir.resolve()).as_posix()
                try:
                    size = storage.object_size(key)
                except AppError as error:
                    if error.code != "STORAGE_OBJECT_MISSING":
                        raise
                else:
                    if size == current.private_json["archiveBytes"]:
                        raise AppError(409, "TRANSFER_ALREADY_READY", "当前成品包完整可用")
                archive_sha = current.private_json["archiveSha256"]
            elif current.status != "failed":
                raise AppError(409, "TRANSFER_RETRY_NOT_ALLOWED", "请修复来源后重新导出")
            value, marker = dispatch.resume_export(user.id, identifier, missing_archive_sha256=archive_sha)
        if marker is not None:
            tasks.add_task(dispatch.run_transfer, marker, immediate=True)
        return {"transfer": transfer_view(value)}
    lock_write(session)
    value = require_transfer(session, identifier, user.id)
    if value.status in {"queued", "processing"}:
        commit_transfer(session, value, settings)
        start_accepted_transfer(request, tasks, value)
        return {"transfer": transfer_view(value)}
    if value.status == "ready" and value.kind != "import":
        archive = (settings.data_dir / value.private_json["archive"]).resolve()
        if not archive.is_relative_to(transfer_root(settings, identifier)):
            raise AppError(422, "TRANSFER_PATH_INVALID", "成品包目录无效")
        try:
            materialize_file(settings, archive, max_bytes=value.private_json["archiveBytes"],
                             sha256=value.private_json["archiveSha256"])
        except AppError as error:
            if error.code != "STORAGE_OBJECT_MISSING":
                raise
        if archive.is_file() and archive.stat().st_size == value.private_json["archiveBytes"] and file_sha256(archive) == value.private_json["archiveSha256"]:
            raise AppError(409, "TRANSFER_ALREADY_READY", "当前成品包完整可用")
    elif value.status != "failed" or value.kind == "import" and value.request_json["phase"] != "apply":
        raise AppError(409, "TRANSFER_RETRY_NOT_ALLOWED", "请修复来源后重新上传完整项目文件")
    value.status, value.error_code, value.error_message = "queued", None, None
    value.updated_at = time.time()
    commit_transfer(session, value, settings)
    start_accepted_transfer(request, tasks, value)
    return {"transfer": transfer_view(value)}


@router.get("/transfers/{identifier}/play/{name:path}")
def play(identifier: str, name: str, request: Request, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    value = export_artifact(settings, user.id, identifier) if gateway_enabled(settings) else require_transfer(session, identifier, user.id)
    return game_response(value, name, settings, request, headers={"Cache-Control": "private, no-store"})


def game_file_record(value: TransferJob | TransferRecord, name: str, settings):
    if value.status != "ready" or value.kind != "game":
        raise AppError(404, "GAME_VERSION_NOT_FOUND", "固定游戏版本不可用")
    root = (settings.data_dir / value.private_json["directory"]).resolve()
    if not root.is_relative_to(transfer_root(settings, value.id)):
        raise AppError(404, "GAME_FILE_NOT_FOUND", "该版本缺少所需文件")
    name = checked_member(name)
    permitted = {"index.html", "game.json", *(item["path"] for item in value.private_json["files"])}
    target = (root / name).resolve()
    if name not in permitted or not target.is_relative_to(root) or (private_storage(settings) is None and not target.is_file()):
        raise AppError(404, "GAME_FILE_NOT_FOUND", "该游戏版本缺少所需文件")
    expected = next((item for item in value.private_json.get("fileManifest", []) if item["path"] == name), None)
    if expected is None:
        raise AppError(409, "GAME_FILE_PROOF_MISSING", "该版本缺少播放文件的完整性记录，请重新导出")
    return target, expected


def game_file(value: TransferJob | TransferRecord, name: str, settings) -> Path:
    target, expected = game_file_record(value, name, settings)
    materialize_file(settings, target, max_bytes=expected["bytes"], sha256=expected["sha256"])
    if not target.is_file():
        raise AppError(404, "GAME_FILE_NOT_FOUND", "该游戏版本缺少所需文件")
    if target.stat().st_size != expected["bytes"] or file_sha256(target) != expected["sha256"]:
        raise AppError(409, "GAME_FILE_CHANGED", "该版本播放文件与固定内容不一致")
    return target


def game_response(value: TransferJob | TransferRecord, name: str, settings, request: Request, *, headers: dict[str, str]):
    storage = private_storage(settings)
    if storage is not None and name != "index.html":
        target, _expected = game_file_record(value, name, settings)
        key = target.relative_to(settings.data_dir.resolve()).as_posix()
        return storage.response(key, media_type=mimetypes.guess_type(target)[0] or "application/octet-stream",
                                headers=headers, range_header=request.headers.get("range"))
    return FileResponse(game_file(value, name, settings), headers=headers)


@router.get("/transfers/{identifier}/source-history", response_model=SourceHistory)
def source_history(identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    value = visible_transfer(settings, user.id, identifier, include_private=True) if gateway_enabled(settings) else require_transfer(session, identifier, user.id)
    if value.kind != "import" or value.status != "ready":
        raise AppError(409, "IMPORT_NOT_READY", "来源历史需要在完整导入后读取")
    return {"sourceFormat": value.private_json["sourceFormat"], "history": value.private_json["history"]}
