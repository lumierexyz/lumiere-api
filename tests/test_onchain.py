"""On-chain reserves (Robinhood Chain): Multicall3 encoding, balances, multipliers, valuation, coverage."""

import pytest

from app.onchain import prices, rpc, service
from tests.fixtures import monthly_statement_csv

WALLET = "0x1111111111111111111111111111111111111111"
NVDA = "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"


def _word(n: int) -> bytes:
    return n.to_bytes(32, "big")


def encode_results(results: list[bytes | None]) -> str:
    """Encode an aggregate3 return value: (bool success, bytes returnData)[]."""
    tuples = []
    for r in results:
        data = r or b""
        pad = data + b"\0" * ((32 - len(data) % 32) % 32)
        tuples.append(_word(1 if r is not None else 0) + _word(0x40) + _word(len(data)) + pad)
    offsets, pos = b"", 32 * len(tuples)
    for t in tuples:
        offsets += _word(pos)
        pos += len(t)
    return "0x" + (_word(0x20) + _word(len(tuples)) + offsets + b"".join(tuples)).hex()


def test_aggregate3_roundtrip():
    calls = [(NVDA, rpc.balance_of_data(WALLET)), (USDG, "0xa60bf13d")]
    enc = rpc._encode_aggregate3(calls)
    assert enc.startswith("0x82ad56cb")
    # 4 head words + per tuple: target, flag, offset, length, padded data
    assert len(bytes.fromhex(enc[10:])) == 32 * 4 + 32 * (4 + 2) + 32 * (4 + 1)
    decoded = rpc._decode_aggregate3(encode_results([_word(5), None]))
    assert decoded == ["0x" + _word(5).hex(), None]


@pytest.fixture()
def chain(monkeypatch):
    """Fake chain: 2 ETH, 3 raw NVDA tokens with a 1.5 multiplier, 250 USDG (6 decimals)."""
    balances = {None: 2 * 10**18, NVDA.lower(): 3 * 10**18, USDG.lower(): 250 * 10**6}

    def fake_multicall(calls, chunk=400):
        out = []
        for target, data in calls:
            if target == rpc.MULTICALL3 and data.startswith(rpc.SEL_GET_ETH_BALANCE):
                out.append("0x" + _word(balances[None]).hex())
            elif data.startswith("0x70a08231"):
                out.append("0x" + _word(balances.get(target.lower(), 0)).hex())
            elif data in (service.SEL_MULTIPLIER, service.SEL_NEW_MULTIPLIER):
                out.append("0x" + _word(15 * 10**17).hex())
            elif data == service.SEL_EFFECTIVE_AT:
                out.append("0x" + _word(1).hex())
            else:
                out.append(None)
        return out

    monkeypatch.setattr(rpc, "multicall", fake_multicall)
    monkeypatch.setattr(
        prices,
        "usd_prices",
        lambda refs: {
            r: {
                "coinbase:ETH": (2000.0, "Coinbase spot"),
                "yahoo:NVDA": (100.0, "Yahoo"),
                "usd": (1.0, "fixed"),
            }.get(r)
            for r in refs
        },
    )
    monkeypatch.setattr(service.privy_admin, "user_wallets", lambda did: [WALLET])


def test_wallet_holdings_applies_multiplier_and_decimals(chain):
    h = {x["symbol"]: x for x in service.wallet_holdings([WALLET])}
    assert set(h) == {"ETH", "NVDA", "USDG"}
    assert h["ETH"]["amount"] == pytest.approx(2)
    assert h["NVDA"]["amount"] == pytest.approx(4.5) and h["NVDA"]["multiplier"] == pytest.approx(1.5)
    assert h["USDG"]["amount"] == pytest.approx(250)


def test_onchain_reserves_endpoint(client, auth, chain):
    r = client.get("/api/v1/reserves/onchain", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["verified"] and body["wallets"] == [WALLET] and body["chain"]["id"] == 4663
    assert body["total_usd"] == pytest.approx(2 * 2000 + 4.5 * 100 + 250)
    assert body["total"] == pytest.approx(body["total_usd"])  # base currency USD
    assert [h["symbol"] for h in body["holdings"]] == ["ETH", "NVDA", "USDG"]  # by value


def test_no_linked_wallet(client, auth, monkeypatch):
    monkeypatch.setattr(service.privy_admin, "user_wallets", lambda did: [])
    body = client.get("/api/v1/reserves/onchain", headers=auth).json()
    assert body["verified"] is False and body["holdings"] == [] and body["total_usd"] == 0


def test_privy_unreachable_is_an_error(client, auth, monkeypatch):
    monkeypatch.setattr(service.privy_admin, "user_wallets", lambda did: None)
    r = client.get("/api/v1/reserves/onchain", headers=auth)
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unavailable"


def test_unpriced_asset_is_listed_not_counted(client, auth, chain, monkeypatch):
    monkeypatch.setattr(
        prices, "usd_prices", lambda refs: {r: (None if r == "yahoo:NVDA" else (1.0, "x")) for r in refs}
    )
    body = client.get("/api/v1/reserves/onchain", headers=auth).json()
    assert body["unpriced"] == ["NVDA"]
    assert body["total_usd"] == pytest.approx(2 + 250)


def test_coverage_with_onchain_reserves_is_verified(client, auth, chain, monkeypatch):
    import httpx

    from app.economic_data import http as econ_http
    from tests.mock_providers import ProviderMock

    econ_http.set_transport(httpx.MockTransport(ProviderMock()))
    try:
        client.post(
            "/api/v1/transactions/import",
            headers=auth,
            files={"file": ("s.csv", monthly_statement_csv(12), "text/csv")},
        )
        r = client.post("/api/v1/coverage/calculate", headers=auth, json={"reserves_source": "onchain"})
    finally:
        econ_http.set_transport(None)
    assert r.status_code == 200, r.text
    res = r.json()["reserves"]
    assert res["verified"] is True and res["declared_amount"] == pytest.approx(4700)
    assert res["onchain"]["wallets"] == [WALLET]


def test_coverage_onchain_without_wallet(client, auth, monkeypatch):
    monkeypatch.setattr(service.privy_admin, "user_wallets", lambda did: [])
    client.post(
        "/api/v1/transactions/import",
        headers=auth,
        files={"file": ("s.csv", monthly_statement_csv(12), "text/csv")},
    )
    r = client.post("/api/v1/coverage/calculate", headers=auth, json={"reserves_source": "onchain"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "no_linked_wallet"


def test_coverage_declared_requires_amount(client, auth):
    r = client.post("/api/v1/coverage/calculate", headers=auth, json={})
    assert r.status_code == 422
