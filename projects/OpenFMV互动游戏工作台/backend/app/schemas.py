from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from pydantic.alias_generators import to_camel


class WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ExtensibleModel(WireModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")


class Credentials(WireModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)


class UserView(WireModel):
    id: str
    email: str


class UserResult(WireModel):
    user: UserView


class OkResult(WireModel):
    ok: Literal[True] = True


class Position(ExtensibleModel):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class Clip(ExtensibleModel):
    id: str = Field(min_length=1, max_length=100)
    type: Literal["video", "image", "audio", "button"]
    start_time: float = Field(ge=0, allow_inf_nan=False)
    duration: float = Field(ge=0, allow_inf_nan=False)
    enabled: bool = True
    asset_id: str | None = None
    src: str | None = None
    source_start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source_duration: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class TimelineTrack(ExtensibleModel):
    id: str = Field(min_length=1, max_length=100)
    type: Literal["media", "interaction"]
    name: str
    clips: list[Clip] = Field(default_factory=list, max_length=10000)


class Timeline(ExtensibleModel):
    version: Literal[2] = 2
    duration: float = Field(ge=0, allow_inf_nan=False)
    tracks: list[TimelineTrack] = Field(max_length=1000)
    bookmarks: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)


class StoryNodeData(ExtensibleModel):
    type: Literal["start", "scene", "end"]
    title: str | None = None
    label: str | None = None
    body_text: str | None = None
    timeline: Timeline | None = None


class StoryNode(ExtensibleModel):
    id: str = Field(min_length=1, max_length=100)
    type: Literal["start", "scene", "end"]
    position: Position
    data: StoryNodeData

    @model_validator(mode="after")
    def check_kind(self):
        if self.type != self.data.type:
            raise ValueError("节点与节点数据的类型必须相同")
        return self


class StoryEdge(ExtensibleModel):
    id: str = Field(min_length=1, max_length=100)
    source: str
    target: str


class StoryGraph(ExtensibleModel):
    nodes: list[StoryNode] = Field(max_length=10000)
    edges: list[StoryEdge] = Field(max_length=50000)

    @model_validator(mode="after")
    def check_references(self):
        identifiers = {node.id for node in self.nodes}
        if len(identifiers) != len(self.nodes):
            raise ValueError("节点标识重复")
        if len({edge.id for edge in self.edges}) != len(self.edges):
            raise ValueError("连线标识重复")
        if any(edge.source not in identifiers or edge.target not in identifiers for edge in self.edges):
            raise ValueError("连线引用了不存在的节点")
        return self


class ProjectAsset(ExtensibleModel):
    id: str
    type: Literal["image", "video", "audio", "text"]
    name: str
    path: str
    relative_path: str
    imported_at: str
    metadata: dict[str, Any] | None = None


class ProjectSnapshot(ExtensibleModel):
    schema_version: Literal[1] = 1
    id: str
    title: str = Field(min_length=1, max_length=200)
    graph_data: StoryGraph
    assets: list[ProjectAsset] = Field(default_factory=list, max_length=20000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class Viewport(WireModel):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    k: float = Field(gt=0, le=20, allow_inf_nan=False)


class CanvasNode(ExtensibleModel):
    id: str = Field(min_length=1, max_length=100)
    type: str = Field(min_length=1, max_length=100)
    position: Position


class CanvasConnection(ExtensibleModel):
    id: str = Field(min_length=1, max_length=100)
    from_node_id: str
    to_node_id: str


def validate_canvas_references(nodes: list[CanvasNode], connections: list[CanvasConnection]) -> None:
    identifiers = {node.id for node in nodes}
    if len(identifiers) != len(nodes):
        raise ValueError("画布节点标识重复")
    if len({connection.id for connection in connections}) != len(connections):
        raise ValueError("画布连线标识重复")
    if any(connection.from_node_id not in identifiers or connection.to_node_id not in identifiers for connection in connections):
        raise ValueError("画布连线引用了不存在的节点")


class CanvasSnapshot(WireModel):
    scene_id: str
    nodes: list[CanvasNode] = Field(default_factory=list, max_length=10000)
    connections: list[CanvasConnection] = Field(default_factory=list, max_length=50000)
    viewport: Viewport = Field(default_factory=lambda: Viewport(x=0, y=0, k=1))

    @model_validator(mode="after")
    def check_references(self):
        validate_canvas_references(self.nodes, self.connections)
        return self


class ProjectEnvelope(WireModel):
    project: ProjectSnapshot
    revision: int
    canvases: list[CanvasSnapshot]


class ProjectListItem(WireModel):
    id: str
    title: str
    revision: int
    created_at: str
    updated_at: str
    archived_at: str | None = None


class ProjectList(WireModel):
    projects: list[ProjectListItem]


class ProjectCreate(WireModel):
    title: str = Field(min_length=1, max_length=200)


class ProjectSave(WireModel):
    expected_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=100)
    project: ProjectSnapshot


class SceneCreate(ProjectCreate):
    expected_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=100)


class CanvasSave(WireModel):
    expected_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=100)
    nodes: list[CanvasNode] = Field(max_length=10000)
    connections: list[CanvasConnection] = Field(max_length=50000)
    viewport: Viewport

    @model_validator(mode="after")
    def check_references(self):
        validate_canvas_references(self.nodes, self.connections)
        return self


class CanvasResult(WireModel):
    canvas: CanvasSnapshot
    revision: int


class WanSettingsInput(WireModel):
    workspace_id: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
    api_key: str = Field(min_length=10, max_length=1024)
    region: Literal["cn-beijing"] = "cn-beijing"
    model: Literal["wan3.0-video"] = "wan3.0-video"


class WanSettingsView(WireModel):
    configured: bool
    workspace_id: str | None
    region: str
    model: str
    prices: dict[str, int]


class PaymentCapabilities(WireModel):
    configured: Literal[False] = False
    provider: None = None


class PaymentOrder(WireModel):
    amount_cents: int = Field(gt=0)
    operation_id: str = Field(min_length=1, max_length=100)


class PaymentNotification(WireModel):
    provider: str = Field(min_length=1, max_length=100)
    event_id: str = Field(min_length=1, max_length=200)
    order_id: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any]


class PaymentRefund(PaymentOrder):
    order_id: str = Field(min_length=1, max_length=100)
