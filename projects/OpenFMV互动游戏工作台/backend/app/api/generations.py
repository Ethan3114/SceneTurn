from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.models import Generation, User
from app.schemas import ProjectEnvelope, WireModel
from app.services.generation import adopt_generation, confirm_generation, create_join_generation, generation_view, quote_generation, require_generation, resume_generation
from app.services.projects import require_project


router = APIRouter(tags=["generations"])


class GenerationQuote(WireModel):
    scene_id: str = Field(min_length=1, max_length=100)
    source_node_id: str = Field(min_length=1, max_length=100)
    output_node_id: str | None = Field(default=None, min_length=1, max_length=100)
    clip_id: str | None = Field(default=None, min_length=1, max_length=100)
    prompt: str = Field(max_length=20000)
    duration: int = Field(default=5, ge=2, le=30, strict=True)
    resolution: Literal["480P", "720P", "1080P"] = "720P"
    operation_id: str = Field(min_length=1, max_length=100)


class GenerationConfirm(WireModel):
    confirmation_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int = Field(ge=1, strict=True)


class GenerationAdopt(WireModel):
    expected_revision: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=100)


class GenerationResume(WireModel):
    pass


class GenerationJoin(WireModel):
    scene_id: str = Field(min_length=1, max_length=100)
    source_node_id: str = Field(min_length=1, max_length=100)
    output_node_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=100)


class GenerationError(WireModel):
    code: str
    message: str


class GenerationView(WireModel):
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
    quoted_cents: int
    reserved_cents: int
    actual_cents: int | None
    remote_task_id: str | None
    remote_request_id: str | None
    asset_id: str | None
    error: GenerationError | None
    usage: dict[str, Any]
    retry_count: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


class GenerationResult(WireModel):
    generation: GenerationView


class GenerationList(WireModel):
    generations: list[GenerationView]


@router.post("/projects/{project_id}/generations/quote", response_model=GenerationResult, status_code=201)
def quote(project_id: str, body: GenerationQuote, user: User = Depends(current_user), session: Session = Depends(get_db)):
    generation = quote_generation(session, project_id, user.id, body.model_dump(by_alias=True), get_settings())
    return {"generation": generation_view(generation)}


@router.post("/generations/{generation_id}/confirm", response_model=GenerationResult)
def confirm(generation_id: str, body: GenerationConfirm, user: User = Depends(current_user), session: Session = Depends(get_db)):
    generation = confirm_generation(session, generation_id, user.id, body.confirmation_hash, body.expected_revision, get_settings())
    return {"generation": generation_view(generation)}


@router.post("/projects/{project_id}/generations/join", response_model=GenerationResult, status_code=201)
def join(project_id: str, body: GenerationJoin, user: User = Depends(current_user), session: Session = Depends(get_db)):
    generation = create_join_generation(session, project_id, user.id, body.model_dump(by_alias=True), get_settings())
    return {"generation": generation_view(generation)}


@router.get("/projects/{project_id}/generations", response_model=GenerationList)
def list_generations(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    generations = session.scalars(select(Generation).where(Generation.project_id == project_id, Generation.owner_id == user.id).order_by(Generation.created_at.desc())).all()
    return {"generations": [generation_view(generation) for generation in generations]}


@router.get("/generations/{generation_id}", response_model=GenerationResult)
def get_generation(generation_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"generation": generation_view(require_generation(session, generation_id, user.id))}


@router.post("/generations/{generation_id}/resume", response_model=GenerationResult)
def resume(generation_id: str, body: GenerationResume, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"generation": generation_view(resume_generation(session, generation_id, user.id))}


@router.post("/generations/{generation_id}/adopt", response_model=ProjectEnvelope, response_model_exclude_unset=True)
def adopt(generation_id: str, body: GenerationAdopt, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return adopt_generation(session, generation_id, user.id, body.expected_revision, body.operation_id, get_settings())
