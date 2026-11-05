import hashlib
import uuid
from collections import Counter
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.classification import ItemIn, amount_bucket, classify_batch
from app.core.logging import logger
from app.services.audit import audit
from app.transactions.categorizer import classify, merchant_key, normalize_text
from app.transactions.csv_parser import ParseResult
from app.transactions.models import Category, CategorySource, ImportBatch, Kind, Transaction


def dedupe_key(
    user_id: uuid.UUID, booked_on, amount: Decimal, currency: str, key: str, occurrence: int
) -> str:
    """Identical rows inside one file are distinct purchases (two coffees on the same day), so the
    occurrence index within the file is part of the key; re-importing the same or an overlapping
    statement then yields the same keys and is skipped."""
    raw = (
        f"{user_id}|{booked_on.isoformat()}|{amount.quantize(Decimal('0.01'))}|{currency}|{key}|{occurrence}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def import_rows(
    db: Session, user_id: uuid.UUID, filename: str, file_sha: str, parsed: ParseResult
) -> ImportBatch:
    batch = ImportBatch(
        user_id=user_id,
        filename=filename[:120],
        file_sha256=file_sha,
        row_count=len(parsed.rows) + len(parsed.rejected),
        rejected_count=len(parsed.rejected),
        normalization=parsed.normalization,
    )
    db.add(batch)
    db.flush()

    seen: Counter[tuple] = Counter()
    candidates: list[Transaction] = []
    for row in parsed.rows:
        mkey = merchant_key(row.merchant, row.description)
        ident = (
            row.booked_on,
            row.amount.quantize(Decimal("0.01")),
            row.currency,
            mkey or normalize_text(row.description)[:80],
        )
        occ = seen[ident]
        seen[ident] += 1
        c = classify(row.description, row.merchant, row.amount, row.category)
        candidates.append(
            Transaction(
                user_id=user_id,
                import_id=batch.id,
                booked_on=row.booked_on,
                description=row.description,
                merchant=row.merchant,
                merchant_key=mkey,
                amount=row.amount,
                currency=row.currency,
                kind=c.kind.value,
                category=c.category.value,
                category_source=c.source,
                category_confidence=c.confidence,
                dedupe_key=dedupe_key(user_id, *ident, occ),
            )
        )

    keys = [t.dedupe_key for t in candidates]
    existing: set[str] = set()
    for i in range(0, len(keys), 500):
        existing |= set(
            db.scalars(
                select(Transaction.dedupe_key).where(
                    Transaction.user_id == user_id, Transaction.dedupe_key.in_(keys[i : i + 500])
                )
            )
        )
    new = [t for t in candidates if t.dedupe_key not in existing]
    db.add_all(new)
    batch.imported_count = len(new)
    batch.duplicate_count = len(candidates) - len(new)
    audit(
        db,
        user_id,
        "transactions.imported",
        import_id=str(batch.id),
        imported=len(new),
        duplicates=batch.duplicate_count,
        rejected=batch.rejected_count,
    )
    db.commit()
    logger.info(
        "import_done",
        extra={
            "user_id": str(user_id),
            "imported": len(new),
            "duplicates": batch.duplicate_count,
            "rejected": batch.rejected_count,
        },
    )
    return batch


def ai_categorize(db: Session, user_id: uuid.UUID, limit: int = 200) -> dict:
    """Send still-uncategorised expense rows to the LLM. Never touches user-corrected rows."""
    rows = list(
        db.scalars(
            select(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.user_corrected.is_(False),
                Transaction.category_source == CategorySource.NONE.value,
                Transaction.kind.in_([Kind.EXPENSE.value, Kind.REFUND.value]),
            )
            .order_by(Transaction.booked_on.desc())
            .limit(limit)
        )
    )
    updated = 0
    for start in range(0, len(rows), 40):
        chunk = rows[start : start + 40]
        items = [
            ItemIn(
                id=i,
                text=t.description[:100],
                merchant_key=t.merchant_key,
                direction="outflow" if t.amount < 0 else "inflow",
                amount_bucket=amount_bucket(abs(float(t.amount))),
            )
            for i, t in enumerate(chunk)
        ]
        results = classify_batch(items)
        for i, t in enumerate(chunk):
            r = results.get(i)
            if r is None or t.user_corrected:
                continue
            t.category = r.category.value
            t.category_source = CategorySource.AI.value
            t.category_confidence = round(r.confidence, 3)
            updated += 1
    audit(db, user_id, "transactions.ai_categorized", count=updated)
    db.commit()
    return {"considered": len(rows), "updated": updated}


def correct_category(
    db: Session, tx: Transaction, category: Category | None, kind: Kind | None
) -> Transaction:
    if category is not None:
        tx.category = category.value
        tx.category_source = CategorySource.USER.value
        tx.category_confidence = 1.0
        tx.user_corrected = True
    if kind is not None:
        tx.kind = kind.value
        tx.user_corrected = True
    audit(db, tx.user_id, "transaction.corrected", transaction_id=str(tx.id))
    db.commit()
    return tx
