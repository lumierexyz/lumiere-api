"""Server-side Privy REST calls, authenticated with the app secret (never sent to browsers).

Used to read a new user's verified email when their profile is provisioned and to delete the Privy
user when they delete their Lumiere account.
"""

import httpx

from app.core.config import get_settings
from app.core.logging import logger

BASE = "https://auth.privy.io/api/v1"
_transport: httpx.BaseTransport | None = None  # tests inject httpx.MockTransport


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport


def _client() -> httpx.Client | None:
    s = get_settings()
    if not (s.privy_app_id and s.privy_app_secret):
        return None
    return httpx.Client(
        base_url=BASE,
        auth=(s.privy_app_id, s.privy_app_secret),
        headers={"privy-app-id": s.privy_app_id},
        timeout=s.http_timeout_seconds,
        transport=_transport,
    )


def user_email(did: str) -> str | None:
    client = _client()
    if client is None:
        return None
    try:
        with client:
            r = client.get(f"/users/{did}")
        if r.status_code != 200:
            return None
        for account in r.json().get("linked_accounts", []):
            if account.get("type") == "email":
                return account.get("address")
    except (httpx.HTTPError, ValueError):
        logger.warning("privy_user_lookup_failed", extra={"provider": "privy"})
    return None


def delete_user(did: str) -> bool:
    client = _client()
    if client is None:
        return False
    try:
        with client:
            r = client.delete(f"/users/{did}")
        return r.status_code in (200, 204, 404)
    except httpx.HTTPError:
        logger.warning("privy_user_delete_failed", extra={"provider": "privy"})
        return False
