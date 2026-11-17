import time

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, new_id


class ProjectMemory(Base):
    __tablename__ = "project_memories"
    __table_args__ = (UniqueConstraint("project_id", "operation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    operation_id: Mapped[str] = mapped_column(String(100))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    scene_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    branch_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    story_time: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sources_json: Mapped[list] = mapped_column(JSON)
    project_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="active")
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("project_memories.id"), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
