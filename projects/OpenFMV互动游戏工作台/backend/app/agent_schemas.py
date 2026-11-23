from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas import WireModel
from app.provider_schemas import ModelSelection


RoleId = Literal["director", "writer", "character_designer", "scene_designer", "storyboarder", "media_producer", "editor", "interaction_designer", "sound_designer"]
RunStatus = Literal["queued", "running", "waiting_confirmation", "waiting_connection", "conflict", "stopping", "stopped", "completed", "failed", "submission_unknown"]


class RunLimits(WireModel):
    max_steps: int = Field(default=12, ge=1, le=60)
    max_tokens: int = Field(default=32768, ge=64, le=32768)
    max_cost_micros: int = Field(default=10000000, ge=1, le=10000000)


class RunCreate(WireModel):
    model_profile_id: str | None = Field(default=None, min_length=1, max_length=100)
    model_selection: ModelSelection | None = None
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    scene_id: str = Field(min_length=1, max_length=100)
    workspace: Literal["project", "scene", "materials"] | None = None
    controller_node_id: str | None = Field(default=None, max_length=100)
    branch_id: str | None = Field(default=None, max_length=100)
    role: RoleId = "director"
    executor: Literal["platform", "codex"] = "platform"
    delivery_mode: Literal["professional", "text_edit", "tool_action"] = "professional"
    goal: str = Field(min_length=1, max_length=12000)
    target_ids: list[str] = Field(default_factory=list, max_length=100)
    limits: RunLimits = Field(default_factory=RunLimits)

    @field_validator("goal")
    @classmethod
    def nonblank_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("创作目标不能为空")
        return value.strip()

    @field_validator("target_ids")
    @classmethod
    def unique_targets(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 100 for item in value) or len(set(value)) != len(value):
            raise ValueError("授权目标标识必须有效且唯一")
        return value


class RunControl(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)


class RunResume(RunControl):
    expected_revision: int = Field(ge=1)


class RunRetryUnknown(RunResume):
    call_id: str = Field(min_length=1, max_length=100)
    confirmed: Literal[True]
    max_steps: int = Field(ge=1, le=60)
    current_round_only: bool = True


class RunLimitExtension(RunResume):
    confirmed: Literal[True]
    max_steps: int = Field(ge=1, le=60)


class ToolInvocation(WireModel):
    run_id: str = Field(min_length=1, max_length=100)
    step_id: str | None = None
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    arguments: dict


class PlanItem(WireModel):
    role: RoleId
    title: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=4000)
    kind: Literal["produce", "verify"] = "produce"
    depends_on: list[int] = Field(default_factory=list, max_length=8)


class ExecutionPlanItem(PlanItem):
    model_config = ConfigDict(strict=True)
    kind: Literal["produce", "verify"]
    depends_on: list[int] = Field(max_length=8)


class AgentDecision(WireModel):
    action: Literal["plan", "tool", "complete"]
    plan: list[PlanItem] = Field(default_factory=list, max_length=8)
    tool: str | None = Field(default=None, max_length=100)
    arguments: dict = Field(default_factory=dict)
    summary: str = Field(default="", max_length=12000)


class ExecutionDecision(AgentDecision):
    model_config = ConfigDict(strict=True)
    plan: list[ExecutionPlanItem] = Field(default_factory=list, max_length=8)


class PlanningDecision(ExecutionDecision):
    action: Literal["plan"]
    plan: list[ExecutionPlanItem] = Field(min_length=1, max_length=8)
    tool: None = None
    arguments: dict = Field(default_factory=dict, max_length=0)


class MaterialDirectorBrief(WireModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    objective: str = Field(min_length=1, max_length=400)
    confirmed_requirements: list[str] = Field(max_length=8)


class MaterialDirectorQuestion(WireModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    question: str = Field(min_length=1, max_length=240)
    suggested_answer: str = Field(max_length=240)


class MaterialPlanningDecision(ExecutionDecision):
    action: Literal["plan"]
    director_brief: MaterialDirectorBrief
    questions: list[MaterialDirectorQuestion] = Field(max_length=5)
    tool: None = None
    arguments: dict = Field(default_factory=dict, max_length=0)

    @model_validator(mode="after")
    def check_gate(self):
        if bool(self.questions) == bool(self.plan):
            raise ValueError("澄清问题和可执行计划必须分别提交")
        return self


class StepDecision(ExecutionDecision):
    action: Literal["tool", "complete"]
    plan: list[ExecutionPlanItem] = Field(default_factory=list, max_length=0)

    @model_validator(mode="after")
    def check_action_fields(self):
        if self.action == "tool" and not self.tool:
            raise ValueError("执行工具需要明确的工具名称")
        if self.action == "complete" and (self.tool is not None or self.arguments):
            raise ValueError("完成响应不能同时提交工具操作")
        return self


class ReportedCodexUsage(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    inputTokens: int | None = Field(default=None, ge=0)
    outputTokens: int | None = Field(default=None, ge=0)
    totalTokens: int | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, max_length=100)


class ErrorView(WireModel):
    code: str
    message: str


class ToolReceipt(WireModel):
    operation_id: str
    tool: str
    arguments: dict
    result: dict
    revision: int


class StepView(WireModel):
    id: str
    index: int
    role: RoleId
    title: str
    instruction: str
    kind: Literal["produce", "verify"] = "produce"
    depends_on: list[int] = Field(default_factory=list)
    status: str
    result: dict | None
    tool_results: list[ToolReceipt]


class CallView(WireModel):
    id: str
    step_id: str | None
    role: RoleId
    provider: str
    model: str
    status: str
    remote_request_id: str | None
    response: dict | None
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    cost_micros: int | None = Field(ge=0)
    usage: dict
    created_at: str
    finished_at: str | None


class RunUsage(WireModel):
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    cost_micros: int | None = Field(ge=0)
    known_cost_micros: int | None = Field(ge=0)
    reserved_cost_micros: int = Field(ge=0)
    billing_policy_configured: Literal[False] = False
    cost_source: Literal["provider_estimate", "personal_account_unknown"]


class AgentRunView(WireModel):
    id: str
    project_id: str
    scene_id: str
    workspace: Literal["project", "scene", "materials"] | None = None
    branch_id: str | None
    controller_node_id: str | None
    role: RoleId
    executor: Literal["platform", "codex"]
    delivery_mode: Literal["professional", "text_edit", "tool_action"] | None = None
    artifact_contract_version: str | None = None
    tool_catalog_version: Literal["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"] = "R1"
    goal: str
    status: RunStatus
    project_revision: int
    initial_revision: int
    current_revision: int
    target_ids: list[str]
    created_node_ids: list[str]
    limits: RunLimits
    plan: list[PlanItem]
    result: dict | None
    stop_requested: bool
    thread_id: str | None
    turn_id: str | None
    error: ErrorView | None
    can_resume: bool = False
    can_extend_limit: bool = False
    retryable_unknown_call_id: str | None = None
    steps: list[StepView]
    calls: list[CallView]
    usage: RunUsage
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


class RunEnvelope(WireModel):
    run: AgentRunView


class RunList(WireModel):
    runs: list[AgentRunView]


class RoleView(WireModel):
    id: RoleId
    title: str
    description: str
    tools: list[str]
    available: bool
    reason: str | None


class RoleList(WireModel):
    roles: list[RoleView]


class ToolResult(WireModel):
    result: dict
    revision: int
