import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base
from app.database.types import IdMixin, utcnow


class EconomicSeries(IdMixin, Base):
    """Metadata for one official price series. Shared reference data (not user-owned)."""

    __tablename__ = "economic_series"
    __table_args__ = (
        UniqueConstraint("source", "series_code", "geography", name="uq_series_source_code_geo"),
    )

    source: Mapped[str] = mapped_column(String(16))  # "fred" | "eurostat"
    series_code: Mapped[str] = mapped_column(String(64))
    geography: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32))  # Lumiere category or "headline"
    title: Mapped[str] = mapped_column(String(255), default="")
    unit: Mapped[str] = mapped_column(String(64), default="")
    frequency: Mapped[str] = mapped_column(String(8), default="M")
    last_observation_date: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # ok | error | pending
    error: Mapped[str | None] = mapped_column(Text)


class EconomicObservation(Base):
    __tablename__ = "economic_observations"
    __table_args__ = (Index("ix_obs_series_date", "series_id", "obs_date"),)

    series_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("economic_series.id", ondelete="CASCADE"), primary_key=True
    )
    obs_date: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FxRate(Base):
    """Daily reference rate: 1 ``base`` = ``rate`` ``quote``. Source and series are recorded so every
    conversion can cite the exact rate and date it used."""

    __tablename__ = "fx_rates"

    base: Mapped[str] = mapped_column(String(3), primary_key=True)
    quote: Mapped[str] = mapped_column(String(3), primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, primary_key=True)
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="fred")
    series_code: Mapped[str] = mapped_column(String(32), default="")
