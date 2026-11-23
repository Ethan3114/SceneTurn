from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.models import User
from app.schemas import WireModel
from app.services import canvas_images
from app.services.image_queue import require_image_worker


router = APIRouter(tags=["canvas-images"])


class CanvasImageCreate(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    scene_id: str = Field(min_length=1, max_length=100)
    source_node_id: str = Field(min_length=1, max_length=100)
    output_node_id: str = Field(min_length=1, max_length=100)
    model_profile_id: str = Field(min_length=1, max_length=100)
    prompt: str = Field(max_length=20000)
    width: int = Field(ge=512, le=4096, strict=True)
    height: int = Field(ge=512, le=4096, strict=True)
    reference_asset_ids: list[str] | None = Field(default=None, max_length=5)


class CanvasImageConfirm(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    account_usage_confirmed: Literal[True]


@router.post("/projects/{project_id}/canvas-image-jobs", status_code=201)
def create(project_id: str, body: CanvasImageCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"job": canvas_images.create_job(session, user.id, project_id, body.model_dump(by_alias=True), get_settings())}


@router.get("/projects/{project_id}/canvas-image-jobs")
def list_jobs(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"jobs": canvas_images.list_jobs(session, user.id, project_id)}


@router.post("/projects/{project_id}/canvas-image-jobs/{job_id}/confirm")
def confirm(project_id: str, job_id: str, body: CanvasImageConfirm, request: Request, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_image_worker(getattr(request.app.state, "image_worker", None))
    return {"job": canvas_images.confirm_job(session, user.id, project_id, job_id, body.model_dump(by_alias=True), get_settings())}
