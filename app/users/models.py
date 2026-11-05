import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base
from app.database.types import IdMixin, JsonType, TimestampMixin, utcnow


class Profile(TimestampMixin, Base):
    """One row per Supabase auth user. ``id`` equals ``auth.users.id`` (the JWT ``sub``)."""

    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(120))
    base_currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    country: Mapped[str] = mapped_column(String(2), default="US", nullable=False)
    # Preferred interpretation of ambiguous dates such as 03/04/2026: "DMY", "MDY" or None (auto).
    date_order: Mapped[str | None] = mapped_column(String(3))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditLog(IdMixin, TimestampMixin, Base):
    """Sensitive-action trail. Holds no financial content; ``user_id`` is kept without a foreign key
    so that the record of an account deletion survives the deletion itself."""

    __tablename__ = "audit_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    meta: Mapped[dict] = mapped_column(JsonType, default=dict)
