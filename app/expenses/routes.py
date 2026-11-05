from datetime import date

from fastapi import APIRouter, Query

from app.auth.deps import CurrentProfile, DbSession
from app.expenses import analytics
from app.expenses.ledger import load_ledger

router = APIRouter(prefix="/expenses", tags=["expenses"])


@router.get("/summary")
def expense_summary(
    profile: CurrentProfile, db: DbSession, start: date | None = None, end: date | None = None
) -> dict:
    return analytics.summary(load_ledger(db, profile.id, profile.base_currency, start, end))


@router.get("/categories")
def expense_categories(
    profile: CurrentProfile, db: DbSession, start: date | None = None, end: date | None = None
) -> dict:
    ledger = load_ledger(db, profile.id, profile.base_currency, start, end)
    s = analytics.summary(ledger)
    return {
        "base_currency": profile.base_currency,
        "categories": s.get("by_category", []),
        "concentration": s.get("concentration"),
        "unconverted": s.get("unconverted", []),
    }


@router.get("/history")
def expense_history(
    profile: CurrentProfile, db: DbSession, limit: int = Query(24, ge=1, le=120), offset: int = Query(0, ge=0)
) -> dict:
    """Monthly totals by category, most recent month first (paginated by month)."""
    ledger = load_ledger(db, profile.id, profile.base_currency)
    matrix, partial = analytics.category_month_matrix(ledger, exclude_partial=False)
    rows = []
    for month in reversed(matrix.index.tolist()):
        r = matrix.loc[month]
        rows.append(
            {
                "month": month.strftime("%Y-%m"),
                "partial": month.strftime("%Y-%m") in partial,
                "total": round(float(r.sum()), 2),
                "by_category": {c: round(float(v), 2) for c, v in r.items() if v != 0},
            }
        )
    return {
        "base_currency": profile.base_currency,
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
    }


@router.get("/recurring")
def expense_recurring(profile: CurrentProfile, db: DbSession) -> dict:
    items = analytics.recurring_expenses(load_ledger(db, profile.id, profile.base_currency))
    return {
        "base_currency": profile.base_currency,
        "items": items,
        "monthly_total": round(sum(i["monthly_equivalent"] for i in items), 2),
    }
