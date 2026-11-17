from pydantic import Field, field_validator

from app.schemas import WireModel


class MaterialCreate(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="素材创作", min_length=1, max_length=200)


class MaterialReference(WireModel):
    node_id: str = Field(min_length=1, max_length=100)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class MaterialSend(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=12000)
    references: list[MaterialReference] = Field(default_factory=list, max_length=20)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("请填写素材需求")
        return value.strip()


class MaterialSummary(WireModel):
    id: str
    project_id: str
    scene_id: str
    title: str
    created_at: str
    updated_at: str


class MaterialMessageView(WireModel):
    id: str
    content: str
    references: list[dict]
    run: dict
    created_at: str


class MaterialView(MaterialSummary):
    messages: list[MaterialMessageView]
    revision: int
    nodes: list[dict]
    capabilities: dict
    pending_review: dict | None = None


class MaterialAnswer(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    review_id: str = Field(min_length=1, max_length=100)
    answers: dict[str, str] = Field(default_factory=dict, max_length=60)
    requested_changes: str = Field(default="", max_length=12000)


class MaterialEnvelope(WireModel):
    conversation: MaterialView


class MaterialList(WireModel):
    conversations: list[MaterialSummary]
