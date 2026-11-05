import hashlib
import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, File, Form, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select

from app.auth.deps import CurrentProfile, DbSession
from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError
from app.schemas.common import Page
from app.transactions import service
from app.transactions.csv_parser import CsvFormatError, parse_csv
from app.transactions.models import Category, ImportBatch, Kind, Transaction

router = APIRouter(tags=["transactions"])


class ImportOut(BaseModel):
    import_id: str
    rows: int
    imported: int
    duplicates: int
    rejected: int
    rejected_rows: list[dict]
    warnings: list[str]
    normalization: dict


class TransactionOut(BaseModel):
    id: str
    booked_on: date
    description: str
    merchant: str | None
    amount: Decimal
    currency: str
    kind: str
    category: str
    category_source: str
    category_confidence: float | None
    user_corrected: bool
    import_id: str | None


def _tx_out(t: Transaction) -> TransactionOut:
    return TransactionOut(
        id=str(t.id),
        booked_on=t.booked_on,
        description=t.description,
        merchant=t.merchant,
        amount=t.amount,
        currency=t.currency,
        kind=t.kind,
        category=t.category,
        category_source=t.category_source,
        category_confidence=t.category_confidence,
        user_corrected=t.user_corrected,
        import_id=str(t.import_id) if t.import_id else None,
    )


@router.post("/transactions/import", response_model=ImportOut, status_code=201)
async def import_csv(
    profile: CurrentProfile,
    db: DbSession,
    file: UploadFile = File(..., description="Bank statement CSV"),
    currency: str | None = Form(
        default=None,
        pattern=r"^[A-Z]{3}$",
        description="Currency for rows without a currency column (default: profile)",
    ),
    date_order: Literal["DMY", "MDY", "YMD"] | None = Form(default=None),
    sign_convention: Literal["auto", "negative_is_outflow", "positive_is_outflow"] = Form(default="auto"),
) -> ImportOut:
    settings = get_settings()
    name = (file.filename or "upload.csv").replace("\\", "/").split("/")[-1]
    if not name.lower().endswith((".csv", ".txt")):
        raise AppError("Only .csv files are accepted.", code="unsupported_file")
    raw = await file.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        raise AppError(
            f"File exceeds {settings.max_upload_bytes // (1024 * 1024)} MB.",
            status_code=413,
            code="payload_too_large",
        )
    if not raw.strip():
        raise AppError("The file is empty.", code="empty_file")
    if b"\x00" in raw[:4096]:
        raise AppError("Binary files are not accepted.", code="unsupported_file")
    try:
        parsed = parse_csv(
            raw,
            default_currency=currency or profile.base_currency,
            preferred_date_order=date_order or profile.date_order,
            sign_convention=sign_convention,
            max_rows=settings.max_csv_rows,
        )
    except CsvFormatError as exc:
        raise AppError(str(exc), code="invalid_csv") from exc
    if not parsed.rows:
        raise AppError(
            "No valid rows were found in the file.",
            code="invalid_csv",
            details={"rejected_rows": parsed.rejected[:20]},
        )
    batch = service.import_rows(db, profile.id, name, hashlib.sha256(raw).hexdigest(), parsed)
    return ImportOut(
        import_id=str(batch.id),
        rows=batch.row_count,
        imported=batch.imported_count,
        duplicates=batch.duplicate_count,
        rejected=batch.rejected_count,
        rejected_rows=parsed.rejected[:20],
        warnings=parsed.warnings,
        normalization=parsed.normalization,
    )


@router.get("/transactions", response_model=Page[TransactionOut])
def list_transactions(
    profile: CurrentProfile,
    db: DbSession,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    start: date | None = None,
    end: date | None = None,
    category: Category | None = None,
    kind: Kind | None = None,
) -> Page[TransactionOut]:
    q = select(Transaction).where(Transaction.user_id == profile.id)
    if start:
        q = q.where(Transaction.booked_on >= start)
    if end:
        q = q.where(Transaction.booked_on <= end)
    if category:
        q = q.where(Transaction.category == category.value)
    if kind:
        q = q.where(Transaction.kind == kind.value)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Transaction.booked_on.desc(), Transaction.id).limit(limit).offset(offset))
    return Page(items=[_tx_out(t) for t in rows], total=total, limit=limit, offset=offset)


class CorrectionIn(BaseModel):
    category: Category | None = None
    kind: Kind | None = None


@router.patch("/transactions/{transaction_id}", response_model=TransactionOut)
def correct_transaction(
    transaction_id: uuid.UUID, body: CorrectionIn, profile: CurrentProfile, db: DbSession
) -> TransactionOut:
    tx = db.get(Transaction, transaction_id)
    if tx is None or tx.user_id != profile.id:  # same response whether missing or not owned
        raise NotFoundError("Transaction not found.")
    if body.category is None and body.kind is None:
        raise AppError("Provide a category and/or a kind.", code="empty_correction")
    return _tx_out(service.correct_category(db, tx, body.category, body.kind))


@router.post("/transactions/categorize")
def categorize_with_ai(profile: CurrentProfile, db: DbSession) -> dict:
    """Classify rows the deterministic rules could not place, using the LLM."""
    return service.ai_categorize(db, profile.id)


class ImportBatchOut(BaseModel):
    id: str
    filename: str
    created_at: str
    rows: int
    imported: int
    duplicates: int
    rejected: int


@router.get("/imports", response_model=Page[ImportBatchOut])
def list_imports(
    profile: CurrentProfile, db: DbSession, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
) -> Page[ImportBatchOut]:
    q = select(ImportBatch).where(ImportBatch.user_id == profile.id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(ImportBatch.created_at.desc()).limit(limit).offset(offset))
    return Page(
        items=[
            ImportBatchOut(
                id=str(b.id),
                filename=b.filename,
                created_at=b.created_at.isoformat(),
                rows=b.row_count,
                imported=b.imported_count,
                duplicates=b.duplicate_count,
                rejected=b.rejected_count,
            )
            for b in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
