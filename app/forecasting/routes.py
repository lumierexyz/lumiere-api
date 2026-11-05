import uuid
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from app.auth.deps import CurrentProfile, DbSession
from app.core.errors import NotFoundError
from app.database.results import Forecast
from app.forecasting import engine
from app.services import context
from app.services.audit import audit
from app.transactions.models import Category

router = APIRouter(tags=["pei & forecasts"])


@router.get("/pei")
def get_pei(profile: CurrentProfile, db: DbSession) -> dict:
    return context.pei_summary(db, profile)


@router.get("/pei/history")
def get_pei_history(profile: CurrentProfile, db: DbSession) -> dict:
    result, _, _ = context.pei_for(db, profile)
    h = result.history
    return {
        "base_month": result.base_month.strftime("%Y-%m"),
        "flags": result.flags,
        "items": [
            {
                "month": i.strftime("%Y-%m"),
                "pei": round(float(r.pei), 3),
                "headline": round(float(r.headline), 3) if r.headline == r.headline else None,
                "basket_coverage": r.coverage,
            }
            for i, r in h.iterrows()
        ],
    }


@router.get("/pei/composition")
def get_pei_composition(profile: CurrentProfile, db: DbSession) -> dict:
    return context.pei_composition(db, profile)


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    inflation_shift_pp: float = Field(
        default=0.0,
        ge=-20,
        le=50,
        description="Added to every category's annual inflation, in percentage points",
    )
    shocks: dict[Category, float] = Field(
        default_factory=dict, description="One-off price level change by category (0.2 = +20%)"
    )

    @field_validator("shocks")
    @classmethod
    def _bounds(cls, v: dict) -> dict:
        for k, x in v.items():
            if not -0.9 <= x <= 5:
                raise ValueError(f"shock for {k} must be between -0.9 and 5")
        return v


class ForecastIn(BaseModel):
    horizon_months: Literal[3, 6, 12, 24] = 12
    scenarios: list[ScenarioIn] | None = Field(
        default=None,
        max_length=8,
        description="Defaults to baseline, lower (-1.5 pp) and higher (+3 pp) inflation scenarios",
    )
    assumed_annual_inflation: float | None = Field(
        default=None, ge=-0.2, le=1.0, description="Used only for categories with no available price index"
    )


def to_scenarios(items: list[ScenarioIn] | None) -> list[engine.Scenario]:
    if not items:
        return list(engine.DEFAULT_SCENARIOS)
    return [
        engine.Scenario(s.name, s.inflation_shift_pp, {k.value: v for k, v in s.shocks.items()})
        for s in items
    ]


@router.post("/forecasts", status_code=201)
def create_forecast(body: ForecastIn, profile: CurrentProfile, db: DbSession) -> dict:
    result = context.forecast(
        db, profile, body.horizon_months, to_scenarios(body.scenarios), body.assumed_annual_inflation
    )
    row = Forecast(user_id=profile.id, request=body.model_dump(mode="json"), result=result)
    db.add(row)
    audit(db, profile.id, "forecast.created", forecast_id=str(row.id))
    db.commit()
    return {"id": str(row.id), "created_at": row.created_at.isoformat(), **result}


@router.get("/forecasts/{forecast_id}")
def get_forecast(forecast_id: uuid.UUID, profile: CurrentProfile, db: DbSession) -> dict:
    row = db.get(Forecast, forecast_id)
    if row is None or row.user_id != profile.id:
        raise NotFoundError("Forecast not found.")
    return {"id": str(row.id), "created_at": row.created_at.isoformat(), "request": row.request, **row.result}
