import time

from sqlalchemy import JSON, Boolean, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, new_id


class CodexImageJob(Base):
    __tablename__ = "codex_image_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    connection_id: Mapped[str | None] = mapped_column(ForeignKey("codex_connections.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), index=True, default="awaiting_confirmation")
    input_json: Mapped[dict] = mapped_column(JSON)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    thread_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    item_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
