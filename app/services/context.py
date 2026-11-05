"""Shared, authorization-bound computations used by REST routes and by the AI agent tools.

Every function takes the *profile* resolved from the verified token; none accepts a user id from a
request body or from the model.
"""

from dataclasses import dataclass

import pandas as pd
from sqlalchemy.orm import Session

from app.core.errors import InsufficientDataError
from app.economic_data.mapping import HEADLINE, resolve_geography
from app.expenses.analytics import category_month_matrix
from app.expenses.ledger import Ledger, load_ledger
from app.forecasting import engine
from app.forecasting.pei import METHODOLOGY, PeiResult, compute_pei, yoy
from app.forecasting.prices import CategoryPrices, annual_inflation, price_panel
from app.users.models import Profile


@dataclass
class UserContext:
    profile: Profile
    ledger: Ledger
    matrix: pd.DataFrame
    partial_months: list[str]


def user_context(db: Session, profile: Profile) -> UserContext:
    ledger = load_ledger(db, profile.id, profile.base_currency)
    matrix, partial = category_month_matrix(ledger)
    return UserContext(profile, ledger, matrix, partial)


def require_history(ctx: UserContext) -> None:
    if ctx.matrix.empty:
        raise InsufficientDataError("No expense history yet. Import a bank statement first.")


def require_geography(profile: Profile) -> None:
    if resolve_geography(profile.country) is None:
        raise InsufficientDataError(
            f"No supported official price indices for country '{profile.country}'. Supported: US (FRED) and "
            "EU/EEA countries plus Switzerland (Eurostat HICP).",
            code="unsupported_geography",
        )


def panel_for(db: Session, profile: Profile) -> dict[str, CategoryPrices]:
    require_geography(profile)
    return price_panel(db, profile.country)


def data_sources(panel: dict[str, CategoryPrices], categories: list[str] | None = None) -> list[dict]:
    out = []
    for c, p in panel.items():
        if categories is not None and c not in categories and c != HEADLINE:
            continue
        out.append({"category": c, **{k: v for k, v in p.meta.items() if v not in (None, "")}})
    return out


def pei_for(
    db: Session, profile: Profile, ctx: UserContext | None = None
) -> tuple[PeiResult, dict, UserContext]:
    ctx = ctx or user_context(db, profile)
    require_history(ctx)
    panel = panel_for(db, profile)
    try:
        result = compute_pei(ctx.matrix, panel)
    except ValueError as exc:
        raise InsufficientDataError(str(exc)) from exc
    if result.history.empty:
        raise InsufficientDataError(
            "Price data does not yet cover your spending period with enough "
            "basket coverage to publish an index value.",
            details={"sources": data_sources(panel)},
        )
    return result, panel, ctx


def pei_summary(db: Session, profile: Profile) -> dict:
    result, panel, ctx = pei_for(db, profile)
    h = result.history
    last = h.index[-1]
    pei_yoy, head_yoy = yoy(h["pei"]), yoy(h["headline"])
    return {
        "value": round(float(h["pei"].iloc[-1]), 2),
        "as_of_month": last.strftime("%Y-%m"),
        "base_month": result.base_month.strftime("%Y-%m"),
        "base_value": 100.0,
        "change_since_base": round(float(h["pei"].iloc[-1] / 100 - 1), 4),
        "headline_change_since_base": (
            round(float(h["headline"].iloc[-1] / 100 - 1), 4) if pd.notna(h["headline"].iloc[-1]) else None
        ),
        "year_over_year": round(pei_yoy, 4) if pei_yoy is not None else None,
        "headline_year_over_year": round(head_yoy, 4) if head_yoy is not None else None,
        "gap_vs_headline_yoy_pp": (
            round((pei_yoy - head_yoy) * 100, 2) if pei_yoy is not None and head_yoy is not None else None
        ),
        "basket_coverage": float(h["coverage"].iloc[-1]),
        "flags": result.flags,
        "methodology": METHODOLOGY,
        "sources": data_sources(panel, list(ctx.matrix.columns)),
        "disclaimer": "Estimate of how your historical spending basket's cost evolves at official category "
        "prices. Not a measurement of your actual prices.",
    }


def pei_composition(db: Session, profile: Profile) -> dict:
    result, panel, ctx = pei_for(db, profile)
    year = max(result.weights_by_year) if result.weights_by_year else None
    weights = result.weights_by_year.get(year, {}) if year else {}
    rows = []
    for c, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        p = panel.get(c)
        pi = annual_inflation(p.series) if p is not None and not p.series.empty else None
        rows.append(
            {
                "category": c,
                "weight": round(w, 4),
                "annual_inflation": round(pi, 4) if pi is not None else None,
                "contribution_pp": round(w * pi * 100, 3) if pi is not None else None,
                "index": {
                    k: v
                    for k, v in (p.meta if p else {}).items()
                    if k
                    in (
                        "source",
                        "series_id",
                        "title",
                        "geography",
                        "proxy",
                        "fallback",
                        "last_observation",
                        "status",
                        "mapping_note",
                    )
                },
            }
        )
    return {
        "weights_year": year,
        "weights_basis": f"Net spending shares during {year - 1}" if year else None,
        "components": rows,
        "flags": result.flags,
        "methodology": METHODOLOGY,
    }


def forecast(
    db: Session,
    profile: Profile,
    horizon: int,
    scenarios: list[engine.Scenario],
    assumed_inflation: float | None = None,
) -> dict:
    ctx = user_context(db, profile)
    require_history(ctx)
    panel = panel_for(db, profile) if resolve_geography(profile.country) else {}
    try:
        base = engine.build_baseline(ctx.matrix, panel, assumed_inflation)
    except ValueError as exc:
        raise InsufficientDataError(str(exc)) from exc
    directly_priced = [
        c
        for c in base.monthly_by_category
        if panel.get(c) and not panel[c].series.empty and not panel[c].meta.get("fallback")
    ]
    return {
        "horizon_months": horizon,
        "base_currency": profile.base_currency,
        "baseline_monthly_by_category": {c: round(v, 2) for c, v in base.monthly_by_category.items()},
        "scenarios": [engine.project(base, horizon, s) for s in scenarios],
        "data_quality": {
            "months_of_history": base.months_of_history,
            "partial_months_excluded": ctx.partial_months,
            "categories_with_direct_index": directly_priced,
            "inflation_sources": base.inflation_source,
            "notes": base.notes,
            "unconverted_currencies": list(ctx.ledger.unconverted.values()),
        },
        "methodology": engine.__doc__.strip(),
        "disclaimer": "Conditional projections under stated assumptions, not predictions of future prices.",
    }
