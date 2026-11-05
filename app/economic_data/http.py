"""Resilient HTTP GET for public data providers: timeouts, bounded retries with exponential backoff,
``Retry-After`` on 429, and errors that never echo URLs containing API keys."""

import time

import httpx

from app.core.config import get_settings
from app.core.errors import UpstreamError
from app.core.logging import logger

_transport: httpx.BaseTransport | None = None  # tests inject httpx.MockTransport here


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport


def get_json(provider: str, url: str, params: dict, *, sleep=time.sleep) -> dict:
    s = get_settings()
    last_error = "unknown error"
    with httpx.Client(
        timeout=s.http_timeout_seconds, transport=_transport, headers={"User-Agent": "lumiere-backend/0.1"}
    ) as client:
        for attempt in range(1, s.http_max_retries + 1):
            try:
                r = client.get(url, params=params)
            except httpx.TransportError as exc:
                last_error = type(exc).__name__
            else:
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        last_error = "invalid JSON"
                        break
                if r.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {r.status_code}"
                    retry_after = r.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit() and attempt < s.http_max_retries:
                        sleep(min(int(retry_after), 30))
                        continue
                else:
                    # 4xx other than 429: not retryable (bad series id, bad key...)
                    raise UpstreamError(
                        f"{provider} rejected the request (HTTP {r.status_code}).",
                        details={"provider": provider, "status": r.status_code},
                    )
            logger.warning("provider_retry", extra={"provider": provider, "attempt": attempt})
            if attempt < s.http_max_retries:
                sleep(min(2 ** (attempt - 1), 8))
    raise UpstreamError(f"{provider} is unavailable ({last_error}).", details={"provider": provider})
