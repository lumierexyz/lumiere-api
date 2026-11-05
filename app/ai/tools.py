"""Tools the agent may call. Each tool is bound server-side to the authenticated profile: the model
chooses *which* calculation to run and its scenario parameters, never *whose* data.

Tool outputs contain aggregates only: no transaction descriptions or merchant names (untrusted text
that could carry prompt-injection payloads, and unnecessary personal data for the LLM provider).
"""

import json
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.expenses import analytics
from app.forecasting import engine
from app.risk.routes import coverage_for_profile
from app.services import context
from app.transactions.models import Category
from app.users.models import Profile


class NoArgs(BaseModel):
    pass


class ShockArgs(BaseModel):
    category: Category
    change: float = Field(ge=-0.9, le=5, description="One-off price level change, e.g. 0.2 for +20%")


class ForecastArgs(BaseModel):
    horizon_months: Literal[3, 6, 12, 24]
    inflation_shift_pp: float = Field(default=0.0, ge=-20, le=50)
    shocks: list[ShockArgs] = Field(default_factory=list, max_length=11)


class CoverageArgs(BaseModel):
    reserves: float = Field(gt=0, le=1e12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    inflation_shift_pp: float = Field(default=0.0, ge=-20, le=50)
    shocks: list[ShockArgs] = Field(default_factory=list, max_length=11)


def _scenarios(shift: float, shocks: list[ShockArgs]) -> list[engine.Scenario]:
    custom = engine.Scenario("requested", shift, {s.category.value: s.change for s in shocks})
    return (
        [engine.Scenario("baseline"), custom]
        if (shift or shocks)
        else [engine.Scenario("baseline"), *engine.DEFAULT_SCENARIOS[1:]]
    )


def _expense_summary(db: Session, profile: Profile, _: NoArgs) -> dict:
    s = analytics.summary(context.user_context(db, profile).ledger)
    if not s.get("has_data"):
        return {"has_data": False}
    keep = (
        "base_currency",
        "period",
        "average_monthly_expenses",
        "median_monthly_expenses",
        "by_category",
        "fixed_vs_variable",
        "volatility",
        "trend",
        "concentration",
        "recurring_count",
        "income_total",
        "unconverted",
    )
    out = {k: s[k] for k in keep if k in s}
    out["unusual_changes"] = [
        {k: v for k, v in u.items() if k != "merchant"} for u in s["unusual_changes"][:5]
    ]
    out["monthly_totals_last_12"] = s["monthly_totals"][-12:]
    return out


def _pei(db: Session, profile: Profile, _: NoArgs) -> dict:
    s = context.pei_summary(db, profile)
    s.pop("methodology", None)
    return s


def _pei_composition(db: Session, profile: Profile, _: NoArgs) -> dict:
    c = context.pei_composition(db, profile)
    c.pop("methodology", None)
    return c


def _forecast(db: Session, profile: Profile, a: ForecastArgs) -> dict:
    r = context.forecast(db, profile, a.horizon_months, _scenarios(a.inflation_shift_pp, a.shocks))
    for s in r["scenarios"]:
        s.pop("monthly", None)  # keep the payload small
    r.pop("methodology", None)
    return r


def _coverage(db: Session, profile: Profile, a: CoverageArgs) -> dict:
    r = coverage_for_profile(db, profile, a.reserves, a.currency, _scenarios(a.inflation_shift_pp, a.shocks))
    r.pop("methodology", None)
    return r


TOOLS: dict[str, tuple[str, type[BaseModel], Callable]] = {
    "get_expense_summary": (
        "Aggregated spending profile: monthly averages, categories, trend, volatility, "
        "fixed vs variable, concentration.",
        NoArgs,
        _expense_summary,
    ),
    "get_personal_expense_index": (
        "The user's Personal Expense Index, its year-over-year change and the headline inflation comparison.",
        NoArgs,
        _pei,
    ),
    "get_pei_composition": (
        "Category weights of the PEI, each category's inflation and its contribution.",
        NoArgs,
        _pei_composition,
    ),
    "forecast_expenses": (
        "Project future monthly and total living costs over 3, 6, 12 or 24 months, optionally "
        "with an inflation shift and category price shocks.",
        ForecastArgs,
        _forecast,
    ),
    "calculate_expense_coverage": (
        "How many months/days the given reserves would cover projected expenses, "
        "optionally under an inflation shift or category shocks.",
        CoverageArgs,
        _coverage,
    ),
}


def _strict(schema: dict) -> dict:
    """Strict tool schemas: every property required, no additional properties, refs inlined.
    Range/length constraints are dropped from the schema and enforced by Pydantic in ``run_tool``."""
    schema = json.loads(json.dumps(schema))
    defs = schema.pop("$defs", {})

    def fix(node: dict) -> dict:
        if "$ref" in node:
            node = fix(dict(defs[node["$ref"].split("/")[-1]]))
        if node.get("type") == "object":
            props = node.get("properties", {})
            node["properties"] = {k: fix(v) for k, v in props.items()}
            node["required"] = list(props)
            node["additionalProperties"] = False
        if node.get("type") == "array" and "items" in node:
            node["items"] = fix(node["items"])
        for k in (
            "default",
            "title",
            "maxLength",
            "minLength",
            "ge",
            "le",
            "exclusiveMinimum",
            "minimum",
            "maximum",
            "maxItems",
            "minItems",
            "pattern",
        ):
            node.pop(k, None)
        return node

    return fix(schema)


def openai_tool_specs() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": d,
                "strict": True,
                "parameters": _strict(m.model_json_schema()),
            },
        }
        for n, (d, m, _) in TOOLS.items()
    ]


def claude_tool_specs() -> list[dict]:
    return [
        {"name": n, "description": d, "strict": True, "input_schema": _strict(m.model_json_schema())}
        for n, (d, m, _) in TOOLS.items()
    ]


def run_tool(db: Session, profile: Profile, name: str, raw_args: str) -> dict:
    if name not in TOOLS:
        return {"error": f"Unknown tool {name}."}
    _, model, fn = TOOLS[name]
    try:
        args = model.model_validate_json(raw_args or "{}")
    except Exception:
        return {"error": "Invalid arguments for this tool."}
    try:
        return fn(db, profile, args)
    except AppError as exc:
        return {"error": exc.message, "code": exc.code}
