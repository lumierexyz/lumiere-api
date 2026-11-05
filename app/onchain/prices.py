"""USD prices for the assets held on Robinhood Chain.

* ETH, BTC (for cbBTC), TAO: Coinbase public spot price.
* USDG: a USD stablecoin, valued at 1 USD.
* Stock tokens: the price of the underlying share (Yahoo Finance chart endpoint). Robinhood stock tokens
  track one share each, adjusted by the token's multiplier (see service.py); this is the reference price
  of the share, not a quote from an on-chain market.

A price that cannot be obtained is returned as None; the holding is then reported without a value and
left out of the total, never estimated.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.core.config import get_settings
from app.core.logging import logger

TTL_SECONDS = 300
_cache: dict[str, tuple[float, float, str]] = {}  # ref -> (fetched_at, price_usd, source)
_transport: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport


def clear_cache() -> None:
    _cache.clear()


def _fetch(client: httpx.Client, ref: str) -> tuple[float, str] | None:
    kind, _, sym = ref.partition(":")
    try:
        if kind == "usd":
            return 1.0, "fixed (USD stablecoin)"
        if kind == "coinbase":
            r = client.get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot")
            if r.status_code == 200:
                return float(r.json()["data"]["amount"]), "Coinbase spot"
        if kind == "yahoo":
            r = client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"range": "1d", "interval": "1d"},
            )
            if r.status_code == 200:
                meta = r.json()["chart"]["result"][0]["meta"]
                if meta.get("currency") == "USD" and meta.get("regularMarketPrice"):
                    return float(meta["regularMarketPrice"]), "Yahoo Finance (underlying share)"
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        pass
    logger.warning("price_unavailable", extra={"provider": kind})
    return None


def usd_prices(refs: set[str]) -> dict[str, tuple[float, str] | None]:
    now = time.time()
    out: dict[str, tuple[float, str] | None] = {}
    todo = []
    for ref in refs:
        hit = _cache.get(ref)
        if hit and now - hit[0] < TTL_SECONDS:
            out[ref] = (hit[1], hit[2])
        else:
            todo.append(ref)
    if todo:
        with (
            httpx.Client(
                timeout=get_settings().http_timeout_seconds,
                transport=_transport,
                headers={"User-Agent": "Mozilla/5.0 (compatible; lumiere-backend/0.1)"},
            ) as client,
            ThreadPoolExecutor(max_workers=6) as pool,
        ):
            for ref, res in zip(todo, pool.map(lambda r: _fetch(client, r), todo), strict=True):
                out[ref] = res
                if res is not None:
                    _cache[ref] = (now, res[0], res[1])
    return out
