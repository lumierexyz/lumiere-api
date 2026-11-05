from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.auth.deps import CurrentProfile, DbSession
from app.core.errors import AppError, InsufficientDataError
from app.economic_data.mapping import resolve_geography
from app.economic_data.service import conversion_rate
from app.forecasting import engine
from app.forecasting.routes import ScenarioIn, to_scenarios
from app.risk.coverage import calculate
from app.schemas.common import CURRENCY_PATTERN
from app.services import context

router = APIRouter(prefix="/coverage", tags=["coverage"])


class CoverageIn(BaseModel):
    reserves: float = Field(gt=0, le=1e12, description="User-declared available reserves (not verified)")
    currency: str = Field(pattern=CURRENCY_PATTERN)
    scenarios: list[ScenarioIn] | None = Field(default=None, max_length=8)
    assumed_annual_inflation: float | None = Field(default=None, ge=-0.2, le=1.0)


def coverage_for_profile(
    db,
    profile,
    reserves: float,
    currency: str,
    scenarios: list[engine.Scenario],
    assumed_inflation: float | None = None,
) -> dict:
    ctx = context.user_context(db, profile)
    context.require_history(ctx)
    conversion = None
    amount = reserves
    if currency != profile.base_currency:
        conversion = conversion_rate(db, currency, profile.base_currency, date.today())
        if conversion is None:
            raise AppError(
                f"No reference rate available to convert {currency} to {profile.base_currency}. "
                "Load rates with POST /api/v1/economic-data/fx/refresh or declare reserves in "
                f"{profile.base_currency}.",
                code="conversion_unavailable",
            )
        amount = reserves * conversion["rate"]
    panel = context.panel_for(db, profile) if resolve_geography(profile.country) else {}
    try:
        baseline = engine.build_baseline(ctx.matrix, panel, assumed_inflation)
    except ValueError as exc:
        raise InsufficientDataError(str(exc)) from exc
    result = calculate(baseline, amount, scenarios)
    return {
        "reserves": {
            "declared_amount": reserves,
            "declared_currency": currency,
            "amount_in_base_currency": round(amount, 2),
            "conversion": conversion,
            "verified": False,
        },
        "base_currency": profile.base_currency,
        **result,
        "assumptions": {
            "inflation_sources": baseline.inflation_source,
            "months_of_history": baseline.months_of_history,
        },
        "limitations": [
            "Reserves are user-declared and not verified against any account.",
            "Projected spending assumes your recent basket continues; lifestyle changes are not modelled.",
            "Income is not included: this measures how long reserves alone would last.",
            "Estimates, not guarantees.",
        ],
    }


@router.post("/calculate")
def calculate_coverage(body: CoverageIn, profile: CurrentProfile, db: DbSession) -> dict:
    return coverage_for_profile(
        db, profile, body.reserves, body.currency, to_scenarios(body.scenarios), body.assumed_annual_inflation
    )
