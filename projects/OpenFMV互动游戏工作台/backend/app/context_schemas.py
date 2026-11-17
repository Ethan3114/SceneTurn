from typing import Any, Literal

from pydantic import Field, model_validator

from app.schemas import WireModel


MemoryKind = Literal["setting", "decision", "story_event", "creation_event", "candidate", "summary"]


class ContextSource(WireModel):
    kind: Literal["project", "scene", "canvas_node", "asset", "memory"]
    id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    scene_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def require_canvas_scene(self):
        if self.kind == "canvas_node" and not self.scene_id:
            raise ValueError("画布来源必须提供sceneId")
        return self


class MemoryWrite(WireModel):
    expected_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=100)
    kind: MemoryKind
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)
    scene_id: str | None = Field(default=None, max_length=100)
    branch_id: str | None = Field(default=None, max_length=100)
    story_time: str | None = Field(default=None, max_length=200)
    sources: list[ContextSource] = Field(min_length=1, max_length=50)
    supersedes_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def require_scope(self):
        if self.branch_id and not self.scene_id:
            raise ValueError("分支记忆必须指定所属场景")
        if self.kind == "story_event" and not self.scene_id:
            raise ValueError("剧情事件必须指定所属场景")
        if not self.content.strip():
            raise ValueError("记忆内容不能为空白")
        return self


class SourceView(ContextSource):
    digest: str
    current: bool
    reason: str | None = None


class MemoryView(WireModel):
    id: str
    schema_version: Literal[1] = 1
    kind: MemoryKind
    subject: str | None
    content: str
    scene_id: str | None
    branch_id: str | None
    story_time: str | None
    sources: list[SourceView]
    project_revision: int
    status: str
    valid: bool
    invalid_reason: str | None
    supersedes_id: str | None
    superseded_by: str | None
    created_at: str


class MemoryResult(WireModel):
    memory: MemoryView
    revision: int


class MemoryList(WireModel):
    memories: list[MemoryView]
    revision: int


class ContextRetrieve(WireModel):
    scene_id: str = Field(min_length=1, max_length=100)
    branch_id: str | None = Field(default=None, max_length=100)
    target_ids: list[str] = Field(default_factory=list, max_length=100)
    max_chars: int = Field(default=16000, ge=1024, le=64000)


class ContextResult(WireModel):
    project_id: str
    revision: int
    scene_id: str
    branch_id: str | None
    project: dict[str, Any]
    branch_analysis: dict[str, Any]
    memories: list[dict[str, Any]]
    story_facts: list[dict[str, Any]]
    conditional_memories: list[dict[str, Any]]
    creation_events: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    canvas_texts: list[dict[str, Any]]
    sources: list[ContextSource]
    max_chars: int
    character_count: int
    omitted_count: int
    truncated: bool


class ContextSourceResult(WireModel):
    source: SourceView
    content: Any
