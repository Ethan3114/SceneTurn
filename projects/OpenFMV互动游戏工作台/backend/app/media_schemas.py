from typing import Literal

from pydantic import Field, model_validator

from app.schemas import ProjectEnvelope, WireModel


class MediaOperation(WireModel):
    expected_revision: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=100)


class SequenceSegment(WireModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=20000)
    duration: int = Field(ge=2, le=30, strict=True)
    resolution: Literal["480P", "720P", "1080P"] = "720P"
    clip_id: str | None = Field(default=None, min_length=1, max_length=100)


class SequenceCreate(MediaOperation):
    title: str = Field(min_length=1, max_length=200)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    frame_asset_ids: list[str] = Field(min_length=2, max_length=101)
    segments: list[SequenceSegment] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def continuous_frames(self):
        if len(self.frame_asset_ids) != len(self.segments) + 1:
            raise ValueError("n个视频片段必须选择n+1张连续首尾图片")
        return self


class AdoptionCheck(MediaOperation):
    scene_id: str = Field(min_length=1, max_length=100)
    asset_id: str = Field(min_length=1, max_length=100)
    generation_id: str | None = Field(default=None, min_length=1, max_length=100)
    clip_id: str | None = Field(default=None, min_length=1, max_length=100)
    mode: Literal["replace", "append"] = "replace"


class AdoptionConfirm(MediaOperation):
    confirmation_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AdoptionKeep(WireModel):
    operation_id: str = Field(min_length=1, max_length=100)


class FrameView(WireModel):
    asset_id: str
    name: str
    sha256: str
    width: int
    height: int


class SegmentView(WireModel):
    key: str
    title: str
    duration: int
    resolution: str
    clip_id: str
    first_frame: FrameView
    last_frame: FrameView
    generation_ids: list[str]


class SequenceView(WireModel):
    id: str
    project_id: str
    scene_id: str
    title: str
    frames: list[FrameView]
    segments: list[SegmentView]
    created_at: str


class AdoptionView(WireModel):
    id: str
    project_id: str
    scene_id: str
    asset_id: str
    generation_id: str | None
    clip_id: str
    project_revision: int
    confirmation_hash: str
    status: str
    target_name: str
    available_seconds: float
    required_seconds: float
    source_start: float
    message: str
    applied_revision: int | None


class AdoptionEnvelope(WireModel):
    adoption: AdoptionView


class AdoptionApplied(AdoptionEnvelope):
    project: ProjectEnvelope


class SequenceEnvelope(WireModel):
    sequence: SequenceView
    project: ProjectEnvelope


class CandidateView(WireModel):
    id: str
    asset_id: str | None
    generation_id: str | None
    clip_id: str
    title: str
    status: str
    adopted: bool
    duration_seconds: float | None
    resolution: str | None
    confirmation_hash: str | None
    project_revision: int | None
    frames: list[dict]


class CandidateList(WireModel):
    project_id: str
    scene_id: str
    revision: int
    sequences: list[SequenceView]
    candidates: list[CandidateView]
    adoptions: list[AdoptionView]
    clips: list[dict]
    images: list[FrameView]
    publish_allowed: bool
