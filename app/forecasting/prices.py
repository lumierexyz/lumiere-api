"""Builds the per-category price panel used by the PEI and the forecasting engine."""

from dataclasses import dataclass

import pandas as pd
from sqlalchemy.orm import Session

from app.economic_data.mapping import HEADLINE, spec_for
from app.economic_data.service import monthly_index, refresh_series, series_status
from app.transactions.models import Category


@dataclass
class CategoryPrices:
    category: str
    series: pd.Series  # month-start index -> index level
    meta: dict  # source, series id, status, proxy/fallback flags


def price_panel(db: Session, country: str, refresh: bool = True) -> dict[str, CategoryPrices]:
    panel: dict[str, CategoryPrices] = {}
    for cat in [HEADLINE, *[c.value for c in Category]]:
        spec, fallback = spec_for(country, cat)
        if spec is None:
            panel[cat] = CategoryPrices(
                cat, pd.Series(dtype=float), {"status": "unsupported", "reason": fallback}
            )
            continue
        row = refresh_series(db, spec) if refresh else None
        meta = series_status(row) | {"mapping_note": spec.note, "proxy": spec.proxy, "fallback": fallback}
        panel[cat] = CategoryPrices(cat, monthly_index(db, row), meta)
    return panel


def annual_inflation(series: pd.Series, months: int = 12) -> float | None:
    """Trailing annualised log-change over ``months`` months ending at the last observation."""
    s = series.dropna()
    if len(s) < 7:
        return None
    window = min(months, len(s) - 1)
    import math

    change = math.log(s.iloc[-1] / s.iloc[-1 - window])
    return math.exp(change * 12 / window) - 1
