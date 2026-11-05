"""Scenario-based projection of future living costs. Interpretable and deterministic; no LLM.

For each category c:

    base_c      = average monthly net spending over the last (up to) 12 full months, restated at the
                  latest price level of the category index (avg spend * P_last / avg P over the window)
    pi_c        = trailing 12-month annualised inflation of the category index (headline if unmapped)
    pi_c,s      = pi_c + delta_s                       (scenario shift, percentage points)
    E_c,m       = base_c * (1 + pi_c,s)^(m/12) * (1 + shock_c,s)    for month m = 1..H

Uncertainty bands reflect month-to-month variation of the user's own spending (std of monthly totals
over history, at least 6 months required), assuming independent monthly deviations
(sigma_H = sigma * sqrt(H)); they do not capture model or macroeconomic uncertainty, which the
scenario spread illustrates instead.
"""

import math
from dataclasses import dataclass, field

import pandas as pd

from app.economic_data.mapping import HEADLINE
from app.forecasting.prices import CategoryPrices, annual_inflation

Z80 = 1.2816


@dataclass
class Scenario:
    name: str
    inflation_shift_pp: float = 0.0
    shocks: dict[str, float] = field(default_factory=dict)  # category -> one-off level change (0.2 = +20%)


DEFAULT_SCENARIOS = [
    Scenario("baseline"),
    Scenario("lower_inflation", -1.5),
    Scenario("higher_inflation", 3.0),
]


@dataclass
class Baseline:
    monthly_by_category: dict[str, float]
    inflation_by_category: dict[str, float]
    inflation_source: dict[str, str]
    monthly_total_std: float | None
    months_of_history: int
    notes: list[str]


def build_baseline(
    matrix: pd.DataFrame, panel: dict[str, CategoryPrices], assumed_inflation: float | None = None
) -> Baseline:
    if matrix.empty:
        raise ValueError("No spending history.")
    window = matrix.iloc[-12:]
    notes: list[str] = []
    headline_pi = annual_inflation(panel[HEADLINE].series) if HEADLINE in panel else None
    monthly, infl, src = {}, {}, {}
    for c in window.columns:
        avg = float(window[c].mean())
        if avg <= 0:
            continue
        cp = panel.get(c)
        s = cp.series if cp is not None else pd.Series(dtype=float)
        level_adj = 1.0
        if not s.empty:
            win_prices = s.reindex(window.index).dropna()
            if len(win_prices) >= max(3, len(window) // 2):
                level_adj = float(s.dropna().iloc[-1] / win_prices.mean())
        monthly[c] = avg * level_adj
        pi = annual_inflation(s) if not s.empty else None
        if pi is not None:
            infl[c], src[c] = pi, (cp.meta.get("series_id", "") if cp else "")
            if cp and cp.meta.get("fallback"):
                src[c] += " (headline fallback)"
        elif headline_pi is not None:
            infl[c], src[c] = headline_pi, "headline index (category index unavailable)"
        elif assumed_inflation is not None:
            infl[c], src[c] = assumed_inflation, "user-supplied assumption"
        else:
            raise ValueError(f"No price data for category '{c}' and no assumed_annual_inflation provided.")
    totals = matrix.sum(axis=1)
    std = float(totals.std(ddof=1)) if len(totals) >= 6 else None
    if std is None:
        notes.append("Fewer than 6 months of history: no statistical uncertainty band is reported.")
    return Baseline(monthly, infl, src, std, len(matrix), notes)


def project(baseline: Baseline, horizon: int, scenario: Scenario) -> dict:
    months = []
    by_cat_total = dict.fromkeys(baseline.monthly_by_category, 0.0)
    for m in range(1, horizon + 1):
        row = {}
        for c, base in baseline.monthly_by_category.items():
            pi = baseline.inflation_by_category[c] + scenario.inflation_shift_pp / 100
            v = base * (1 + pi) ** (m / 12) * (1 + scenario.shocks.get(c, 0.0))
            row[c] = v
            by_cat_total[c] += v
        months.append(
            {
                "month_offset": m,
                "total": round(sum(row.values()), 2),
                "by_category": {c: round(v, 2) for c, v in row.items()},
            }
        )
    total = sum(x["total"] for x in months)
    out = {
        "scenario": scenario.name,
        "assumptions": {
            "inflation_shift_pp": scenario.inflation_shift_pp,
            "category_shocks": scenario.shocks,
            "annual_inflation_by_category": {
                c: round(v + scenario.inflation_shift_pp / 100, 4)
                for c, v in baseline.inflation_by_category.items()
            },
        },
        "expected_monthly_average": round(total / horizon, 2),
        "expected_total": round(total, 2),
        "by_category_total": {c: round(v, 2) for c, v in sorted(by_cat_total.items(), key=lambda kv: -kv[1])},
        "monthly": months,
    }
    if baseline.monthly_total_std is not None:
        sd = baseline.monthly_total_std
        out["uncertainty_80pct"] = {
            "monthly_low": round(max(out["expected_monthly_average"] - Z80 * sd, 0), 2),
            "monthly_high": round(out["expected_monthly_average"] + Z80 * sd, 2),
            "total_low": round(max(total - Z80 * sd * math.sqrt(horizon), 0), 2),
            "total_high": round(total + Z80 * sd * math.sqrt(horizon), 2),
            "method": "Normal approximation from the std of historical monthly totals; independent months.",
        }
    return out


def monthly_path(baseline: Baseline, scenario: Scenario, months: int) -> list[dict[str, float]]:
    """Per-category projected spending for months 1..months (used by the coverage engine)."""
    path = []
    for m in range(1, months + 1):
        path.append(
            {
                c: base
                * (1 + baseline.inflation_by_category[c] + scenario.inflation_shift_pp / 100) ** (m / 12)
                * (1 + scenario.shocks.get(c, 0.0))
                for c, base in baseline.monthly_by_category.items()
            }
        )
    return path
