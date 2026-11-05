"""Deterministic expense analytics. Every figure here is computed from stored transactions."""

import numpy as np
import pandas as pd

from app.expenses.ledger import Ledger, full_months
from app.transactions.models import Category, Kind


def category_month_matrix(ledger: Ledger, exclude_partial: bool = True) -> tuple[pd.DataFrame, list[str]]:
    spend = ledger.spend
    months, partial = full_months(spend)
    if spend.empty:
        return pd.DataFrame(), partial
    m = spend.pivot_table(index="month", columns="category", values="spend", aggfunc="sum").reindex(months)
    m = m.fillna(0.0)
    for c in Category:
        if c.value not in m.columns:
            m[c.value] = 0.0
    m = m[[c.value for c in Category]]
    if exclude_partial and len(m) - len(partial) >= 1:
        m = m.drop(index=[pd.Timestamp(p + "-01") for p in partial], errors="ignore")
    return m, partial


def recurring_expenses(ledger: Ledger) -> list[dict]:
    """Merchants charged on a regular cadence with a stable amount.

    Criteria: >= 3 charges, median gap within a weekly (5-9 d), monthly (25-35 d) or yearly
    (350-380 d) window, and amount coefficient of variation <= 0.2.
    """
    f = ledger.frame
    exp = f[(f["kind"] == Kind.EXPENSE.value) & f["base_amount"].notna() & (f["merchant_key"] != "")]
    out = []
    for key, g in exp.groupby("merchant_key"):
        if len(g) < 3:
            continue
        g = g.sort_values("date")
        gaps = g["date"].diff().dt.days.dropna()
        med = float(gaps.median())
        amounts = g["spend"]
        mean_amt = float(amounts.mean())
        cv = float(amounts.std(ddof=0) / mean_amt) if mean_amt > 0 else 1.0
        if cv > 0.2:
            continue
        if 25 <= med <= 35:
            freq, per_month = "monthly", 1.0
        elif 5 <= med <= 9:
            freq, per_month = "weekly", 52 / 12
        elif 350 <= med <= 380:
            freq, per_month = "yearly", 1 / 12
        else:
            continue
        if freq == "monthly" and g["month"].nunique() < 3:
            continue
        last = g["date"].iloc[-1]
        out.append(
            {
                "merchant": (g["merchant"].dropna().iloc[-1] if g["merchant"].notna().any() else key),
                "merchant_key": key,
                "category": g["category"].mode().iloc[0],
                "frequency": freq,
                "typical_amount": round(float(amounts.median()), 2),
                "monthly_equivalent": round(float(amounts.median()) * per_month, 2),
                "occurrences": int(len(g)),
                "amount_variation": round(cv, 3),
                "last_charged": last.date().isoformat(),
                "next_expected": (last + pd.Timedelta(days=round(med))).date().isoformat(),
            }
        )
    return sorted(out, key=lambda r: -r["monthly_equivalent"])


def _trend(totals: pd.Series) -> dict:
    if len(totals) < 3:
        return {"slope_per_month": None, "slope_pct_of_mean": None, "direction": "insufficient_data"}
    x = np.arange(len(totals), dtype=float)
    slope, _ = np.polyfit(x, totals.values.astype(float), 1)
    mean = float(totals.mean())
    pct = slope / mean if mean else None
    direction = "flat" if pct is None or abs(pct) < 0.01 else ("rising" if slope > 0 else "falling")
    return {
        "slope_per_month": round(float(slope), 2),
        "slope_pct_of_mean": round(pct, 4) if pct is not None else None,
        "direction": direction,
    }


def unusual_changes(matrix: pd.DataFrame, window: int = 6, z: float = 2.0) -> list[dict]:
    """Category-months deviating from the *trailing* window (no look-ahead) by more than ``z`` sigma
    and by at least 25 % of the trailing mean."""
    out = []
    if len(matrix) < 4:
        return out
    for cat in matrix.columns:
        s = matrix[cat]
        for i in range(3, len(s)):
            hist = s.iloc[max(0, i - window) : i]
            mu, sd = float(hist.mean()), float(hist.std(ddof=0))
            v = float(s.iloc[i])
            if mu <= 0 and v <= 0:
                continue
            dev = v - mu
            if abs(dev) >= 0.25 * max(mu, 1.0) and (sd == 0 and abs(dev) > 0 or sd > 0 and abs(dev) / sd > z):
                out.append(
                    {
                        "month": s.index[i].strftime("%Y-%m"),
                        "category": cat,
                        "amount": round(v, 2),
                        "trailing_mean": round(mu, 2),
                        "change_pct": round(dev / mu, 3) if mu else None,
                        "z_score": round(dev / sd, 2) if sd else None,
                    }
                )
    return sorted(out, key=lambda r: r["month"], reverse=True)[:20]


