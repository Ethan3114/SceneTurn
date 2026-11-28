from typing import Literal

from pydantic import Field

from app.schemas import WireModel


class CodexImageCreate(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    prompt: str = Field(min_length=1, max_length=30000)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=5)
    transparent_background: bool = False
    name: str = Field(default="Codex生成图片", min_length=1, max_length=150)


class CodexImageConfirm(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)
    connection_id: str = Field(min_length=1, max_length=100)
    account_usage_confirmed: Literal[True]


class CodexImageStop(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)


class CodexImageEvent(WireModel):
    event_type: Literal["heartbeat", "thread_started", "turn_requested", "turn_started", "image_started", "image_completed", "turn_completed", "disconnected", "failed", "interrupted"]
    thread_id: str | None = Field(default=None, min_length=1, max_length=200)
    turn_id: str | None = Field(default=None, min_length=1, max_length=200)
    item_id: str | None = Field(default=None, min_length=1, max_length=200)
    error_code: str | None = Field(default=None, max_length=100)
    input_images: list[dict] = Field(default_factory=list, max_length=5)
    usage: dict | None = None


class CodexImageResultMetadata(WireModel):
    thread_id: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=200)
    item_id: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    source: Literal["savedPath", "inline"]
