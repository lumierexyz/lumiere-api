"""Economic series cache backed by the database.

A series is refetched when older than ``ECONOMIC_DATA_TTL_HOURS``. When a refresh fails, previously
stored observations remain available and the response marks them stale; if nothing was ever stored the
category is reported as unavailable. Nothing is interpolated or invented.
"""

from datetime import UTC, date, datetime, timedelta

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import UpstreamError
from app.core.logging import logger
from app.economic_data.mapping import SeriesSpec
from app.economic_data.models import EconomicObservation, EconomicSeries, FxRate
from app.economic_data.providers import fetch_eurostat_hicp, fetch_fred


def _get_row(db: Session, spec: SeriesSpec) -> EconomicSeries | None:
    return db.scalar(
        select(EconomicSeries).where(
            EconomicSeries.source == spec.source,
            EconomicSeries.series_code == spec.code,
            EconomicSeries.geography == spec.geography,
        )
    )


def refresh_series(db: Session, spec: SeriesSpec, force: bool = False) -> EconomicSeries:
    """Fetch and store a series unless the stored copy is fresh.

    Several requests from one page can refresh the same series at the same moment (index, history,
    forecast and coverage load in parallel). If another request wins the race, its rows violate a
    unique constraint here: roll back and use what it stored."""
    try:
        return _refresh_series(db, spec, force)
    except IntegrityError:
        db.rollback()
        logger.info("series_refresh_race", extra={"provider": spec.source, "series_id": spec.code})
        row = _get_row(db, spec)
        if row is None:
            raise
        return row


def _refresh_series(db: Session, spec: SeriesSpec, force: bool) -> EconomicSeries:
    row = _get_row(db, spec)
    ttl = timedelta(hours=get_settings().economic_data_ttl_hours)
    now = datetime.now(UTC)
    if row and not force and row.status == "ok" and row.fetched_at:
        fetched = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=UTC)
        if now - fetched < ttl:
            return row
    if row is None:
        row = EconomicSeries(
            source=spec.source,
            series_code=spec.code,
            geography=spec.geography,
            category=spec.category,
            title=spec.note,
        )
        db.add(row)
        db.flush()
    try:
        fetched_series = (
            fetch_fred(spec.code) if spec.source == "fred" else fetch_eurostat_hicp(spec.code, spec.geography)
        )
    except UpstreamError as exc:
        row.status = "error"
        row.error = exc.message
        row.fetched_at = now
        db.commit()
        logger.warning("series_refresh_failed", extra={"provider": spec.source, "series_id": spec.code})
        return row
    if not fetched_series.observations:
        row.status, row.error, row.fetched_at = "error", "Provider returned no observations.", now
        db.commit()
        return row
    db.execute(delete(EconomicObservation).where(EconomicObservation.series_id == row.id))
    db.add_all(
        EconomicObservation(series_id=row.id, obs_date=d, value=v) for d, v in fetched_series.observations
    )
    row.title, row.unit, row.frequency = fetched_series.title, fetched_series.unit, fetched_series.frequency
    row.last_observation_date = fetched_series.observations[-1][0]
    row.status, row.error, row.fetched_at = "ok", None, now
    db.commit()
    return row


def monthly_index(db: Session, row: EconomicSeries | None) -> pd.Series:
    """Observations as a month-start indexed Series. Quarterly/annual data is forward-filled only
    within its own period (a Q1 value applies to Jan-Mar), never past the last observation."""
    if row is None:
        return pd.Series(dtype=float)
    obs = db.execute(
        select(EconomicObservation.obs_date, EconomicObservation.value)
        .where(EconomicObservation.series_id == row.id)
        .order_by(EconomicObservation.obs_date)
    ).all()
    if not obs:
        return pd.Series(dtype=float)
    s = pd.Series({pd.Timestamp(d).to_period("M").to_timestamp(): v for d, v in obs}).sort_index()
    if row.frequency.upper().startswith("Q"):
        s = s.reindex(pd.date_range(s.index[0], s.index[-1] + pd.offsets.MonthBegin(2), freq="MS")).ffill(
            limit=2
        )
    elif row.frequency.upper().startswith("A"):
        s = s.reindex(pd.date_range(s.index[0], s.index[-1] + pd.offsets.MonthBegin(11), freq="MS")).ffill(
            limit=11
        )
    return s.astype(float)


def series_status(row: EconomicSeries | None) -> dict:
    if row is None:
        return {"status": "unavailable"}
    stale = row.status != "ok"
    return {
        "source": row.source,
        "series_id": row.series_code,
        "geography": row.geography,
        "title": row.title,
        "unit": row.unit,
        "frequency": row.frequency,
        "last_observation": row.last_observation_date.isoformat() if row.last_observation_date else None,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        "status": "stale" if stale and row.last_observation_date else row.status,
        "error": row.error,
    }


# ---------------------------------------------------------------- FX (FRED H.10 daily rates)
# (FRED series, base, quote): 1 base = value quote
FX_SERIES = {
    "EUR": ("DEXUSEU", "EUR", "USD"),
    "GBP": ("DEXUSUK", "GBP", "USD"),
    "AUD": ("DEXUSAL", "AUD", "USD"),
    "CAD": ("DEXCAUS", "USD", "CAD"),
    "CHF": ("DEXSZUS", "USD", "CHF"),
    "JPY": ("DEXJPUS", "USD", "JPY"),
}


def refresh_fx(db: Session, currency: str) -> None:
    spec = FX_SERIES.get(currency)
    if spec is None:
        return
    code, base, quote = spec
    data = fetch_fred(code, start=(date.today() - timedelta(days=365 * 6)).isoformat())
    db.execute(delete(FxRate).where(FxRate.base == base, FxRate.quote == quote))
    db.add_all(
        FxRate(base=base, quote=quote, rate_date=d, rate=v, source="fred", series_code=code)
        for d, v in data.observations
    )
    db.commit()


def _usd_rate(db: Session, currency: str, on: date) -> tuple[float, date, str] | None:
    """Units of USD per 1 unit of ``currency`` on the latest trading day on or before ``on``
    (at most 10 days earlier)."""
    if currency == "USD":
        return 1.0, on, "identity"
    spec = FX_SERIES.get(currency)
    if spec is None:
        return None
    code, base, quote = spec
    row = db.execute(
        select(FxRate.rate, FxRate.rate_date)
        .where(
            FxRate.base == base,
            FxRate.quote == quote,
            FxRate.rate_date <= on,
            FxRate.rate_date >= on - timedelta(days=10),
        )
        .order_by(FxRate.rate_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    rate = row.rate if base == currency else 1.0 / row.rate
    return rate, row.rate_date, code


def conversion_rate(db: Session, src: str, dst: str, on: date) -> dict | None:
    """Explicit rate (1 src = rate dst) with its date and source, or None when unavailable."""
    if src == dst:
        return {"rate": 1.0, "rate_date": on.isoformat(), "source": "identity"}
    a, b = _usd_rate(db, src, on), _usd_rate(db, dst, on)
    if a is None or b is None:
        return None
    return {
        "rate": a[0] / b[0],
        "rate_date": min(a[1], b[1]).isoformat(),
        "source": "FRED H.10 " + "/".join(x for x in (a[2], b[2]) if x != "identity"),
    }
