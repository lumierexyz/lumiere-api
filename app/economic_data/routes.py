from fastapi import APIRouter

from app.auth.deps import CurrentProfile, DbSession
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.economic_data.mapping import HEADLINE, resolve_geography, spec_for
from app.economic_data.service import FX_SERIES, refresh_fx, refresh_series, series_status
from app.services.audit import audit
from app.transactions.models import Category

router = APIRouter(prefix="/economic-data", tags=["economic data"])


@router.get("/series")
def list_series(profile: CurrentProfile, db: DbSession, refresh: bool = False) -> dict:
    """Price series mapped to each spending category for the caller's country, with freshness."""
    geo = resolve_geography(profile.country)
    if geo is None:
        return {"country": profile.country, "supported": False, "series": []}
    if refresh:
        limiter.check(f"econ-refresh:{profile.id}", 5, 3600)
    out = []
    for cat in [HEADLINE, *[c.value for c in Category]]:
        spec, fallback = spec_for(profile.country, cat)
        row = refresh_series(db, spec, force=refresh) if spec else None
        out.append(
            {
                "category": cat,
                "mapping_note": spec.note if spec else None,
                "proxy": spec.proxy if spec else None,
                "fallback": fallback,
                **series_status(row),
            }
        )
    return {
        "country": profile.country,
        "supported": True,
        "source": geo[0],
        "series": out,
        "ttl_hours": get_settings().economic_data_ttl_hours,
    }


@router.post("/fx/refresh")
def refresh_fx_rates(profile: CurrentProfile, db: DbSession) -> dict:
    """Load FRED H.10 reference rates needed to convert the caller's currencies."""
    limiter.check(f"fx-refresh:{profile.id}", 5, 3600)
    from sqlalchemy import distinct, select

    from app.transactions.models import Transaction

    currencies = set(
        db.scalars(select(distinct(Transaction.currency)).where(Transaction.user_id == profile.id))
    )
    currencies.add(profile.base_currency)
    refreshed, unsupported = [], []
    for c in sorted(currencies - {"USD"}):
        if c in FX_SERIES:
            refresh_fx(db, c)
            refreshed.append(c)
        else:
            unsupported.append(c)
    audit(db, profile.id, "fx.refreshed", count=len(refreshed))
    db.commit()
    return {"refreshed": refreshed, "unsupported": unsupported, "source": "FRED H.10 daily reference rates"}
