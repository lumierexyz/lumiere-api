from typing import Literal

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete

from app.auth.deps import CurrentProfile, DbSession
from app.core.config import get_settings
from app.core.logging import logger
from app.database.results import Conversation, Forecast, PortfolioSimulation
from app.schemas.common import CURRENCY_PATTERN
from app.services.audit import audit
from app.transactions.models import ImportBatch, Transaction
from app.users.models import Profile

router = APIRouter(prefix="/me", tags=["profile"])


class ProfileOut(BaseModel):
    id: str
    email: str | None
    display_name: str | None
    base_currency: str
    country: str
    date_order: str | None


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    base_currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    date_order: Literal["DMY", "MDY", "YMD"] | None = None


def _out(p: Profile) -> ProfileOut:
    return ProfileOut(
        id=str(p.id),
        email=p.email,
        display_name=p.display_name,
        base_currency=p.base_currency,
        country=p.country,
        date_order=p.date_order,
    )


@router.get("", response_model=ProfileOut)
def get_me(profile: CurrentProfile) -> ProfileOut:
    return _out(profile)


@router.patch("", response_model=ProfileOut)
def update_me(body: ProfileUpdate, profile: CurrentProfile, db: DbSession) -> ProfileOut:
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    audit(db, profile.id, "profile.updated", fields=sorted(body.model_dump(exclude_unset=True)))
    db.commit()
    return _out(profile)


class DeletionResult(BaseModel):
    deleted_financial_data: bool
    deleted_auth_account: bool
    note: str


@router.delete("/data", response_model=DeletionResult)
def delete_financial_data(profile: CurrentProfile, db: DbSession) -> DeletionResult:
    """Erase all transactions, imports, forecasts, simulations and conversations; keep the account."""
    uid = profile.id
    for model in (Transaction, ImportBatch, Forecast, PortfolioSimulation, Conversation):
        db.execute(delete(model).where(model.user_id == uid))
    audit(db, uid, "financial_data.deleted")
    db.commit()
    return DeletionResult(
        deleted_financial_data=True,
        deleted_auth_account=False,
        note="All financial data was deleted. Your sign-in account remains.",
    )


@router.delete("", response_model=DeletionResult)
def delete_account(
    profile: CurrentProfile,
    db: DbSession,
    confirm: bool = Query(False, description="Must be true to delete the account permanently."),
) -> DeletionResult:
    from app.core.errors import AppError

    if not confirm:
        raise AppError("Pass confirm=true to permanently delete the account.", code="confirmation_required")
    uid, subject = profile.id, profile.auth_subject
    db.delete(profile)  # cascades to every user-owned table
    audit(db, uid, "account.deleted")
    db.commit()

    settings = get_settings()
    auth_deleted = False
    if settings.auth_provider == "privy":
        from app.auth import privy_admin

        auth_deleted = bool(subject) and privy_admin.delete_user(subject)
    elif settings.supabase_service_role_key and settings.supabase_url:
        try:
            r = httpx.delete(
                f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{uid}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                },
                timeout=settings.http_timeout_seconds,
            )
            auth_deleted = r.status_code in (200, 204, 404)
        except httpx.HTTPError:
            logger.warning("auth_account_delete_failed", extra={"user_id": str(uid)})
    note = (
        "Account and all data deleted."
        if auth_deleted
        else "All data deleted. The sign-in account could not be removed server-side; "
        "it must be deleted from the identity provider dashboard."
    )
    return DeletionResult(deleted_financial_data=True, deleted_auth_account=auth_deleted, note=note)
