from datetime import date
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.auth.deps import CurrentProfile, DbSession
from app.core.errors import AppError, InsufficientDataError
from app.economic_data.mapping import resolve_geography
from app.economic_data.service import conversion_rate
from app.forecasting import engine
from app.forecasting.routes import ScenarioIn, to_scenarios
from app.onchain.service import onchain_reserves
from app.risk.coverage import calculate
from app.schemas.common import CURRENCY_PATTERN
from app.services import context

router = APIRouter(prefix="/coverage", tags=["coverage"])


class CoverageIn(BaseModel):
    reserves_source: Literal["declared", "onchain"] = Field(
        default="declared",
        description="'onchain': verified balances of the wallets linked to the account (Robinhood Chain)",
    )
    reserves: float | None = Field(
        default=None, gt=0, le=1e12, description="User-declared reserves (not verified)"
    )
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)
    scenarios: list[ScenarioIn] | None = Field(default=None, max_length=8)
    assumed_annual_inflation: float | None = Field(default=None, ge=-0.2, le=1.0)


def coverage_for_profile(
    db,
    profile,
    reserves: float,
    currency: str,
    scenarios: list[engine.Scenario],
    assumed_inflation: float | None = None,
    verified: bool = False,
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
            "verified": verified,
        },
        "base_currency": profile.base_currency,
        **result,
        "assumptions": {
            "inflation_sources": baseline.inflation_source,
            "months_of_history": baseline.months_of_history,
        },
        "limitations": [
            "Reserves are the on-chain balances of your linked wallets at current prices; prices move."
            if verified
            else "Reserves are user-declared and not verified against any account.",
            "Projected spending assumes your recent basket continues; lifestyle changes are not modelled.",
            "Income is not included: this measures how long reserves alone would last.",
            "Estimates, not guarantees.",
        ],
    }


@router.post("/calculate")
def calculate_coverage(body: CoverageIn, profile: CurrentProfile, db: DbSession) -> dict:
    if body.reserves_source == "onchain":
        context.require_history(context.user_context(db, profile))  # fail fast, before any RPC call
        onchain = onchain_reserves(db, profile)
        if not onchain["wallets"]:
            raise AppError("No wallet is linked to this account.", code="no_linked_wallet")
        if onchain["total"] is None or onchain["total"] <= 0:
            raise AppError(
                "Your linked wallets hold no priced assets on Robinhood Chain.", code="no_onchain_reserves"
            )
        out = coverage_for_profile(
            db,
            profile,
            onchain["total"],
            profile.base_currency,
            to_scenarios(body.scenarios),
            body.assumed_annual_inflation,
            verified=True,
        )
        out["reserves"]["onchain"] = {
            k: onchain[k] for k in ("chain", "wallets", "holdings", "unpriced", "as_of")
        }
        return out
    if body.reserves is None or body.currency is None:
        raise AppError(
            "Provide reserves and currency, or set reserves_source to 'onchain'.",
            code="validation_error",
            status_code=422,
        )
    return coverage_for_profile(
        db, profile, body.reserves, body.currency, to_scenarios(body.scenarios), body.assumed_annual_inflation
    )
