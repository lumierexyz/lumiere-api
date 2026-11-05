"""Loads a user's transactions into a DataFrame with explicit, dated currency conversion.

Amounts are kept in their original currency. A base-currency value is computed only when an explicit
reference rate exists for the transaction date (or the transaction is already in the base currency);
otherwise the row is excluded from base-currency totals and reported in ``unconverted``.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.economic_data.service import conversion_rate
from app.transactions.models import Kind, Transaction


@dataclass
class Ledger:
    base_currency: str
    frame: pd.DataFrame  # one row per transaction
    unconverted: dict[str, dict] = field(default_factory=dict)
    conversions: list[dict] = field(default_factory=list)

    @property
    def spend(self) -> pd.DataFrame:
        """Expense rows (positive) net of refunds (negative), in base currency, convertible only."""
        f = self.frame
        return f[f["kind"].isin([Kind.EXPENSE.value, Kind.REFUND.value]) & f["base_amount"].notna()].copy()


def load_ledger(
    db: Session, user_id: uuid.UUID, base_currency: str, start: date | None = None, end: date | None = None
) -> Ledger:
    q = select(
        Transaction.booked_on,
        Transaction.amount,
        Transaction.currency,
        Transaction.kind,
        Transaction.category,
        Transaction.merchant_key,
        Transaction.merchant,
    ).where(Transaction.user_id == user_id)
    if start:
        q = q.where(Transaction.booked_on >= start)
    if end:
        q = q.where(Transaction.booked_on <= end)
    rows = db.execute(q).all()
    cols = ["date", "amount", "currency", "kind", "category", "merchant_key", "merchant"]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        df["base_amount"] = pd.Series(dtype=float)
        df["spend"] = pd.Series(dtype=float)
        df["month"] = pd.Series(dtype="datetime64[ns]")
        return Ledger(base_currency, df)

    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = df["amount"].astype(float)
    df["month"] = df["date"].dt.to_period("M").dt.to_timestamp()

    ledger = Ledger(base_currency, df)
    rates: dict[tuple[str, date], dict | None] = {}
    base_vals = []
    for cur, d, amt in zip(df["currency"], df["date"].dt.date, df["amount"], strict=True):
        if cur == base_currency:
            base_vals.append(amt)
            continue
        key = (cur, d)
        if key not in rates:
            rates[key] = conversion_rate(db, cur, base_currency, d)
        r = rates[key]
        if r is None:
            base_vals.append(float("nan"))
            u = ledger.unconverted.setdefault(cur, {"currency": cur, "transactions": 0, "net_amount": 0.0})
            u["transactions"] += 1
            u["net_amount"] = round(u["net_amount"] + amt, 2)
        else:
            base_vals.append(amt * r["rate"])
    ledger.conversions = [
        {"currency": c, "date": d.isoformat(), **r} for (c, d), r in list(rates.items())[:50] if r
    ]
    df["base_amount"] = base_vals
    # Spending sign convention: expense outflows positive, refunds negative.
    df["spend"] = -df["base_amount"]
    return ledger


def full_months(spend: pd.DataFrame) -> tuple[pd.DatetimeIndex, list[str]]:
    """Calendar months between first and last transaction; months at either edge that are only partly
    covered are listed so averages can exclude them."""
    if spend.empty:
        return pd.DatetimeIndex([]), []
    first, last = spend["date"].min(), spend["date"].max()
    months = pd.date_range(first.to_period("M").to_timestamp(), last.to_period("M").to_timestamp(), freq="MS")
    partial = []
    if first.day > 3:
        partial.append(months[0].strftime("%Y-%m"))
    if last.day < (last + pd.offsets.MonthEnd(0)).day - 3:
        partial.append(months[-1].strftime("%Y-%m"))
    return months, partial
