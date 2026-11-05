"""Sample statement: generated from the questionnaire, flagged, removable, never touching real imports."""

import uuid
from datetime import date

import httpx
import pytest

from app.economic_data import http as econ_http
from app.transactions.sample import MONTHS, build_sample
from tests.fixtures import monthly_statement_csv
from tests.mock_providers import ProviderMock


@pytest.fixture()
def providers():
    econ_http.set_transport(httpx.MockTransport(ProviderMock()))
    yield
    econ_http.set_transport(None)


def test_build_sample_matches_declared_basket():
    uid = uuid.uuid4()
    basket = {"housing": 1200.0, "groceries": 400.0, "energy": 100.0}
    p = build_sample(uid, "EUR", basket, date(2026, 11, 5))
    expenses = [r for r in p.rows if r.amount < 0]
    months = {r.booked_on.strftime("%Y-%m") for r in expenses}
    assert len(months) == MONTHS and max(months) == "2026-10"  # complete months only
    housing = [r for r in expenses if r.category == "housing"]
    assert len(housing) == MONTHS and all(float(-r.amount) == pytest.approx(1200) for r in housing)
    groceries = sum(float(-r.amount) for r in expenses if r.category == "groceries") / MONTHS
    assert groceries == pytest.approx(400, rel=0.1)
    assert any(r.merchant == "Carrefour" for r in expenses)  # local merchants for EUR
    assert p.normalization["sample"] is True
    # deterministic for a given user
    assert [r.amount for r in build_sample(uid, "EUR", basket, date(2026, 11, 5)).rows] == [
        r.amount for r in p.rows
    ]


def test_default_basket_when_no_answers():
    p = build_sample(uuid.uuid4(), "USD", None, date(2026, 11, 5))
    assert {r.category for r in p.rows if r.amount < 0} >= {"housing", "groceries", "energy"}


def test_sample_lifecycle(client, auth, providers):
    assert client.get("/api/v1/transactions/sample", headers=auth).json()["active"] is False
    r = client.post(
        "/api/v1/transactions/sample", headers=auth, json={"monthly": {"housing": 1500, "groceries": 400}}
    )
    assert r.status_code == 201 and r.json()["imported"] > 0 and r.json()["normalization"]["sample"]
    assert client.get("/api/v1/transactions/sample", headers=auth).json()["active"] is True
    assert client.get("/api/v1/pei", headers=auth).status_code == 200  # every feature works on the sample
    summary = client.get("/api/v1/expenses/summary", headers=auth).json()
    assert summary["has_data"] and summary["average_monthly_expenses"] == pytest.approx(1900, rel=0.05)

    # Loading again replaces the previous sample instead of doubling it
    n1 = r.json()["imported"]
    r2 = client.post("/api/v1/transactions/sample", headers=auth, json={})
    total = client.get("/api/v1/transactions", headers=auth, params={"limit": 1}).json()["total"]
    assert total == r2.json()["imported"] and r2.json()["imported"] != n1

    removed = client.delete("/api/v1/transactions/sample", headers=auth).json()["removed_transactions"]
    assert removed == total
    assert client.get("/api/v1/transactions", headers=auth, params={"limit": 1}).json()["total"] == 0
    assert client.get("/api/v1/transactions/sample", headers=auth).json()["active"] is False


def test_removing_sample_keeps_real_imports(client, auth, providers):
    real = client.post(
        "/api/v1/transactions/import",
        headers=auth,
        files={"file": ("s.csv", monthly_statement_csv(12), "text/csv")},
    ).json()["imported"]
    client.post("/api/v1/transactions/sample", headers=auth, json={})
    client.delete("/api/v1/transactions/sample", headers=auth)
    assert client.get("/api/v1/transactions", headers=auth, params={"limit": 1}).json()["total"] == real


def test_sample_is_private_to_its_owner(client, auth, other_auth, providers):
    client.post("/api/v1/transactions/sample", headers=auth, json={})
    assert client.get("/api/v1/transactions/sample", headers=other_auth).json()["active"] is False
    assert (
        client.delete("/api/v1/transactions/sample", headers=other_auth).json()["removed_transactions"] == 0
    )


def test_agent_tool_flags_sample_and_reads_onchain(client, auth, providers, monkeypatch):
    from sqlalchemy.orm import Session

    from app.ai import tools
    from app.database.session import get_engine
    from app.onchain import service
    from app.users.models import Profile

    client.post("/api/v1/transactions/sample", headers=auth, json={})
    monkeypatch.setattr(
        service,
        "onchain_reserves",
        lambda db, p: {
            "wallets": ["0xabc"],
            "verified": True,
            "chain": {"name": "Robinhood Chain"},
            "total": 9000.0,
            "total_usd": 9000.0,
            "base_currency": "USD",
            "holdings": [],
            "unpriced": [],
            "as_of": "x",
            "notes": [],
        },
    )
    monkeypatch.setattr(tools, "onchain_reserves", service.onchain_reserves)
    with Session(get_engine()) as db:
        profile = db.query(Profile).first()
        summary = tools.run_tool(db, profile, "get_expense_summary", "{}")
        assert summary["sample_data"] is True
        oc = tools.run_tool(db, profile, "get_onchain_reserves", "{}")
        assert oc["total"] == 9000.0 and oc["linked_wallets"] == 1
        cov = tools.run_tool(
            db,
            profile,
            "calculate_expense_coverage",
            '{"use_onchain_reserves": true, "reserves": 1, "currency": "USD", '
            '"inflation_shift_pp": 0, "shocks": []}',
        )
        assert cov["reserves"]["verified"] is True and cov["reserves"]["declared_amount"] == 9000.0
