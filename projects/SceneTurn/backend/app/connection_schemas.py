from typing import Any, Literal

from pydantic import Field

from app.schemas import WireModel


ConnectionTool = Literal["read_project", "retrieve_context", "write_script", "write_artifact", "read_artifact", "patch_artifact", "apply_canvas_ops", "submit_generation", "inspect_generation", "adopt_asset", "validate_scene", "write_memory", "split_image", "join_videos", "validate_artifacts", "read_story", "apply_story_ops", "edit_timeline", "edit_interaction", "prepare_scene_media", "validate_story", "arrange_story", "move_timeline_clip", "place_interaction", "undo_project_edit"]


class PairingCreate(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    tools: list[ConnectionTool] = Field(min_length=1, max_length=25)
    expires_in_seconds: int = Field(default=3600, ge=300, le=86400)


class PairingResult(WireModel):
    pairing_id: str
    pairing_code: str
    project_id: str
    tools: list[str]
    expires_at: str
    connection_expires_at: str


class PairingClaim(WireModel):
    pairing_code: str = Field(min_length=32, max_length=200)
    client_version: str = Field(min_length=1, max_length=100)


class ConnectionView(WireModel):
    id: str
    project_id: str
    tools: list[str]
    client_version: str
    status: Literal["online", "offline", "revoked", "expired"]
    expires_at: str
    last_seen_at: str
    created_at: str
    can_generate_images: bool = False


class ConnectionHeartbeat(WireModel):
    capabilities: list[Literal["native_image_v1"]] | None = Field(default=None, max_length=1)


class ConnectionList(WireModel):
    connections: list[ConnectionView]


class ConnectionClaimResult(WireModel):
    connection: ConnectionView
    token: str


class ConnectionRevoke(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)


class CodexCounters(WireModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    reasoning_output_tokens: int = Field(ge=0)
    cache_write_input_tokens: int = Field(default=0, ge=0)


class RawCodexUsage(WireModel):
    last: CodexCounters
    total: CodexCounters
    model_context_window: int | None = Field(default=None, ge=0)


class CodexUsage(CodexCounters):
    model: str = Field(min_length=1, max_length=100)
    thread_totals: CodexCounters
    baseline: CodexCounters | None = None
    raw_token_usage: RawCodexUsage


class BridgeEvent(WireModel):
    run_id: str
    event_type: Literal["thread_started", "turn_started", "usage", "disconnected", "failed", "interrupted"]
    thread_id: str | None = Field(default=None, max_length=200)
    turn_id: str | None = Field(default=None, max_length=200)
    usage: CodexUsage | None = None
    error_code: str | None = Field(default=None, max_length=100)


class BridgeToolCall(WireModel):
    run_id: str
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    tool_name: ConnectionTool
    arguments: dict[str, Any]


class BridgeComplete(WireModel):
    run_id: str
    thread_id: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=200)
    result: dict[str, Any]
    usage: CodexUsage | None = None


class BridgeResult(WireModel):
    result: dict[str, Any] | None = None


class HeartbeatResult(WireModel):
    connection: ConnectionView
    active_runs: list[dict[str, Any]]
