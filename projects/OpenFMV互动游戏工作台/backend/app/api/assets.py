import copy
import time

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import Asset, User
from app.services.canvas_delivery import public_project_envelope
from app.services.media import asset_view, resolve_asset_path, save_upload
from app.services.projects import iso_timestamp, lock_write, project_envelope, record_project_version, require_project


router = APIRouter(tags=["assets"])


def owned_asset(session: Session, asset_id: str, owner_id: str) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None or asset.owner_id != owner_id:
        raise AppError(404, "ASSET_NOT_FOUND", "素材不存在或无权访问")
    require_project(session, asset.project_id, owner_id)
    return asset


@router.post("/projects/{project_id}/assets", status_code=201)
async def upload_asset(project_id: str, file: UploadFile = File(), user: User = Depends(current_user), session: Session = Depends(get_db)):
    project = require_project(session, project_id, user.id)
    media = await save_upload(file, get_settings())
    lock_write(session)
    session.refresh(project)
    asset = Asset(owner_id=user.id, project_id=project_id, generation_id=None, name=media.name,
                  relpath=media.relpath, poster_relpath=media.poster_relpath, mime_type=media.mime_type,
                  bytes=media.bytes, sha256=media.sha256, duration_ms=media.duration_ms,
                  width=media.width, height=media.height, metadata_json=media.metadata_json)
    session.add(asset)
    session.flush()
    snapshot = copy.deepcopy(project.data_json)
    asset_type = "text" if media.mime_type.startswith("text/") else media.mime_type.split("/", 1)[0]
    snapshot["assets"].append({
        "id": asset.id, "type": asset_type, "name": asset.name,
        "path": f"/api/v1/assets/{asset.id}/content", "relativePath": f"assets/{asset.id}",
        "importedAt": iso_timestamp(time.time()),
        "metadata": {**media.metadata_json, "duration": (asset.duration_ms or 0) / 1000, "width": asset.width, "height": asset.height,
                     "poster": f"/api/v1/assets/{asset.id}/poster" if asset.poster_relpath else None},
    })
    snapshot["updatedAt"] = iso_timestamp(time.time())
    project.data_json = snapshot
    project.revision += 1
    project.updated_at = time.time()
    record_project_version(session, project)
    result = {"asset": asset_view(asset), **public_project_envelope(session, user.id, project_envelope(session, project))}
    session.commit()
    return result


@router.get("/assets/{asset_id}/content")
def asset_content(asset_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    asset = owned_asset(session, asset_id, user.id)
    media_type = asset.mime_type
    if media_type.startswith("text/"):
        media_type = f"{media_type}; charset={asset.metadata_json['encoding']}"
    return FileResponse(resolve_asset_path(get_settings(), asset.relpath), media_type=media_type,
                        filename=asset.name, content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/assets/{asset_id}/poster")
def asset_poster(asset_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    asset = owned_asset(session, asset_id, user.id)
    if not asset.poster_relpath:
        raise AppError(404, "POSTER_NOT_FOUND", "该素材没有封面")
    return FileResponse(resolve_asset_path(get_settings(), asset.poster_relpath), media_type="image/jpeg",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
