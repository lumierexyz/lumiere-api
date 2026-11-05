"""Read-only JSON-RPC access to Robinhood Chain (chain id 4663).

All reads for one request go out as JSON-RPC *batches*: the public RPC sits behind Cloudflare, which
answers bursts of parallel requests with an HTML challenge (HTTP 403) instead of JSON. One batched POST
per step avoids that; a 403/429/5xx is retried with a short backoff.
"""

import time

import httpx

from app.core.config import get_settings
from app.core.errors import UpstreamError
from app.core.logging import logger

CHAIN_ID = 4663
BATCH_SIZE = 60

_transport: httpx.BaseTransport | None = None  # tests inject httpx.MockTransport here


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport


def _post(client: httpx.Client, url: str, payload: list[dict], sleep=time.sleep) -> list[dict]:
    retries = get_settings().http_max_retries
    last = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            r = client.post(url, json=payload)
        except httpx.TransportError as exc:
            last = type(exc).__name__
        else:
            if r.status_code == 200:
                try:
                    body = r.json()
                except ValueError:
                    last = "non-JSON response"  # Cloudflare challenge page
                else:
                    if isinstance(body, list):
                        return body
                    last = "unexpected response"
            else:
                last = f"HTTP {r.status_code}"
                if r.status_code not in (403, 429, 500, 502, 503, 504):
                    break
        logger.warning("rpc_retry", extra={"provider": "robinhood-chain", "attempt": attempt})
        if attempt < retries:
            sleep(1.5 * attempt)
    raise UpstreamError(
        f"Robinhood Chain RPC is unavailable ({last}).", details={"provider": "robinhood-chain"}
    )


def batch(calls: list[tuple[str, list]]) -> list[str | None]:
    """Run ``(method, params)`` calls; returns each hex result, or None when that call failed."""
    s = get_settings()
    out: list[str | None] = [None] * len(calls)
    with httpx.Client(
        timeout=s.http_timeout_seconds,
        transport=_transport,
        headers={"User-Agent": "lumiere-backend/0.1", "Content-Type": "application/json"},
    ) as client:
        for start in range(0, len(calls), BATCH_SIZE):
            chunk = calls[start : start + BATCH_SIZE]
            payload = [
                {"jsonrpc": "2.0", "id": start + i, "method": m, "params": p}
                for i, (m, p) in enumerate(chunk)
            ]
            for item in _post(client, s.robinhood_rpc_url, payload):
                idx = item.get("id")
                if isinstance(idx, int) and 0 <= idx < len(out) and isinstance(item.get("result"), str):
                    out[idx] = item["result"]
    return out


def balance_of_data(holder: str) -> str:
    return "0x70a08231" + holder.lower().removeprefix("0x").rjust(64, "0")


def to_int(hex_value: str | None) -> int | None:
    if not hex_value or hex_value == "0x":
        return None
    try:
        return int(hex_value, 16)
    except ValueError:
        return None


MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"  # deployed on Robinhood Chain (verified)
SEL_AGGREGATE3 = "82ad56cb"  # aggregate3((address,bool,bytes)[])
SEL_GET_ETH_BALANCE = "0x4d2301cc"  # getEthBalance(address)


def _word(n: int) -> str:
    return format(n, "064x")


def _encode_aggregate3(calls: list[tuple[str, str]]) -> str:
    """ABI-encode aggregate3 with allowFailure=true for every call."""
    tuples = []
    for target, data in calls:
        body = bytes.fromhex(data.removeprefix("0x"))
        padded = body.hex() + "00" * ((32 - len(body) % 32) % 32)
        tuples.append(
            target.lower().removeprefix("0x").rjust(64, "0")
            + _word(1)
            + _word(0x60)
            + _word(len(body))
            + padded
        )
    offsets, pos = [], 32 * len(tuples)
    for t in tuples:
        offsets.append(_word(pos))
        pos += len(t) // 2
    return "0x" + SEL_AGGREGATE3 + _word(0x20) + _word(len(tuples)) + "".join(offsets) + "".join(tuples)


def _decode_aggregate3(hex_result: str) -> list[str | None]:
    raw = bytes.fromhex(hex_result.removeprefix("0x"))

    def word(at: int) -> int:
        return int.from_bytes(raw[at : at + 32], "big")

    arr = word(0)
    n = word(arr)
    base = arr + 32
    out: list[str | None] = []
    for i in range(n):
        t = base + word(base + 32 * i)
        ok = word(t) == 1
        b = t + word(t + 32)
        length = word(b)
        out.append("0x" + raw[b + 32 : b + 32 + length].hex() if ok else None)
    return out


def multicall(calls: list[tuple[str, str]], chunk: int = 400) -> list[str | None]:
    """Run ``(target, calldata)`` reads through Multicall3: one eth_call per ``chunk`` reads.
    Returns each call's return data, or None when that call reverted."""
    out: list[str | None] = []
    for start in range(0, len(calls), chunk):
        part = calls[start : start + chunk]
        (res,) = batch([("eth_call", [{"to": MULTICALL3, "data": _encode_aggregate3(part)}, "latest"])])
        if res is None:
            raise UpstreamError("Robinhood Chain returned no data.", details={"provider": "robinhood-chain"})
        out += _decode_aggregate3(res)
    return out
