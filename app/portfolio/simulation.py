"""Simulation-only hedging research: could a mix of *hypothetical* instruments keep pace with a user's
Personal Expense Index?

Nothing here is executable or priced by a market. Instruments are explicitly hypothetical, defined by
documented payoff formulas over simulated inflation scenarios:

* Scenarios: 12-month category inflation vectors drawn from a multivariate normal whose mean and
  covariance are estimated from historical 12-month log changes of the official category indices
  (overlapping monthly windows; this understates tail risk and overstates sample size, so treat the
  dispersion as indicative). Optional user stress scenarios are appended.
* Liability: PEI inflation in each scenario, pi_PEI = sum_c w_c * pi_c (current basket weights).
* Instruments (1-year holding-period returns):
    HYP_CASH                      r_cash (user-set nominal rate)
    HYP_HEADLINE_LINKER           pi_headline + real_yield (stylised inflation-linked note, no credit risk)
    HYP_<CATEGORY>_TRACKER        pi_category - tracking_cost (stylised claim on a category price index)
    HYP_BINARY_HEADLINE_ABOVE_K   pays 1 per contract if pi_headline > K, bought at price p:
                                  return = 1/p - 1 if the event occurs else -1.
  A binary contract has a fixed payout: it does not scale with *how much* prices rise, so it cannot
  replicate a continuous expense increase; its fit is measured, not assumed.
* Optimisation (CVXPY): minimise mean squared real shortfall E[(R_p - pi_PEI)^2] subject to a budget
  constraint (weights sum to 1), long-only weights and a per-instrument cap.
"""

from dataclasses import dataclass

import cvxpy as cp
import numpy as np
import pandas as pd

from app.economic_data.mapping import HEADLINE
from app.forecasting.prices import CategoryPrices

TRACKABLE = ("housing", "groceries", "energy", "transportation", "healthcare")


@dataclass
class SimulationParams:
    budget: float
    max_weight: float = 0.6
    cash_rate: float = 0.03
    linker_real_yield: float = 0.01
    tracker_cost: float = 0.005
    binary_strike: float | None = None
    binary_price: float | None = None
    n_scenarios: int = 2000
    seed: int = 7
    stress_scenarios: list[dict] | None = None


def yearly_changes(panel: dict[str, CategoryPrices], cats: list[str]) -> pd.DataFrame:
    cols = {}
    for c in cats:
        p = panel.get(c)
        s = p.series.dropna() if p is not None else pd.Series(dtype=float)
        if len(s) < 37:  # need >= 3 years for a 12-month change distribution
            s = panel[HEADLINE].series.dropna() if HEADLINE in panel else s
        cols[c] = np.log(s / s.shift(12)).dropna()
    df = pd.DataFrame(cols).dropna()
    if len(df) < 24:
        raise ValueError(
            "Not enough price history (need at least 3 years of monthly data) to estimate "
            "an inflation scenario distribution."
        )
    return np.expm1(df)


