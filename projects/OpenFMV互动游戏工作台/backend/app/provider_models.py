import time

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, new_id


class ModelProviderConnection(Base):
    __tablename__ = "model_provider_connections"
    __table_args__ = (UniqueConstraint("scope_key", "provider_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    scope_key: Mapped[str] = mapped_column(String(50), index=True)
    provider_id: Mapped[str] = mapped_column(String(50))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
