import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.auth.deps import CurrentProfile, DbSession
from app.core.errors import InsufficientDataError, NotFoundError
from app.database.results import PortfolioSimulation
from app.portfolio.simulation import SimulationParams, simulate
from app.services import context
from app.services.audit import audit

router = APIRouter(prefix="/portfolio", tags=["portfolio research (simulation)"])


class StressScenario(BaseModel):
    name: str = Field(max_length=40)
    inflation: dict[str, float] = Field(description="12-month inflation by category or 'headline', e.g. 0.08")


class SimulationIn(BaseModel):
    budget: float = Field(gt=0, le=1e12, description="Hypothetical amount to allocate (base currency)")
    max_weight: float = Field(default=0.6, gt=0, le=1)
    cash_rate: float = Field(default=0.03, ge=-0.05, le=0.3)
    linker_real_yield: float = Field(default=0.01, ge=-0.05, le=0.1)
    tracker_cost: float = Field(default=0.005, ge=0, le=0.1)
    binary_strike: float | None = Field(default=None, ge=-0.1, le=0.5)
    binary_price: float | None = Field(default=None, gt=0.01, lt=0.99)
    n_scenarios: int = Field(default=2000, ge=200, le=10000)
    seed: int = Field(default=7, ge=0)
    stress_scenarios: list[StressScenario] = Field(default_factory=list, max_length=10)


@router.post("/simulate", status_code=201)
def run_simulation(body: SimulationIn, profile: CurrentProfile, db: DbSession) -> dict:
    result_pei, panel, _ = context.pei_for(db, profile)
    year = max(result_pei.weights_by_year)
    weights = result_pei.weights_by_year[year]
    params = SimulationParams(
        **body.model_dump(exclude={"stress_scenarios"}),
        stress_scenarios=[s.model_dump() for s in body.stress_scenarios],
    )
    if params.max_weight * 10 < 1:
        raise InsufficientDataError("max_weight too small for the available instruments to sum to 1.")
    try:
        result = simulate(weights, panel, params)
    except ValueError as exc:
        raise InsufficientDataError(str(exc)) from exc
    result["base_currency"] = profile.base_currency
    row = PortfolioSimulation(user_id=profile.id, request=body.model_dump(mode="json"), result=result)
    db.add(row)
    audit(db, profile.id, "portfolio.simulated", simulation_id=str(row.id))
    db.commit()
    return {"id": str(row.id), "created_at": row.created_at.isoformat(), **result}


@router.get("/simulations/{simulation_id}")
def get_simulation(simulation_id: uuid.UUID, profile: CurrentProfile, db: DbSession) -> dict:
    row = db.get(PortfolioSimulation, simulation_id)
    if row is None or row.user_id != profile.id:
        raise NotFoundError("Simulation not found.")
    return {"id": str(row.id), "created_at": row.created_at.isoformat(), "request": row.request, **row.result}
