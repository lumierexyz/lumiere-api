from fastapi import APIRouter

from app.auth.deps import CurrentProfile, DbSession
from app.onchain.service import onchain_reserves

router = APIRouter(prefix="/reserves", tags=["reserves"])


@router.get("/onchain")
def get_onchain_reserves(profile: CurrentProfile, db: DbSession) -> dict:
    """Balances of the wallets linked to this account on Robinhood Chain, valued in the base currency."""
    return onchain_reserves(db, profile)
