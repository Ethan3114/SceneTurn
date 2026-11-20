from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.models import User
from app.schemas import ProjectEnvelope, WireModel
from app.services.canvas_delivery import public_project_envelope
from app.services.image_split import split_image


router = APIRouter(tags=["media-tools"])


class ImageSplitRequest(WireModel):
    scene_id: str = Field(min_length=1, max_length=100)
    source_node_id: str = Field(min_length=1, max_length=100)
    rows: int = Field(ge=1, le=12, strict=True)
    columns: int = Field(ge=1, le=12, strict=True)
    horizontal_lines: list[float] | None = Field(default=None, max_length=11)
    vertical_lines: list[float] | None = Field(default=None, max_length=11)
    expected_revision: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=100)


class ImageSplitPiece(WireModel):
    node_id: str
    asset_id: str
    row: int
    column: int


class ImageSplitResult(ProjectEnvelope):
    pieces: list[ImageSplitPiece]


@router.post("/projects/{project_id}/media-tools/split", response_model=ImageSplitResult, response_model_exclude_unset=True, status_code=201)
def split(project_id: str, body: ImageSplitRequest, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return public_project_envelope(session, user.id, split_image(session, project_id, user.id, body.model_dump(by_alias=True), get_settings()))
