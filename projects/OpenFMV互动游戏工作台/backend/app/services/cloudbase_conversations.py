from typing import Literal

from pydantic import ValidationError, model_validator

from app.conversation_schemas import ConversationCreate, ConversationEnvelope, ConversationList, ConversationSend, ConversationSummary, MessageView
from app.core.config import Settings
from app.core.errors import AppError
from app.models import new_id
from app.schemas import WireModel
from app.services.agent import render_run_view
from app.services.cloudbase_database import call_cloudbase_rpc
from app.services.conversations import editing_run_limits
from app.services.projects import payload_digest


class CodexConversationRun(WireModel):
    id: str
    project_id: str
    scene_id: str
    branch_id: str | None
    controller_node_id: str | None
    role: Literal["director"]
    executor: Literal["codex"]
    goal: str
    status: str
    project_revision: int
    initial_revision: int
    target_ids_json: list[str]
    created_ids_json: list[str]
    limits_json: dict
    plan_json: list[dict]
    result_json: dict | None
    stop_requested: int
    thread_id: str | None
    turn_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None


class CodexConversationStep(WireModel):
    id: str
    index: int
    role: str
    title: str
    instruction: str
    status: str
    result_json: dict | None
    tool_results_json: list[dict]


class CodexConversationCall(WireModel):
    id: str
    step_id: str | None
    role: str
    provider: str
    model: str
    status: str
    remote_request_id: str | None
    response_json: dict | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_micros: int | None
    usage_json: dict
    created_at: float
    finished_at: float | None


class ConversationReservation(WireModel):
    reserved_micros: int


class ConversationDelivery(WireModel):
    summary: ConversationSummary
    messages: list[MessageView]
    revision: int
    run: CodexConversationRun | None
    steps: list[CodexConversationStep]
    calls: list[CodexConversationCall]
    reservations: list[ConversationReservation]

    @model_validator(mode="after")
    def check_run(self):
        if self.run is not None and (
            self.run.project_id != self.summary.project_id
            or not {"maxSteps", "maxTokens", "maxCostMicros"}.issubset(self.run.limits_json)
            or any(not 0 <= step.index < len(self.run.plan_json) for step in self.steps)
        ):
            raise ValueError("项目对话关联的任务记录无效")
        return self

    def public_envelope(self) -> ConversationEnvelope:
        rendered_run = None
        if self.run is not None:
            rendered_run = render_run_view(self.run, self.run.plan_json, self.steps, self.calls, self.reservations, self.revision)
        return ConversationEnvelope.model_validate({"conversation": {**self.summary.model_dump(),
            "messages": self.messages, "run": rendered_run}}, strict=True)


class CloudBaseConversations:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _call(self, name: str, **parameters):
        return call_cloudbase_rpc(self.settings, f"sceneturn_conversation_{name}", parameters)

    def _detail(self, name: str, **parameters) -> ConversationEnvelope:
        value = self._call(name, **parameters)
        try:
            return ConversationDelivery.model_validate(value, strict=True).public_envelope()
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "项目对话暂时无法读取") from None

    def require_project(self, owner_id: str, project_id: str) -> None:
        if self._call("require_project", p_owner_id=owner_id, p_project_id=project_id) is not True:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "项目对话暂时无法读取")

    def list(self, owner_id: str, project_id: str) -> ConversationList:
        value = self._call("list", p_owner_id=owner_id, p_project_id=project_id)
        try:
            return ConversationList.model_validate(value, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "项目对话暂时无法读取") from None

    def read(self, owner_id: str, project_id: str, conversation_id: str) -> ConversationEnvelope:
        return self._detail("read", p_owner_id=owner_id, p_project_id=project_id, p_conversation_id=conversation_id)

    def create(self, owner_id: str, project_id: str, body: ConversationCreate) -> ConversationEnvelope:
        return self._detail("create", p_owner_id=owner_id, p_project_id=project_id, p_conversation_id=new_id(),
            p_operation_id=body.operation_id, p_payload_hash=payload_digest(body.model_dump(by_alias=True)), p_title=body.title)

    def send(self, owner_id: str, project_id: str, conversation_id: str, body: ConversationSend) -> ConversationEnvelope:
        payload = {"conversationId": conversation_id, **body.model_dump(by_alias=True)}
        return self._detail("send", p_owner_id=owner_id, p_project_id=project_id, p_conversation_id=conversation_id,
            p_operation_id=body.operation_id, p_payload_hash=payload_digest(payload), p_expected_revision=body.expected_revision,
            p_content=body.content, p_limits=editing_run_limits(conversation_id, payload["context"]))
