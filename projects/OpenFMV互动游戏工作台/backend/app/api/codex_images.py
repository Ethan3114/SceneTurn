import asyncio

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.connections import current_connection
from app.api.deps import current_user, get_db
from app.codex_image_models import CodexImageJob
from app.codex_image_schemas import CodexImageConfirm, CodexImageCreate, CodexImageEvent, CodexImageResultMetadata, CodexImageStop
from app.connection_models import CodexConnection
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import Asset, User
from app.services import codex_images
from app.services.material_references import validate_reference_assets
from app.services.media import resolve_asset_path, save_upload
from app.services.projects import require_project


router = APIRouter(tags=["codex-images"])


@router.post("/projects/{project_id}/codex-images", status_code=201)
def create(project_id: str, body: CodexImageCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"job": codex_images.create_job(session, user.id, project_id, body.model_dump(by_alias=True), get_settings())}


@router.get("/projects/{project_id}/codex-images")
def list_jobs(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    jobs = session.scalars(select(CodexImageJob).where(CodexImageJob.project_id == project_id, CodexImageJob.owner_id == user.id,
        CodexImageJob.input_json["materialRunId"].as_string().is_(None),
        CodexImageJob.input_json["canvasImageJob"].as_boolean().is_not(True)).order_by(CodexImageJob.created_at.desc())).all()
    return {"jobs": [codex_images.job_view(session, job) for job in jobs]}


@router.get("/projects/{project_id}/codex-images/{identifier}")
def get_job(project_id: str, identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"job": codex_images.job_view(session, codex_images.owned_job(session, user.id, project_id, identifier))}


@router.post("/projects/{project_id}/codex-images/{identifier}/confirm")
def confirm(project_id: str, identifier: str, body: CodexImageConfirm, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"job": codex_images.confirm_job(session, user.id, project_id, identifier, body.model_dump(by_alias=True))}


@router.post("/projects/{project_id}/codex-images/{identifier}/stop")
def stop(project_id: str, identifier: str, body: CodexImageStop, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"job": codex_images.stop_job(session, user.id, project_id, identifier, body.operation_id)}


@router.post("/connections/images/claim")
def claim(connection: CodexConnection = Depends(current_connection), session: Session = Depends(get_db)):
    return {"result": codex_images.claim_job(session, connection, get_settings())}


@router.post("/connections/images/{identifier}/events")
def event(identifier: str, body: CodexImageEvent, connection: CodexConnection = Depends(current_connection), session: Session = Depends(get_db)):
    return {"job": codex_images.event_job(session, connection, identifier, body.model_dump(by_alias=True))}


@router.get("/connections/images/{identifier}/references/{asset_id}/content")
def reference(identifier: str, asset_id: str, connection: CodexConnection = Depends(current_connection), session: Session = Depends(get_db)):
    job = codex_images.connection_job(session, connection, identifier)
    frozen = job.input_json["referenceAssets"]
    if asset_id not in {item["assetId"] for item in frozen}:
        raise AppError(403, "CODEX_IMAGE_REFERENCE_NOT_ALLOWED", "此图片未被本人选为当前任务参考")
    project = require_project(session, job.project_id, job.owner_id)
    validate_reference_assets(session, project, [item["assetId"] for item in frozen], get_settings(), frozen)
    asset = session.get(Asset, asset_id)
    return FileResponse(resolve_asset_path(get_settings(), asset.relpath), media_type=asset.mime_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/connections/images/{identifier}/result")
async def result(identifier: str, metadata: str = Form(max_length=2000), file: UploadFile = File(),
                 connection: CodexConnection = Depends(current_connection), session: Session = Depends(get_db)):
    try:
        body = CodexImageResultMetadata.model_validate_json(metadata).model_dump(by_alias=True)
    except ValidationError as error:
        raise AppError(422, "CODEX_IMAGE_METADATA_INVALID", "图片回传元数据未通过结构检查") from error
    job = codex_images.check_result(session, connection, identifier, body)
    if job.asset_id:
        return {"job": codex_images.job_view(session, job)}
    settings = get_settings().model_copy(update={"max_upload_bytes": codex_images.MAX_IMAGE_BYTES})
    source = media = await save_upload(file, settings)
    try:
        media = await asyncio.to_thread(codex_images.prepare_result_media, job, body, source, settings)
        return {"job": codex_images.persist_result(session, connection, identifier, body, source, media)}
    finally:
        session.rollback()
        for stored in {item.relpath: item for item in (source, media)}.values():
            if session.scalar(select(Asset.id).where(Asset.relpath == stored.relpath)) is None:
                for path in (stored.relpath, stored.poster_relpath):
                    if path:
                        resolve_asset_path(settings, path).unlink()
