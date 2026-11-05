"""Future Expense Coverage: how long reserves last against *projected* expenses.

Reserves are drawn down month by month against the scenario's projected spending (which grows with
category inflation). When the remaining reserve is smaller than the next month's projected spending,
the fractional month ``remaining / spending_next`` is added. Days are months x 30.436875 (average
Gregorian month). Nominal coverage (reserves / current average monthly spending, no price growth) is
returned alongside for comparison.
"""

from app.forecasting.engine import Baseline, Scenario, monthly_path

DAYS_PER_MONTH = 365.2425 / 12
MAX_MONTHS = 600


def coverage_for(baseline: Baseline, reserves: float, scenario: Scenario) -> dict:
    current_monthly = sum(baseline.monthly_by_category.values())
    if current_monthly <= 0:
        return {
            "scenario": scenario.name,
            "months": None,
            "days": None,
            "note": "No positive projected spending; coverage is undefined.",
        }
    path = monthly_path(baseline, scenario, MAX_MONTHS)
    remaining = reserves
    months = 0.0
    consumed = dict.fromkeys(baseline.monthly_by_category, 0.0)
    exhausted = False
    for month in path:
        total = sum(month.values())
        if total <= 0:
            months += 1
            continue
        if remaining >= total:
            remaining -= total
            months += 1
            for c, v in month.items():
                consumed[c] += v
        else:
            frac = remaining / total
            months += frac
            for c, v in month.items():
                consumed[c] += v * frac
            remaining = 0.0
            exhausted = True
            break
    spent = sum(consumed.values())
    return {
        "scenario": scenario.name,
        "inflation_shift_pp": scenario.inflation_shift_pp,
        "category_shocks": scenario.shocks,
        "months": round(months, 2) if exhausted else None,
        "days": round(months * DAYS_PER_MONTH, 1) if exhausted else None,
        "exceeds_horizon": not exhausted,
        "note": None if exhausted else f"Reserves exceed {MAX_MONTHS} months of projected expenses.",
        "first_month_projected_expenses": round(sum(path[0].values()), 2),
        "by_category": [
            {"category": c, "amount": round(v, 2), "share": round(v / spent, 4) if spent else None}
            for c, v in sorted(consumed.items(), key=lambda kv: -kv[1])
            if v > 0
        ],
    }


def calculate(baseline: Baseline, reserves: float, scenarios: list[Scenario]) -> dict:
    current_monthly = sum(baseline.monthly_by_category.values())
    nominal_months = reserves / current_monthly if current_monthly > 0 else None
    return {
        "current_monthly_expenses": round(current_monthly, 2),
        "nominal": {
            "months": round(nominal_months, 2) if nominal_months is not None else None,
            "days": round(nominal_months * DAYS_PER_MONTH, 1) if nominal_months is not None else None,
            "definition": "Reserves divided by current average monthly spending, assuming prices stay flat.",
        },
        "projected": [coverage_for(baseline, reserves, s) for s in scenarios],
        "methodology": __doc__.strip(),
    }