def simulate(weights: dict[str, float], panel: dict[str, CategoryPrices], params: SimulationParams) -> dict:
    basket = [c for c, w in weights.items() if w > 0]
    cats = list(dict.fromkeys([HEADLINE, *basket, *[c for c in TRACKABLE if c in basket]]))
    hist = yearly_changes(panel, cats)
    mu, cov = hist.mean().values, np.cov(hist.values.T)
    rng = np.random.default_rng(params.seed)
    draws = rng.multivariate_normal(mu, cov, size=params.n_scenarios, method="eigh")
    scen = pd.DataFrame(draws, columns=hist.columns)
    for st in params.stress_scenarios or []:
        row = {
            c: float(st.get("inflation", {}).get(c, mu[list(hist.columns).index(c)])) for c in hist.columns
        }
        scen = pd.concat([scen, pd.DataFrame([row])], ignore_index=True)
    w = np.array([weights[c] for c in basket])
    w = w / w.sum()
    pei = scen[basket].values @ w
    head = scen[HEADLINE].values

    instruments: dict[str, np.ndarray] = {
        "HYP_CASH": np.full(len(scen), params.cash_rate),
        "HYP_HEADLINE_LINKER": head + params.linker_real_yield,
    }
    for c in TRACKABLE:
        if c in basket:
            instruments[f"HYP_{c.upper()}_TRACKER"] = scen[c].values - params.tracker_cost
    strike = params.binary_strike if params.binary_strike is not None else float(np.median(head)) + 0.01
    event = head > strike
    fair = float(event.mean())
    price = params.binary_price if params.binary_price is not None else min(max(fair + 0.02, 0.02), 0.98)
    instruments[f"HYP_BINARY_HEADLINE_ABOVE_{strike:.3f}"] = np.where(event, 1.0 / price - 1.0, -1.0)

    names = list(instruments)
    R = np.column_stack([instruments[n] for n in names])
    x = cp.Variable(len(names))
    objective = cp.Minimize(cp.sum_squares(R @ x - pei) / len(pei))
    constraints = [cp.sum(x) == 1, x >= 0, x <= params.max_weight]
    problem = cp.Problem(objective, constraints)
    problem.solve(solver=cp.CLARABEL)
    if x.value is None or problem.status not in ("optimal", "optimal_inaccurate"):
        raise ValueError(f"Optimisation did not converge ({problem.status}).")
    xv = np.clip(x.value, 0, None)
    xv = xv / xv.sum()

    def metrics(weights_vec: np.ndarray) -> dict:
        rp = R @ weights_vec
        diff = rp - pei
        shortfall = pei - rp
        tail = np.sort(shortfall)[-max(1, int(0.05 * len(shortfall))) :]
        return {
            "expected_return": round(float(rp.mean()), 4),
            "expected_real_return_vs_pei": round(float(diff.mean()), 4),
            "tracking_error": round(float(diff.std(ddof=1)), 4),
            "probability_keeps_pace": round(float((diff >= 0).mean()), 4),
            "cvar95_shortfall": round(float(tail.mean()), 4),
            "expected_shortfall_amount": round(float(np.clip(shortfall, 0, None).mean() * params.budget), 2),
        }

    cash_vec = np.array([1.0 if n == "HYP_CASH" else 0.0 for n in names])
    opt, cash = metrics(xv), metrics(cash_vec)
    var_opt = float(np.var(R @ xv - pei, ddof=1))
    var_cash = float(np.var(R @ cash_vec - pei, ddof=1))
    opt["hedge_effectiveness_vs_cash"] = round(1 - var_opt / var_cash, 4) if var_cash > 0 else None

    sensitivity = []
    for i, c in enumerate(basket):
        bump = np.zeros(len(basket))
        bump[i] = 0.01
        pei_b = pei + float(w @ bump)
        rp = R @ xv
        # Instruments tied to the bumped category move with it (trackers; headline-linked if c == headline)
        rp_b = rp.copy()
        tn = f"HYP_{c.upper()}_TRACKER"
        if tn in names:
            rp_b = rp_b + 0.01 * xv[names.index(tn)]
        sensitivity.append(
            {
                "category": c,
                "shock_pp": 1.0,
                "pei_change_pp": round(float(w[i]) * 1.0, 4),
                "change_in_expected_shortfall_pp": round(
                    float(((pei_b - rp_b) - (pei - rp)).mean()) * 100, 4
                ),
            }
        )
    return {
        "simulation": True,
        "label": "SIMULATION WITH HYPOTHETICAL INSTRUMENTS - NOT EXECUTABLE, NOT INVESTMENT ADVICE",
        "budget": params.budget,
        "allocation": [
            {"instrument": n, "weight": round(float(v), 4), "amount": round(float(v) * params.budget, 2)}
            for n, v in sorted(zip(names, xv, strict=True), key=lambda t: -t[1])
            if v > 1e-4
        ],
        "metrics": opt,
        "benchmarks": {"all_cash": cash},
        "binary_contract": {
            "strike_headline_inflation": round(strike, 4),
            "assumed_price": round(price, 4),
            "model_event_probability": round(fair, 4),
            "note": "Price is an assumption (model probability + 2 pt spread unless provided), "
            "not a market quote. Binary payout does not scale with the size of the "
            "price increase.",
        },
        "scenario_distribution": {
            "n_scenarios": int(len(scen)),
            "seed": params.seed,
            "history_months": int(len(hist)),
            "history_start": hist.index[0].strftime("%Y-%m"),
            "history_end": hist.index[-1].strftime("%Y-%m"),
            "mean_12m_inflation": {c: round(float(v), 4) for c, v in zip(hist.columns, mu, strict=True)},
            "pei_inflation_p5_p50_p95": [round(float(np.percentile(pei, q)), 4) for q in (5, 50, 95)],
        },
        "basket_weights": {c: round(float(v), 4) for c, v in zip(basket, w, strict=True)},
        "sensitivity": sensitivity,
        "solver": {
            "status": problem.status,
            "objective": round(float(problem.value), 8),
            "constraints": {"budget": "weights sum to 1", "long_only": True, "max_weight": params.max_weight},
        },
        "methodology": __doc__.strip(),
        "limitations": [
            "Instruments are hypothetical; real instruments carry credit, liquidity, basis and fee risks.",
            "Scenario dispersion is estimated from limited overlapping history and is indicative only.",
            "One-year horizon; no rebalancing, taxes or transaction costs beyond the stated tracking cost.",
        ],
    }
