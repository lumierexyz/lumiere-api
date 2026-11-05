"""Verified on-chain reserves: balances of the wallets linked to the user's account on Robinhood Chain.

Only wallets linked to the user's Privy account are read. Linking requires a signature from the wallet
(Sign-In With Ethereum), so ownership is proven; an address typed by the user is never accepted.

Stock tokens carry a share multiplier (corporate actions such as splits and dividends adjust it). The
number of shares a balance represents is ``balance * multiplier / 1e18``. The contract exposes the
current multiplier, a pending one and the time the pending one takes effect; the pending one is used
once that time has passed.
"""

import json
import time
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

from app.auth import privy_admin
from app.core.errors import AppError
from app.economic_data.service import conversion_rate, refresh_fx
from app.onchain import prices, rpc

CHAIN = {"id": rpc.CHAIN_ID, "name": "Robinhood Chain", "explorer": "https://robinhoodchain.blockscout.com"}
SEL_MULTIPLIER = "0xa60bf13d"
SEL_NEW_MULTIPLIER = "0xdc767007"
SEL_EFFECTIVE_AT = "0x97a4064f"
ONE = 10**18


@lru_cache(maxsize=1)
def assets() -> list[dict]:
    return json.loads((Path(__file__).parent / "assets.json").read_text(encoding="utf-8"))


def _multipliers(tokens: list[str]) -> dict[str, float]:
    sels = (SEL_MULTIPLIER, SEL_NEW_MULTIPLIER, SEL_EFFECTIVE_AT)
    res = rpc.multicall([(t, sel) for t in tokens for sel in sels])
    out = {}
    now = int(time.time())
    for i, t in enumerate(tokens):
        cur, new, at = (rpc.to_int(x) for x in res[3 * i : 3 * i + 3])
        m = new if (new and at is not None and now >= at) else cur
        out[t] = (m / ONE) if m else 1.0
    return out


def wallet_holdings(addresses: list[str]) -> list[dict]:
    """Per-asset amounts summed over ``addresses`` (amounts in units; stock tokens in shares)."""
    if not addresses:
        return []
    reg = assets()
    calls = []
    for a in addresses:
        for asset in reg:
            if asset["address"] is None:
                calls.append(
                    (rpc.MULTICALL3, rpc.SEL_GET_ETH_BALANCE + a.lower().removeprefix("0x").rjust(64, "0"))
                )
            else:
                calls.append((asset["address"], rpc.balance_of_data(a)))
    res = rpc.multicall(calls)
    raw: dict[str, int] = {}
    for i, value in enumerate(res):
        v = rpc.to_int(value)
        if v:
            sym = reg[i % len(reg)]["symbol"]
            raw[sym] = raw.get(sym, 0) + v
    by_sym = {x["symbol"]: x for x in reg}
    equities = [by_sym[s]["address"] for s in raw if by_sym[s]["kind"] == "equity"]
    mult = _multipliers(equities) if equities else {}
    out = []
    for sym, units in raw.items():
        asset = by_sym[sym]
        row = {
            "symbol": sym,
            "name": asset["name"],
            "kind": asset["kind"],
            "address": asset["address"],
            "amount": units / 10 ** asset["decimals"],
            "price_ref": asset["price"],
        }
        if asset["kind"] == "equity":
            row["multiplier"] = mult.get(asset["address"], 1.0)
            row["amount"] *= row["multiplier"]  # shares
        out.append(row)
    return out


def _to_base(db: Session, base: str) -> dict | None:
    today = date.today()
    conv = conversion_rate(db, "USD", base, today)
    if conv is None and base != "USD":
        try:
            refresh_fx(db, base)
        except Exception:  # noqa: BLE001 - FX refresh is best effort; the USD total is still returned
            return None
        conv = conversion_rate(db, "USD", base, today)
    return conv


def onchain_reserves(db: Session, profile) -> dict:
    wallets = privy_admin.user_wallets(profile.auth_subject) if profile.auth_subject else []
    if wallets is None:
        raise AppError(
            "Your linked wallets could not be read. Please try again.",
            status_code=502,
            code="upstream_unavailable",
        )
    holdings = wallet_holdings(wallets)
    quotes = prices.usd_prices({h["price_ref"] for h in holdings if h["price_ref"]})
    conv = _to_base(db, profile.base_currency) if holdings else None
    total_usd = 0.0
    unpriced = []
    for h in holdings:
        q = quotes.get(h.pop("price_ref")) if h.get("price_ref") is not None else None
        if q is None:
            h.update(price_usd=None, price_source=None, value_usd=None, value=None)
            unpriced.append(h["symbol"])
            continue
        h["price_usd"], h["price_source"] = q
        h["value_usd"] = round(h["amount"] * q[0], 2)
        h["value"] = round(h["value_usd"] * conv["rate"], 2) if conv else None
        total_usd += h["value_usd"]
    holdings.sort(key=lambda h: h["value_usd"] or 0, reverse=True)
    return {
        "chain": CHAIN,
        "wallets": wallets,
        "holdings": holdings,
        "unpriced": unpriced,
        "total_usd": round(total_usd, 2),
        "total": round(total_usd * conv["rate"], 2) if conv else (0.0 if not holdings else None),
        "base_currency": profile.base_currency,
        "conversion": conv,
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "verified": bool(wallets),
        "notes": [
            "Only wallets linked to your account by signature are read; nothing can be moved from here.",
            "Stock tokens are valued at the price of the underlying share, adjusted by the token multiplier.",
            "Assets without an available price are listed but not counted.",
        ],
    }
