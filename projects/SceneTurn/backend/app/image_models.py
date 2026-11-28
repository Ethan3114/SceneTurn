import time

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, new_id


class ImageAccount(Base):
    __tablename__ = "image_accounts"
    __table_args__ = (UniqueConstraint("owner_id", "slot"), UniqueConstraint("credential_hash"), UniqueConstraint("active_job_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    slot: Mapped[int] = mapped_column(Integer)
    credential_hash: Mapped[str] = mapped_column(String(64))
    api_key_encrypted: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="available")
    active_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_assigned_at: Mapped[float] = mapped_column(Float, default=0)


class ImageJob(Base):
    __tablename__ = "image_jobs"
    __table_args__ = (UniqueConstraint("owner_id", "operation_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    scene_id: Mapped[str] = mapped_column(String(100))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), index=True, nullable=True)
    node_id: Mapped[str] = mapped_column(String(100))
    artifact_hash: Mapped[str] = mapped_column(String(64))
    operation_id: Mapped[str] = mapped_column(String(160))
    label: Mapped[str] = mapped_column(String(500))
    frame_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="awaiting_confirmation", index=True)
    request_json: Mapped[dict] = mapped_column(JSON)
    reference_asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    dependency_ids: Mapped[list] = mapped_column(JSON, default=list)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("image_accounts.id"), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    response_relpath: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    confirmed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    submitted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
