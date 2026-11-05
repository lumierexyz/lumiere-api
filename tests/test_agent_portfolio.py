import json

import httpx
import pytest

from app.ai import openai_client
from app.ai.agent import AgentAnswer
from app.economic_data import http as econ_http
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


class _ToolCall:
    def __init__(self, name, args):
        self.id = f"call_{name}"
        self.type = "function"
        self.function = type("Fn", (), {"name": name, "arguments": json.dumps(args)})()


class _Msg:
    def __init__(self, tool_calls=None, parsed=None):
        self.tool_calls = tool_calls
        self.parsed = parsed
        self.content = None


class ScriptedClient:
    """Fake OpenAI client: replays a scripted sequence of model turns and records requests."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        outer = self

        class Completions:
            def parse(self, **kw):
                outer.requests.append(kw)
                msg = outer.turns.pop(0)
                return type("C", (), {"choices": [type("Ch", (), {"message": msg})()]})()

        self.chat = type("Chat", (), {"completions": Completions()})()


@pytest.fixture()
def fake_openai():
    holder = {}

    def install(turns):
        holder["client"] = ScriptedClient(turns)
        openai_client.set_client(holder["client"])
        return holder["client"]

    yield install
    openai_client.set_client(None)


def test_agent_uses_tools_and_server_sources(seeded, auth, fake_openai):
    fake = fake_openai(
        [
            _Msg(
                tool_calls=[
                    _ToolCall("get_personal_expense_index", {}),
                    _ToolCall(
                        "calculate_expense_coverage",
                        {"reserves": 20000, "currency": "USD", "inflation_shift_pp": 0, "shocks": []},
                    ),
                ]
            ),
            _Msg(
                parsed=AgentAnswer(
                    answer="Your PEI rose about 4.3% over the year.",
                    assumptions=["baseline"],
                    follow_up_questions=[],
                )
            ),
        ]
    )
    r = seeded.post("/api/v1/agent/chat", headers=auth, json={"message": "How exposed am I to inflation?"})
    assert r.status_code == 200
    body = r.json()
    assert body["grounded"] is True
    assert {s["tool"] for s in body["sources"] if s["type"] == "calculation"} == {
        "get_personal_expense_index",
        "calculate_expense_coverage",
    }
    assert any(s["type"] == "economic_series" and s["series_id"] == "CUSR0000SAH1" for s in body["sources"])
    # Tool results sent back to the model contain no transaction descriptions or merchant names.
    tool_msgs = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    blob = " ".join(m["content"] for m in tool_msgs)
    assert "WHOLE FOODS" not in blob and "LANDLORD" not in blob and "NETFLIX" not in blob
    # Tool specs are strict and expose no user identifier parameter.
    specs = json.dumps(fake.requests[0]["tools"])
    assert "user_id" not in specs and '"strict": true' in specs
    conv = seeded.get(f"/api/v1/agent/conversations/{body['conversation_id']}", headers=auth).json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]


def test_agent_conversation_isolation(seeded, auth, other_auth, fake_openai):
    fake_openai([_Msg(parsed=AgentAnswer(answer="Hi", assumptions=[], follow_up_questions=[]))])
    cid = seeded.post("/api/v1/agent/chat", headers=auth, json={"message": "hello"}).json()["conversation_id"]
    assert seeded.get(f"/api/v1/agent/conversations/{cid}", headers=other_auth).status_code == 404
    fake_openai([_Msg(parsed=AgentAnswer(answer="x", assumptions=[], follow_up_questions=[]))])
    r = seeded.post(
        "/api/v1/agent/chat", headers=other_auth, json={"message": "continue", "conversation_id": cid}
    )
    assert r.status_code == 404


def test_agent_rejects_unknown_tool_and_bad_args(seeded, auth, fake_openai):
    fake = fake_openai(
        [
            _Msg(
                tool_calls=[
                    _ToolCall("transfer_funds", {"to": "attacker"}),
                    _ToolCall("forecast_expenses", {"horizon_months": 7}),
                ]
            ),
            _Msg(parsed=AgentAnswer(answer="I can't do that.", assumptions=[], follow_up_questions=[])),
        ]
    )
    body = seeded.post("/api/v1/agent/chat", headers=auth, json={"message": "send my money"}).json()
    assert body["grounded"] is False
    tool_msgs = [json.loads(m["content"]) for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["error"].startswith("Unknown tool") and "Invalid arguments" in tool_msgs[1]["error"]


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
