from typing import Literal

from pydantic import Field, field_validator

from app.agent_schemas import RoleId, RunStatus
from app.schemas import WireModel
from app.provider_schemas import ModelSelection


class MaterialCreate(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="素材创作", min_length=1, max_length=200)


class MaterialReference(WireModel):
    node_id: str = Field(min_length=1, max_length=100)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class MaterialSend(WireModel):
    model_selection: ModelSelection | None = None
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=12000)
    references: list[MaterialReference] = Field(default_factory=list, max_length=20)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=5)
    intent: Literal["followup", "start"] | None = None

    @field_validator("reference_asset_ids")
    @classmethod
    def valid_reference_assets(cls, value):
        if any(not item.strip() or len(item) > 100 for item in value) or len(set(value)) != len(value):
            raise ValueError("参考图片需要提供不重复的有效标识")
        return value

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("请填写素材需求")
        return value


class MaterialSummary(WireModel):
    id: str
    project_id: str
    scene_id: str
    title: str
    created_at: str
    updated_at: str


class DirectorBriefView(WireModel):
    objective: str
    confirmed_requirements: list[str]
    status_text: str | None = None


class MaterialRunView(WireModel):
    id: str
    project_id: str
    scene_id: str
    workspace: Literal["materials"] = "materials"
    role: RoleId
    executor: Literal["platform"] = "platform"
    goal: str
    status: RunStatus
    project_revision: int
    current_revision: int
    target_ids: list[str]
    can_resume: bool
    can_extend_limit: bool = False
    retryable_unknown_call_id: str | None
    limits: dict
    plan: list[dict]
    steps: list[dict]
    usage: dict
    error: dict | None


class MaterialMessageView(WireModel):
    id: str
    source: Literal["user", "automation"]
    content: str
    references: list[dict]
    run: MaterialRunView
    created_at: str
    director_brief: DirectorBriefView | None = None
    kind: Literal["production", "followup"] = "production"
    reply: str | None = None
    reference_assets: list[dict] = Field(default_factory=list)


class MaterialView(MaterialSummary):
    messages: list[MaterialMessageView]
    revision: int
    nodes: list[dict]
    capabilities: dict
    pending_review: dict | None = None
    selected_reference_assets: list[dict] = Field(default_factory=list)
    director_brief: DirectorBriefView | None = None
    active_run_id: str | None = None
    can_start_production: bool = True


class MaterialAnswer(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    review_id: str = Field(min_length=1, max_length=100)
    answers: dict[str, str] = Field(default_factory=dict, max_length=60)
    requested_changes: str = Field(default="", max_length=12000)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=5)
    account_usage_confirmed: Literal[True] | None = None

    @field_validator("reference_asset_ids")
    @classmethod
    def valid_reference_assets(cls, value):
        return MaterialSend.valid_reference_assets(value)


class MaterialEnvelope(WireModel):
    conversation: MaterialView


class MaterialList(WireModel):
    conversations: list[MaterialSummary]


def material_request_payload(body: MaterialSend | MaterialAnswer) -> dict:
    payload = body.model_dump(by_alias=True)
    if not body.reference_asset_ids:
        payload.pop("referenceAssetIds")
    if isinstance(body, MaterialSend) and body.intent is None:
        payload.pop("intent")
    if isinstance(body, MaterialSend) and body.model_selection is None:
        payload.pop("modelSelection")
    if isinstance(body, MaterialAnswer) and body.account_usage_confirmed is None:
        payload.pop("accountUsageConfirmed")
    return payload
