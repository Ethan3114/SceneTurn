import time
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import Field, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import encode_secret
from app.models import TextGeneration, TextProviderSettings, TextUsageLedger, User
from app.schemas import WireModel
from app.services.cloudbase_database import gateway_enabled
from app.services.cloudbase_text import CloudBaseTextRepository
from app.services.projects import lock_write, require_project
from app.services.text_generation import apply_text_job, create_text_job, is_text_admin, require_text_job, resume_text_job, stop_text_job, text_view
from app.services.text_provider import MODEL, TaskKind, price_catalog


router = APIRouter(tags=["text-generations"])


class TextSettingsUpdate(WireModel):
    provider: Literal["deepseek"] = "deepseek"
    model: Literal["deepseek-flash"] = "deepseek-flash"
    api_key: SecretStr


class TextSettingsView(WireModel):
    configured: bool
    provider: str
    model: str
    prices: dict[str, Any]
    billing_policy_configured: bool
    can_manage: bool


class TextCreate(WireModel):
    model_profile_id: str | None = Field(default=None, min_length=1, max_length=100)
    scene_id: str = Field(min_length=1, max_length=100)
    source_node_id: str = Field(min_length=1, max_length=100)
    output_node_id: str = Field(min_length=1, max_length=100)
    prompt: str = Field(max_length=20000)
    task: TaskKind = "text"
    expected_revision: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=100)
    max_tokens: int | None = Field(default=None, ge=64, le=32768, strict=True)


class TextApply(WireModel):
    expected_revision: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=100)


class TextJobView(WireModel):
    id: str
    project_id: str
    scene_id: str
    source_node_id: str
    output_node_id: str
    task: TaskKind
    project_revision: int
    status: Literal["queued", "starting", "submitting", "running", "ready", "interrupted", "submission_unknown", "failed", "conflict"]
    input: dict[str, Any]
    result: dict[str, Any] | None
    output_text: str | None
    partial_text: str
    provider: str
    model: str
    remote_request_id: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    usage: dict[str, Any]
    cost_micros: int | None
    reserved_cost_micros: int
    billing_policy_configured: bool
    quality_issues: list[str]
    applied_revision: int | None
    stop_requested: bool
    error: dict[str, str] | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


class TextJobResult(WireModel):
    text_generation: TextJobView


class TextJobList(WireModel):
    text_generations: list[TextJobView]


class TextUsageEntry(WireModel):
    generation_id: str
    remote_request_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    cost_micros: int
    price: dict[str, Any]


class TextUsageView(WireModel):
    billing_policy_configured: bool
    currency: Literal["CNY"]
    cost_unit: Literal["microYuan"]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_micros: int
    entries: list[TextUsageEntry]


def settings_view(session: Session, user: User) -> dict:
    provider = session.get(TextProviderSettings, "platform")
    return {"configured": bool(provider and provider.api_key_encrypted), "provider": "deepseek", "model": MODEL, "prices": price_catalog(), "billingPolicyConfigured": False, "canManage": is_text_admin(user, get_settings())}


@router.get("/settings/text", response_model=TextSettingsView)
def read_settings(user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseTextRepository(settings).provider_settings(user.id)
    return settings_view(session, user)


@router.put("/settings/text", response_model=TextSettingsView)
def update_settings(body: TextSettingsUpdate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if not is_text_admin(user, settings):
        raise AppError(403, "TEXT_SETTINGS_FORBIDDEN", "只有平台管理员能够配置文本供应商")
    secret = body.api_key.get_secret_value().strip()
    if not 8 <= len(secret) <= 2048 or any(character.isspace() for character in secret):
        raise AppError(422, "TEXT_KEY_INVALID", "请填写完整的DeepSeek API Key")
    if gateway_enabled(settings):
        return CloudBaseTextRepository(settings).provider_settings(user.id, secret)
    lock_write(session)
    provider = session.get(TextProviderSettings, "platform")
    if provider is None:
        provider = TextProviderSettings(id="platform", admin_owner_id=user.id)
        session.add(provider)
    provider.admin_owner_id, provider.provider, provider.model = user.id, body.provider, body.model
    provider.api_key_encrypted, provider.updated_at = encode_secret(secret, settings), time.time()
    session.commit()
    return settings_view(session, user)


@router.post("/projects/{project_id}/text-generations", response_model=TextJobResult, status_code=201)
def create(project_id: str, body: TextCreate, tasks: BackgroundTasks, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    payload = body.model_dump(by_alias=True)
    if gateway_enabled(settings):
        repository = CloudBaseTextRepository(settings)
        view, marker = repository.accept(user, project_id, payload)
        if marker is not None:
            tasks.add_task(repository.run, marker, immediate=True)
        return {"textGeneration": view}
    job = create_text_job(session, project_id, user, payload, settings)
    return {"textGeneration": text_view(job)}


@router.get("/projects/{project_id}/text-generations", response_model=TextJobList)
def list_jobs(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return {"textGenerations": CloudBaseTextRepository(settings).list(user.id, project_id)}
    require_project(session, project_id, user.id)
    jobs = session.scalars(select(TextGeneration).where(TextGeneration.project_id == project_id, TextGeneration.owner_id == user.id).order_by(TextGeneration.created_at.desc())).all()
    return {"textGenerations": [text_view(job) for job in jobs]}


@router.get("/text-generations/{identifier}", response_model=TextJobResult)
def read_job(identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return {"textGeneration": CloudBaseTextRepository(settings).read(user.id, identifier)}
    return {"textGeneration": text_view(require_text_job(session, identifier, user.id))}


@router.post("/text-generations/{identifier}/stop", response_model=TextJobResult)
def stop(identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return {"textGeneration": CloudBaseTextRepository(settings).stop(user.id, identifier)}
    return {"textGeneration": text_view(stop_text_job(session, identifier, user.id))}


@router.post("/text-generations/{identifier}/resume", response_model=TextJobResult)
def resume(identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return {"textGeneration": CloudBaseTextRepository(settings).resume(user.id, identifier)}
    return {"textGeneration": text_view(resume_text_job(session, identifier, user.id))}


@router.post("/text-generations/{identifier}/apply", response_model=TextJobResult)
def apply(identifier: str, body: TextApply, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return {"textGeneration": CloudBaseTextRepository(settings).apply(user.id, identifier, body.expected_revision, body.operation_id)}
    return {"textGeneration": text_view(apply_text_job(session, identifier, user.id, body.expected_revision, body.operation_id))}


@router.get("/projects/{project_id}/text-usage", response_model=TextUsageView)
def usage(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseTextRepository(settings).usage(user.id, project_id)
    require_project(session, project_id, user.id)
    entries = session.scalars(select(TextUsageLedger).where(TextUsageLedger.project_id == project_id, TextUsageLedger.owner_id == user.id).order_by(TextUsageLedger.created_at)).all()
    return {"billingPolicyConfigured": False, "currency": "CNY", "costUnit": "microYuan", "inputTokens": sum(item.input_tokens for item in entries), "outputTokens": sum(item.output_tokens for item in entries), "totalTokens": sum(item.total_tokens for item in entries), "costMicros": sum(item.cost_micros for item in entries), "entries": [{"generationId": item.generation_id, "remoteRequestId": item.remote_request_id, "inputTokens": item.input_tokens, "outputTokens": item.output_tokens, "totalTokens": item.total_tokens, "cacheHitTokens": item.cache_hit_tokens, "cacheMissTokens": item.cache_miss_tokens, "costMicros": item.cost_micros, "price": item.price_json} for item in entries]}
