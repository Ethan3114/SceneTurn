import time

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, new_id


class MediaSequence(Base):
    __tablename__ = "media_sequences"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    scene_id: Mapped[str] = mapped_column(String(100))
    input_json: Mapped[dict] = mapped_column(JSON)
    input_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class MediaAdoption(Base):
    __tablename__ = "media_adoptions"
    __table_args__ = (UniqueConstraint("owner_id", "project_id", "operation_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    scene_id: Mapped[str] = mapped_column(String(100))
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"))
    generation_id: Mapped[str | None] = mapped_column(ForeignKey("generations.id"), nullable=True)
    clip_id: Mapped[str] = mapped_column(String(100))
    operation_id: Mapped[str] = mapped_column(String(100))
    project_revision: Mapped[int] = mapped_column(Integer)
    target_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proposal_json: Mapped[dict] = mapped_column(JSON)
    confirmation_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), index=True)
    applied_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
