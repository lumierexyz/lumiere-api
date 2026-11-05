import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base
from app.database.types import IdMixin, JsonType, TimestampMixin, utcnow


class Category(StrEnum):
    HOUSING = "housing"
    GROCERIES = "groceries"
    RESTAURANTS = "restaurants"
    TRANSPORTATION = "transportation"
    ENERGY = "energy"
    HEALTHCARE = "healthcare"
    INSURANCE = "insurance"
    SUBSCRIPTIONS = "subscriptions"
    EDUCATION = "education"
    ENTERTAINMENT = "entertainment"
    OTHER = "other"


class Kind(StrEnum):
    """Economic nature of a row. Only EXPENSE (net of REFUND) enters spending analytics."""

    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    REFUND = "refund"


class CategorySource(StrEnum):
    IMPORT = "import"  # provided by the bank file
    RULE = "rule"  # deterministic keyword/merchant rule
    AI = "ai"  # LLM classification, validated by Pydantic
    USER = "user"  # manual correction, never overwritten automatically
    NONE = "none"


class ImportBatch(IdMixin, TimestampMixin, Base):
    __tablename__ = "import_batches"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(120))
    file_sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    # How the file was interpreted (column mapping, date order, decimal separator, sign convention).
    # The raw file itself is not retained.
    normalization: Mapped[dict] = mapped_column(JsonType, default=dict)


class Transaction(IdMixin, TimestampMixin, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_transactions_user_dedupe"),
        Index("ix_transactions_user_date", "user_id", "booked_on"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    import_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("import_batches.id", ondelete="SET NULL"), index=True
    )
    booked_on: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(140), default="")
    merchant: Mapped[str | None] = mapped_column(String(120))
    merchant_key: Mapped[str] = mapped_column(String(80), default="", index=True)
    # Signed, in the original currency: negative = money out, positive = money in.
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default=Kind.EXPENSE)
    category: Mapped[str] = mapped_column(String(32), default=Category.OTHER)
    category_source: Mapped[str] = mapped_column(String(16), default=CategorySource.NONE)
    category_confidence: Mapped[float | None] = mapped_column(Float)
    user_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
