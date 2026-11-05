import json

import httpx
import pytest

from app.ai import claude_client
from app.economic_data import http as econ_http
from tests.fake_claude import FakeClaude, message, text_block, tool_use
from tests.fixtures import monthly_statement_csv
from tests.mock_providers import ProviderMock


@pytest.fixture()
def seeded(client, auth):
    econ_http.set_transport(httpx.MockTransport(ProviderMock()))
    client.post(
        "/api/v1/transactions/import",
        headers=auth,
        files={"file": ("s.csv", monthly_statement_csv(12), "text/csv")},
    )
    yield client
    econ_http.set_transport(None)


# ---------------------------------------------------------------- agent


def answer(text, assumptions=()):
    return message(text_block({"answer": text, "assumptions": list(assumptions), "follow_up_questions": []}))


@pytest.fixture()
def fake_claude():
    def install(turns):
        fake = FakeClaude(list(turns))
        claude_client.set_client(fake)
        return fake

    yield install
    claude_client.set_client(None)


def tool_results(request) -> list[dict]:
    """tool_result blocks of the last user message of a recorded request."""
    return [b for b in request["messages"][-1]["content"] if b.get("type") == "tool_result"]


def test_agent_uses_tools_and_server_sources(seeded, auth, fake_claude):
    fake = fake_claude(
        [
            message(
                tool_use("get_personal_expense_index", {}, 0),
                tool_use(
                    "calculate_expense_coverage",
                    {"reserves": 20000, "currency": "USD", "inflation_shift_pp": 0, "shocks": []},
                    1,
                ),
            ),
            answer("Your PEI rose about 4.3% over the year.", ["baseline"]),
        ]
    )
    r = seeded.post("/api/v1/agent/chat", headers=auth, json={"message": "How exposed am I to inflation?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"].startswith("Your PEI") and body["grounded"] is True
    assert {s["tool"] for s in body["sources"] if s["type"] == "calculation"} == {
        "get_personal_expense_index",
        "calculate_expense_coverage",
    }
    assert any(s["type"] == "economic_series" and s["series_id"] == "CUSR0000SAH1" for s in body["sources"])
    second = fake.requests[1]
    # All tool results come back in one user message, after the assistant turn appended unchanged.
    assert second["messages"][-2]["role"] == "assistant"
    results = tool_results(second)
    assert [r["tool_use_id"] for r in results] == [
        "toolu_get_personal_expense_index_0",
        "toolu_calculate_expense_coverage_1",
    ]
    blob = " ".join(r["content"] for r in results)
    assert "WHOLE FOODS" not in blob and "LANDLORD" not in blob and "NETFLIX" not in blob
    # Strict tool schemas, no user identifier parameter, fallback enabled, structured final answer.
    first = fake.requests[0]
    specs = json.dumps(first["tools"])
    assert "user_id" not in specs and all(t["strict"] is True for t in first["tools"])
    assert first["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in first["betas"]
    assert first["output_config"]["format"]["type"] == "json_schema"
    conv = seeded.get(f"/api/v1/agent/conversations/{body['conversation_id']}", headers=auth).json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]


def test_agent_conversation_isolation(seeded, auth, other_auth, fake_claude):
    fake_claude([answer("Hi")])
    cid = seeded.post("/api/v1/agent/chat", headers=auth, json={"message": "hello"}).json()["conversation_id"]
    assert seeded.get(f"/api/v1/agent/conversations/{cid}", headers=other_auth).status_code == 404
    fake_claude([answer("x")])
    r = seeded.post(
        "/api/v1/agent/chat", headers=other_auth, json={"message": "continue", "conversation_id": cid}
    )
    assert r.status_code == 404


def test_agent_rejects_unknown_tool_and_bad_args(seeded, auth, fake_claude):
    fake = fake_claude(
        [
            message(
                tool_use("transfer_funds", {"to": "attacker"}, 0),
                tool_use("forecast_expenses", {"horizon_months": 7}, 1),
            ),
            answer("I can't do that."),
        ]
    )
    body = seeded.post("/api/v1/agent/chat", headers=auth, json={"message": "send my money"}).json()
    assert body["grounded"] is False
    results = tool_results(fake.requests[1])
    assert all(r["is_error"] for r in results)
    assert json.loads(results[0]["content"])["error"].startswith("Unknown tool")
    assert "Invalid arguments" in json.loads(results[1]["content"])["error"]


def test_agent_refusal_is_handled(seeded, auth, fake_claude):
    fake_claude([message(stop_reason="refusal")])
    body = seeded.post("/api/v1/agent/chat", headers=auth, json={"message": "something off-policy"}).json()
    assert body["answer"].startswith("I can't help with that request")


def test_agent_unavailable_without_key(seeded, auth):
    r = seeded.post("/api/v1/agent/chat", headers=auth, json={"message": "hi"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "ai_unavailable"


# ---------------------------------------------------------------- portfolio


def test_portfolio_simulation_constraints(seeded, auth, other_auth):
    r = seeded.post(
        "/api/v1/portfolio/simulate",
        headers=auth,
        json={
            "budget": 10000,
            "max_weight": 0.5,
            "n_scenarios": 1000,
            "stress_scenarios": [{"name": "energy spike", "inflation": {"energy": 0.4}}],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["simulation"] is True and "HYPOTHETICAL" in body["label"]
    weights = [a["weight"] for a in body["allocation"]]
    assert sum(weights) == pytest.approx(1, abs=1e-3)
    assert all(0 <= w <= 0.5 + 1e-3 for w in weights)
    assert all(a["instrument"].startswith("HYP_") for a in body["allocation"])
    assert body["metrics"]["tracking_error"] <= body["benchmarks"]["all_cash"]["tracking_error"] + 1e-9
    assert body["metrics"]["hedge_effectiveness_vs_cash"] > 0
    assert body["scenario_distribution"]["n_scenarios"] == 1001
    assert seeded.get(f"/api/v1/portfolio/simulations/{body['id']}", headers=other_auth).status_code == 404


def test_portfolio_validation(seeded, auth):
    r = seeded.post("/api/v1/portfolio/simulate", headers=auth, json={"budget": -5})
    assert r.status_code == 422
