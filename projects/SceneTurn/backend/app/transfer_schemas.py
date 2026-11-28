from typing import Literal

from pydantic import Field, JsonValue

from app.schemas import CanvasNode, CanvasSnapshot, ExtensibleModel, WireModel


class ExportCreate(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1, strict=True)
    kind: Literal["backup", "game"]


class TransferConfirm(WireModel):
    confirmation_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class TransferIssue(WireModel):
    code: str
    message: str
    source_id: str | None = None


class TransferProject(WireModel):
    id: str
    title: str
    scene_count: int
    canvas_node_count: int
    asset_count: int


class SourceMapping(WireModel):
    kind: Literal["project", "scene", "node", "asset"]
    source_id: str
    target_id: str


class TransferView(WireModel):
    id: str
    kind: Literal["import", "backup", "game"]
    status: Literal["queued", "processing", "awaiting_confirmation", "blocked", "ready", "failed"]
    project_id: str | None
    source_revision: int | None
    source_format: str | None
    projects: list[TransferProject]
    mappings: list[SourceMapping]
    issues: list[TransferIssue]
    confirmation_hash: str | None
    download_url: str | None
    preview_url: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str


class TransferResult(WireModel):
    transfer: TransferView


class TransferList(WireModel):
    transfers: list[TransferView]


class SourceHistory(WireModel):
    source_format: str
    history: dict[str, JsonValue]


class ImportCanvasNode(CanvasNode):
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ImportCanvasSnapshot(CanvasSnapshot):
    nodes: list[ImportCanvasNode] = Field(default_factory=list, max_length=10000)


class CanvasSource(ExtensibleModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    nodes: list[ImportCanvasNode] = Field(max_length=10000)
    connections: list[dict] = Field(max_length=50000)
    viewport: dict


class CanvasFile(WireModel):
    storage_key: str = Field(alias="storageKey", min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=1000)
    mime_type: str = Field(alias="mimeType")
    bytes: int = Field(ge=1, strict=True)


class CanvasSourceItem(WireModel):
    project: CanvasSource
    files: list[CanvasFile] = Field(max_length=20000)


class CanvasArchive(WireModel):
    app: Literal["infinite-canvas"]
    version: Literal[3]
    exported_at: str
    projects: list[CanvasSourceItem] = Field(min_length=1, max_length=100)


class BackupAsset(WireModel):
    source_id: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=1000)
    name: str = Field(min_length=1, max_length=200)
    mime_type: str
    bytes: int = Field(ge=1, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


BACKUP_FORMAT = "sceneturn-workbench"
LEGACY_BACKUP_FORMAT = "openfmv-workbench"
BACKUP_SOURCE_FORMAT = "sceneturn-workbench-v1"


class BackupManifest(WireModel):
    format: Literal[BACKUP_FORMAT, LEGACY_BACKUP_FORMAT]
    version: Literal[1]
    source_revision: int = Field(ge=1, strict=True)
    assets: list[BackupAsset] = Field(max_length=20000)
