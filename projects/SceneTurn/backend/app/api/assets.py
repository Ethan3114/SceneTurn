from fastapi import APIRouter, BackgroundTasks, Depends, File, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import current_user, get_db, login_token
from app.api.deployment_dispatch import start_accepted_transfer
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.models import Asset, User
from app.services.asset_ingest import register_asset, uploaded_asset_view
from app.services.cloudbase_assets import CloudBaseAssetRecord, CloudBaseAssetRepository
from app.services.cloudbase_database import gateway_enabled
from app.services.cloudbase_direct_uploads import CloudBaseDirectAssetUploads
from app.services.direct_uploads import DirectUploadCreate, finish_upload, prepare_upload, require_upload, upload_result
from app.services.media import resolve_asset_path, save_upload
from app.services.object_storage import private_storage
from app.services.projects import lock_write, require_project
from app.services.transfer_rpc import TransferRpc


router = APIRouter(tags=["assets"])


def owned_asset(session: Session, asset_id: str, owner_id: str) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None or asset.owner_id != owner_id:
        raise AppError(404, "ASSET_NOT_FOUND", "素材不存在或无权访问")
    require_project(session, asset.project_id, owner_id)
    return asset


def readable_asset(session: Session, settings: Settings, asset_id: str, owner_id: str) -> Asset | CloudBaseAssetRecord:
    if gateway_enabled(settings):
        return CloudBaseAssetRepository(settings).get(owner_id, asset_id)
    return owned_asset(session, asset_id, owner_id)


@router.post("/projects/{project_id}/assets", status_code=201)
async def upload_asset(project_id: str, file: UploadFile = File(), user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        repository = CloudBaseAssetRepository(settings)
        await run_in_threadpool(repository.check_upload_target, user.id, project_id)
        media = await save_upload(file, settings)
        return await run_in_threadpool(repository.register, user.id, project_id, media)
    project = require_project(session, project_id, user.id)
    media = await save_upload(file, settings)
    lock_write(session)
    session.refresh(project)
    asset = register_asset(session, project, media)
    result = uploaded_asset_view(session, project, asset)
    session.commit()
    return result


@router.post("/projects/{project_id}/assets/direct-upload")
def prepare_asset_upload(project_id: str, body: DirectUploadCreate, request: Request, response: Response,
                         user: User = Depends(current_user), session: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    token = login_token(request)
    if gateway_enabled(settings):
        return CloudBaseDirectAssetUploads(settings).prepare(user.id, project_id, token, body)
    return prepare_upload(session, settings, user.id, project_id, "asset_upload", token, body)


@router.post("/projects/{project_id}/assets/direct-upload/{identifier}/complete", status_code=202)
def complete_asset_upload(project_id: str, identifier: str, request: Request, tasks: BackgroundTasks,
                          user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    token = login_token(request)
    if gateway_enabled(settings):
        dispatch = TransferRpc(settings)
        result, marker = CloudBaseDirectAssetUploads(settings).complete(user.id, project_id, identifier, token, dispatch.publish_marker)
        if marker is not None:
            tasks.add_task(dispatch.run_transfer, marker, immediate=True)
        return result
    value = finish_upload(session, settings, identifier, user.id, project_id, "asset_upload", token)
    start_accepted_transfer(request, tasks, value)
    return upload_result(session, value)


@router.get("/projects/{project_id}/assets/direct-upload/{identifier}")
def asset_upload_status(project_id: str, identifier: str, response: Response,
                        user: User = Depends(current_user), session: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseDirectAssetUploads(settings).status(user.id, project_id, identifier)
    return upload_result(session, require_upload(session, identifier, user.id, project_id, "asset_upload"))


@router.get("/assets/{asset_id}/content")
def asset_content(asset_id: str, request: Request, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    asset = readable_asset(session, settings, asset_id, user.id)
    media_type = asset.mime_type
    if media_type.startswith("text/"):
        media_type = f"{media_type}; charset={asset.metadata_json['encoding']}"
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    storage = private_storage(settings)
    if storage is not None:
        return storage.response(asset.relpath, media_type=media_type, headers=headers,
                                range_header=request.headers.get("range"), filename=asset.name)
    return FileResponse(resolve_asset_path(settings, asset.relpath), media_type=media_type,
                        filename=asset.name, content_disposition_type="inline", headers=headers)


@router.get("/assets/{asset_id}/poster")
def asset_poster(asset_id: str, request: Request, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    asset = readable_asset(session, settings, asset_id, user.id)
    if not asset.poster_relpath:
        raise AppError(404, "POSTER_NOT_FOUND", "该素材没有封面")
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    storage = private_storage(settings)
    if storage is not None:
        return storage.response(asset.poster_relpath, media_type="image/jpeg", headers=headers,
                                range_header=request.headers.get("range"))
    return FileResponse(resolve_asset_path(settings, asset.poster_relpath), media_type="image/jpeg", headers=headers)
