from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import Generation, User
from app.schemas import ProjectEnvelope, WireModel
from app.services.canvas_delivery import public_project_envelope
from app.services.cloudbase_database import gateway_enabled
from app.services.cloudbase_video import CloudBaseVideoRepository
from app.services.generation import adopt_generation, confirm_generation, create_join_generation, generation_view, quote_generation, require_generation, resume_generation, retry_generation_quote
from app.services.material_delivery import is_material_generation, material_generation_ids, material_video_artifacts, public_material_generation
from app.services.projects import require_project
from app.services.wan import MAX_REFERENCE_IMAGES


router = APIRouter(tags=["generations"])


class GenerationQuote(WireModel):
    model_profile_id: str | None = Field(default=None, min_length=1, max_length=100)
    scene_id: str = Field(min_length=1, max_length=100)
    source_node_id: str = Field(min_length=1, max_length=100)
    output_node_id: str | None = Field(default=None, min_length=1, max_length=100)
    clip_id: str | None = Field(default=None, min_length=1, max_length=100)
    prompt: str = Field(max_length=20000)
    duration: int = Field(default=5, ge=2, le=30, strict=True)
    resolution: Literal["480P", "720P", "1080P"] = "720P"
    ratio: Literal["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"] | None = None
    first_frame_node_id: str | None = Field(default=None, min_length=1, max_length=100)
    last_frame_node_id: str | None = Field(default=None, min_length=1, max_length=100)
    reference_video_node_id: str | None = Field(default=None, min_length=1, max_length=100)
    reference_image_node_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(default=None, max_length=MAX_REFERENCE_IMAGES)
    video_input_mode: Literal["text", "reference", "edit", "extend"] | None = None
    operation_id: str = Field(min_length=1, max_length=100)


class GenerationConfirm(WireModel):
    confirmation_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int = Field(ge=1, strict=True)


class GenerationAdopt(WireModel):
    expected_revision: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=100)


class GenerationResume(WireModel):
    pass


class GenerationRetryQuote(WireModel):
    expected_revision: int = Field(ge=1, strict=True)


class GenerationJoin(WireModel):
    scene_id: str = Field(min_length=1, max_length=100)
    source_node_id: str = Field(min_length=1, max_length=100)
    output_node_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=100)


class GenerationError(WireModel):
    code: str
    message: str


class GenerationDetail(WireModel):
    id: str
    project_id: str
    scene_id: str
    source_node_id: str
    output_node_id: str | None = None
    clip_id: str
    project_revision: int
    status: Literal["awaiting_confirmation", "queued", "submitting", "submission_unknown", "processing", "downloading", "ready", "failed", "query_interrupted"]
    input: dict[str, Any]
    confirmation_hash: str
    retry_of: str | None = None
    asset_id: str | None
    error: GenerationError | None
    retry_count: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


class GenerationView(GenerationDetail):
    quoted_cents: int
    reserved_cents: int
    actual_cents: int | None
    remote_task_id: str | None
    remote_request_id: str | None
    usage: dict[str, Any]


class MaterialGenerationView(GenerationDetail):
    material: Literal[True] = True
    can_resume: bool
    can_retry: bool = False


class GenerationResult(WireModel):
    generation: GenerationView | MaterialGenerationView


class GenerationList(WireModel):
    generations: list[GenerationView | MaterialGenerationView]


def visible_generation(session, generation, material_ids=None, artifacts=None):
    value = generation_view(generation)
    if material_ids is None:
        material_ids = material_generation_ids(session, generation.project_id, generation.owner_id)
    if not is_material_generation(value, material_ids):
        return value
    request = value["input"].get("request") or {}
    if artifacts is None and request.get("videoArtifactNodeId") and not request.get("shotSummary"):
        artifacts = material_video_artifacts(session, generation.project_id, generation.owner_id, generation.scene_id)
    return public_material_generation(value, artifacts)


def require_video_worker():
    settings = get_settings()
    if gateway_enabled(settings):
        raise AppError(503, "VIDEO_WORKER_UNAVAILABLE", "线上视频生成服务暂不可用")
    return settings


@router.post("/projects/{project_id}/generations/quote", response_model=GenerationResult, status_code=201)
def quote(project_id: str, body: GenerationQuote, user: User = Depends(current_user), session: Session = Depends(get_db)):
    generation = quote_generation(session, project_id, user.id, body.model_dump(by_alias=True), require_video_worker())
    return {"generation": visible_generation(session, generation)}


@router.post("/generations/{generation_id}/confirm", response_model=GenerationResult)
def confirm(generation_id: str, body: GenerationConfirm, user: User = Depends(current_user), session: Session = Depends(get_db)):
    generation = confirm_generation(session, generation_id, user.id, body.confirmation_hash, body.expected_revision, require_video_worker())
    return {"generation": visible_generation(session, generation)}


@router.post("/projects/{project_id}/generations/join", response_model=GenerationResult, status_code=201)
def join(project_id: str, body: GenerationJoin, user: User = Depends(current_user), session: Session = Depends(get_db)):
    generation = create_join_generation(session, project_id, user.id, body.model_dump(by_alias=True), require_video_worker())
    return {"generation": generation_view(generation)}


@router.get("/projects/{project_id}/generations", response_model=GenerationList)
def list_generations(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return {"generations": CloudBaseVideoRepository(settings).list(user.id, project_id)}
    require_project(session, project_id, user.id)
    generations = session.scalars(select(Generation).where(Generation.project_id == project_id, Generation.owner_id == user.id).order_by(Generation.created_at.desc())).all()
    material_ids = material_generation_ids(session, project_id, user.id)
    requests = [generation.input_json.get("request") or {} for generation in generations]
    artifacts = material_video_artifacts(session, project_id, user.id) if any(request.get("videoArtifactNodeId") and not request.get("shotSummary") for request in requests) else {}
    return {"generations": [visible_generation(session, generation, material_ids, artifacts) for generation in generations]}


@router.get("/generations/{generation_id}", response_model=GenerationResult)
def get_generation(generation_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return {"generation": CloudBaseVideoRepository(settings).read(user.id, generation_id)}
    return {"generation": visible_generation(session, require_generation(session, generation_id, user.id))}


@router.post("/generations/{generation_id}/resume", response_model=GenerationResult)
def resume(generation_id: str, body: GenerationResume, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_video_worker()
    return {"generation": visible_generation(session, resume_generation(session, generation_id, user.id))}


@router.post("/generations/{generation_id}/retry-quote", response_model=GenerationResult)
def retry_quote(generation_id: str, body: GenerationRetryQuote, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"generation": visible_generation(session, retry_generation_quote(session, generation_id, user.id,
        body.expected_revision, require_video_worker()))}


@router.post("/generations/{generation_id}/adopt", response_model=ProjectEnvelope, response_model_exclude_unset=True)
def adopt(generation_id: str, body: GenerationAdopt, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return public_project_envelope(session, user.id, adopt_generation(session, generation_id, user.id, body.expected_revision, body.operation_id, require_video_worker()))
