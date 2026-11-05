"""Personal Expense Index (PEI).

Methodology (also returned by the API):

* Frequency: monthly. Index level 100 at the base month (default: first month of spending history).
* Basket: the user's net spending by category (expenses minus refunds, base currency, transfers and
  income excluded).
* Prices: each category is mapped to an official price index (see ``economic_data.mapping``); categories
  without a direct index use the headline index and are flagged.
* Aggregation: chain-linked Laspeyres with annual re-weighting. Months of calendar year Y use weights
  from spending during year Y-1 and price relatives against December of Y-1:

      PEI_t = PEI_Dec(Y-1) * sum_c w_c(Y) * P_c,t / P_c,Dec(Y-1)

  This uses only information available at time t (no look-ahead). Exception, flagged in the output:
  months before the user has a full prior year of history use the weights of the first available
  12 months (or all available months), because no earlier basket exists.
* Missing data: if a category's price index has no value for a month (publication lag), it is dropped
  for that month and the remaining weights are renormalised; a month is published only if at least
  80 % of the basket weight is priced. No observation is interpolated or extrapolated.

The PEI estimates how the cost of the user's past basket would have evolved at national category
prices. It is not a measurement of the prices this user actually paid.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.economic_data.mapping import HEADLINE
from app.forecasting.prices import CategoryPrices

MIN_COVERAGE = 0.8

METHODOLOGY = {
    "type": "Chain-linked Laspeyres, annual re-weighting, December link month",
    "weights": "Year Y uses the user's net spending shares during year Y-1; the first link uses the first "
    "12 months of history (contemporaneous weights, flagged).",
    "prices": "Official category price indices (BLS CPI-U via FRED, Eurostat HICP COICOP 2018); headline "
    "index where no category index exists.",
    "missing_data": f"Categories without a price for a month are dropped and weights renormalised; months "
    f"with less than {int(MIN_COVERAGE * 100)}% of the basket priced are not published.",
    "limitations": "Estimates the evolution of the cost of the user's historical basket at national "
    "category prices; it does not observe the prices the user actually paid.",
}


@dataclass
class PeiResult:
    history: pd.DataFrame  # index month; columns pei, headline, coverage
    weights_by_year: dict[int, dict[str, float]]
    flags: list[str]
    base_month: pd.Timestamp


def _weights(spend: pd.DataFrame) -> dict[str, float]:
    s = spend.clip(lower=0).sum(axis=0)
    total = float(s.sum())
    return {c: float(v / total) for c, v in s.items() if total > 0 and v > 0}


def compute_pei(
    matrix: pd.DataFrame, panel: dict[str, CategoryPrices], base_month: pd.Timestamp | None = None
) -> PeiResult:
    """``matrix``: months x categories net spending (full months only)."""
    if matrix.empty or matrix.clip(lower=0).sum().sum() <= 0:
        raise ValueError("No spending history to weight the index.")
    flags: list[str] = []
    prices = pd.DataFrame({c: p.series for c, p in panel.items() if c != HEADLINE and not p.series.empty})
    if prices.empty:
        raise ValueError("No price index data available for this geography.")
    prices = prices.sort_index()

    first_spend_month = matrix.index.min()
    base = base_month or first_spend_month
    initial_window = matrix.loc[matrix.index < first_spend_month + pd.DateOffset(months=12)]
    initial_w = _weights(initial_window)
    weights_by_year: dict[int, dict[str, float]] = {}

    def weights_for_year(year: int) -> dict[str, float]:
        if year not in weights_by_year:
            prev = matrix.loc[matrix.index.year == year - 1]
            if len(prev) >= 6:
                weights_by_year[year] = _weights(prev)
            else:
                weights_by_year[year] = initial_w
                if "initial_link_uses_contemporaneous_weights" not in flags:
                    flags.append("initial_link_uses_contemporaneous_weights")
        return weights_by_year[year]

    months = prices.index[(prices.index >= base)]
    level_at_link = 100.0
    link_month = base
    values, coverages, out_months = [], [], []
    current_year = None
    for t in months:
        year = t.year
        if current_year is None:
            current_year = year
        elif year != current_year:
            # New link: rebase on December of the previous year (or the last published month).
            if values:
                level_at_link = values[-1]
                link_month = out_months[-1]
            current_year = year
        w = weights_for_year(year if t != base else year)
        num, cov = 0.0, 0.0
        for c, wc in w.items():
            if c not in prices.columns:
                continue
            p_t, p_0 = prices.at[t, c], prices[c].get(link_month, np.nan)
            if pd.notna(p_t) and pd.notna(p_0) and p_0 > 0:
                num += wc * p_t / p_0
                cov += wc
        if cov < MIN_COVERAGE:
            continue
        values.append(level_at_link * num / cov)
        coverages.append(round(cov, 4))
        out_months.append(t)

    hist = pd.DataFrame({"pei": values, "coverage": coverages}, index=pd.DatetimeIndex(out_months))
    head = panel.get(HEADLINE)
    if head is not None and not head.series.empty and base in head.series.index:
        hist["headline"] = head.series.reindex(hist.index) / head.series[base] * 100.0
    else:
        hist["headline"] = np.nan
        flags.append("headline_unavailable_for_comparison")
    if any(p.meta.get("fallback") for c, p in panel.items() if c in initial_w):
        flags.append("some_categories_use_headline_fallback")
    if any(p.meta.get("proxy") for c, p in panel.items() if c in initial_w):
        flags.append("some_categories_use_proxy_indices")
    return PeiResult(hist, weights_by_year, flags, base)


def yoy(series: pd.Series) -> float | None:
    s = series.dropna()
    if len(s) < 13:
        return None
    last = s.index[-1]
    prev = last - pd.DateOffset(months=12)
    if prev not in s.index:
        return None
    return float(s[last] / s[prev] - 1)