def summary(ledger: Ledger) -> dict:
    f = ledger.frame
    matrix, partial = category_month_matrix(ledger)
    base = ledger.base_currency
    native = []
    for cur, g in f.groupby("currency"):
        native.append(
            {
                "currency": cur,
                "expenses": round(float(-g.loc[g["kind"] == Kind.EXPENSE.value, "amount"].sum()), 2),
                "refunds": round(float(g.loc[g["kind"] == Kind.REFUND.value, "amount"].sum()), 2),
                "income": round(float(g.loc[g["kind"] == Kind.INCOME.value, "amount"].sum()), 2),
                "transfers_out": round(
                    float(-g.loc[(g["kind"] == Kind.TRANSFER.value) & (g["amount"] < 0), "amount"].sum()), 2
                ),
                "transfers_in": round(
                    float(g.loc[(g["kind"] == Kind.TRANSFER.value) & (g["amount"] > 0), "amount"].sum()), 2
                ),
            }
        )
    if matrix.empty:
        return {
            "base_currency": base,
            "has_data": False,
            "by_currency_native": native,
            "unconverted": list(ledger.unconverted.values()),
        }

    totals = matrix.sum(axis=1)
    total = float(totals.sum())
    by_cat = matrix.sum(axis=0)
    shares = (by_cat / total) if total > 0 else by_cat * 0
    recurring = recurring_expenses(ledger)
    fixed_monthly = sum(r["monthly_equivalent"] for r in recurring)
    avg = float(totals.mean())
    hhi = float((shares**2).sum()) if total > 0 else None
    conv = f[f["base_amount"].notna()]
    return {
        "base_currency": base,
        "has_data": True,
        "period": {
            "start": f["date"].min().date().isoformat(),
            "end": f["date"].max().date().isoformat(),
            "months_analyzed": int(len(matrix)),
            "partial_months_excluded": partial,
        },
        "total_expenses": round(total, 2),
        "average_monthly_expenses": round(avg, 2),
        "median_monthly_expenses": round(float(totals.median()), 2),
        "monthly_totals": [
            {"month": i.strftime("%Y-%m"), "amount": round(float(v), 2)} for i, v in totals.items()
        ],
        "by_category": [
            {
                "category": c,
                "total": round(float(by_cat[c]), 2),
                "monthly_average": round(float(matrix[c].mean()), 2),
                "share": round(float(shares[c]), 4),
            }
            for c in by_cat.sort_values(ascending=False).index
            if by_cat[c] != 0
        ],
        "income_total": round(float(conv.loc[conv["kind"] == Kind.INCOME.value, "base_amount"].sum()), 2),
        "refunds_total": round(float(conv.loc[conv["kind"] == Kind.REFUND.value, "base_amount"].sum()), 2),
        "transfers_excluded": int((f["kind"] == Kind.TRANSFER.value).sum()),
        "fixed_vs_variable": {
            "method": "Fixed = monthly equivalent of detected recurring charges; variable = remainder.",
            "fixed_monthly": round(fixed_monthly, 2),
            "variable_monthly": round(max(avg - fixed_monthly, 0.0), 2),
            "fixed_share": round(min(fixed_monthly / avg, 1.0), 4) if avg > 0 else None,
        },
        "volatility": {
            "monthly_std": round(float(totals.std(ddof=1)), 2) if len(totals) > 1 else None,
            "coefficient_of_variation": round(float(totals.std(ddof=1) / avg), 4)
            if len(totals) > 1 and avg
            else None,
        },
        "trend": _trend(totals),
        "unusual_changes": unusual_changes(matrix),
        "concentration": {
            "herfindahl_index": round(hhi, 4) if hhi is not None else None,
            "top_category": str(by_cat.idxmax()),
            "top_category_share": round(float(shares.max()), 4),
            "interpretation": "HHI near 1 = spending concentrated in one category; near 0.09 = spread evenly "
            "across all 11 categories.",
        },
        "recurring_count": len(recurring),
        "by_currency_native": native,
        "unconverted": list(ledger.unconverted.values()),
        "conversion_note": "Non-base-currency rows are converted at the FRED H.10 reference rate of their "
        "transaction date; rows without an available rate are excluded and listed in "
        "'unconverted'.",
    }
