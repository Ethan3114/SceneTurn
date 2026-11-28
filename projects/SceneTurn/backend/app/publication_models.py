import time

from sqlalchemy import Boolean, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, new_id


class Publication(Base):
    __tablename__ = "publications"

    slug: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfer_jobs.id"), unique=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    poster_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    trailer_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    published: Mapped[bool] = mapped_column(Boolean, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
