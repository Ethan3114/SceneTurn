from typing import Literal

from pydantic import Field, field_validator

from app.agent_schemas import AgentRunView
from app.schemas import WireModel


class ConversationCreate(WireModel):
    title: str = Field(default="项目对话", min_length=1, max_length=200)
    operation_id: str = Field(min_length=1, max_length=100)


class EditingContext(WireModel):
    scene_id: str | None = Field(default=None, max_length=100)
    selected_node_ids: list[str] = Field(default_factory=list, max_length=100)
    selected_clip_ids: list[str] = Field(default_factory=list, max_length=100)
    timeline_time: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)
    surface: Literal["editor", "timeline", "materials"] = "editor"


class ConversationSend(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=12000)
    context: EditingContext = Field(default_factory=EditingContext)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("消息不能为空")
        return value.strip()


class ConversationSummary(WireModel):
    id: str
    title: str
    project_id: str
    created_at: str
    updated_at: str


class MessageView(WireModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    context: dict
    run_id: str | None
    created_at: str


class ConversationView(ConversationSummary):
    messages: list[MessageView]
    run: AgentRunView | None


class ConversationEnvelope(WireModel):
    conversation: ConversationView


class ConversationList(WireModel):
    conversations: list[ConversationSummary]


class ConversationCapabilities(WireModel):
    tools: list[str]
    skills: list[str]
