import httpx
import pytest

from app.economic_data import http as econ_http
from app.economic_data.mapping import SeriesSpec
from app.economic_data.service import monthly_index, refresh_series
from tests.fixtures import monthly_statement_csv
from tests.mock_providers import ProviderMock


@pytest.fixture()
def providers():
    mock = ProviderMock()
    econ_http.set_transport(httpx.MockTransport(mock))
    yield mock
    econ_http.set_transport(None)


@pytest.fixture()
def seeded(client, auth, providers):
    r = client.post(
        "/api/v1/transactions/import",
        headers=auth,
        files={"file": ("s.csv", monthly_statement_csv(12), "text/csv")},
    )
    assert r.status_code == 201
    return client


# ---------------------------------------------------------------- economic data


def test_fred_missing_values_skipped_and_cached(db, providers):
    spec = SeriesSpec("fred", "CPIAUCSL", "US", "headline")
    row = refresh_series(db, spec)
    s = monthly_index(db, row)
    assert row.status == "ok" and len(s) == 105  # the "." observation was dropped, not filled
    n_calls = len(providers.calls)
    refresh_series(db, spec)  # within TTL: served from the database
    assert len(providers.calls) == n_calls


def test_retry_then_success_and_non_retryable_error(db, providers, monkeypatch):
    monkeypatch.setattr("app.economic_data.http.time.sleep", lambda *_: None)
    providers.flaky["CUSR0000SAH1"] = 1
    ok = refresh_series(db, SeriesSpec("fred", "CUSR0000SAH1", "US", "housing"))
    assert ok.status == "ok"
    providers.failing.add("BADSERIES")
    bad = refresh_series(db, SeriesSpec("fred", "BADSERIES", "US", "housing"))
    assert bad.status == "error" and "HTTP 400" in bad.error
    assert monthly_index(db, bad).empty  # no fabricated data


def test_series_endpoint_reports_mapping(seeded, auth):
    r = seeded.get("/api/v1/economic-data/series", headers=auth).json()
    assert r["supported"] and r["source"] == "fred"
    ins = next(s for s in r["series"] if s["category"] == "insurance")
    assert ins["proxy"] is True
    other = next(s for s in r["series"] if s["category"] == "other")
    assert other["fallback"]


def test_unsupported_country_is_explicit(seeded, auth):
    seeded.patch("/api/v1/me", headers=auth, json={"country": "BR"})
    r = seeded.get("/api/v1/pei", headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_geography"


def test_eurostat_geography(client, auth, providers):
    client.patch("/api/v1/me", headers=auth, json={"country": "FR", "base_currency": "EUR"})
    raw = monthly_statement_csv(12).replace(b",USD", b",EUR")
    client.post("/api/v1/transactions/import", headers=auth, files={"file": ("s.csv", raw, "text/csv")})
    r = client.get("/api/v1/pei", headers=auth)
    assert r.status_code == 200
    assert any("europa.eu" in c and "coicop18=CP041" in c for c in providers.calls)
    assert r.json()["year_over_year"] == pytest.approx(0.02, abs=1e-3)


# ---------------------------------------------------------------- PEI


def test_pei_weighted_inflation(seeded, auth):
    r = seeded.get("/api/v1/pei", headers=auth).json()
    # Basket: housing 1500, groceries 400, energy 120, subscriptions 15.49 (monthly)
    w = {"housing": 1500, "groceries": 400, "energy": 120, "subscriptions": 15.49}
    total = sum(w.values())
    rates = {"housing": 0.04, "groceries": 0.02, "energy": 0.10, "subscriptions": 0.01}
    expected = sum(w[c] / total * rates[c] for c in w)
    assert r["year_over_year"] == pytest.approx(expected, abs=2e-3)
    assert r["headline_year_over_year"] == pytest.approx(0.03, abs=1e-3)
    assert r["base_month"] == "2025-01" and r["basket_coverage"] == 1.0
    assert "initial_link_uses_contemporaneous_weights" in r["flags"]
    comp = seeded.get("/api/v1/pei/composition", headers=auth).json()
    assert comp["components"][0]["category"] == "housing"
    assert sum(c["weight"] for c in comp["components"]) == pytest.approx(1, abs=1e-3)
    hist = seeded.get("/api/v1/pei/history", headers=auth).json()
    assert hist["items"][0]["pei"] == 100.0


def test_pei_no_lookahead(client, auth, providers):
    first = monthly_statement_csv(12)
    client.post("/api/v1/transactions/import", headers=auth, files={"file": ("a.csv", first, "text/csv")})
    before = {i["month"]: i["pei"] for i in client.get("/api/v1/pei/history", headers=auth).json()["items"]}
    # A later year with a completely different basket must not alter already-published months.
    later = b"Date,Description,Amount\n" + b"".join(
        f"2026-{m:02d}-03,SHELL FUEL,-900.00\n".encode() for m in range(1, 10)
    )
    client.post("/api/v1/transactions/import", headers=auth, files={"file": ("b.csv", later, "text/csv")})
    after = {i["month"]: i["pei"] for i in client.get("/api/v1/pei/history", headers=auth).json()["items"]}
    for m in [k for k in before if k.startswith("2025")]:
        assert after[m] == pytest.approx(before[m], abs=1e-9)


def test_pei_missing_category_series_renormalises(client, auth, providers):
    providers.failing.add("CUSR0000SA0E")  # energy index unavailable
    client.post(
        "/api/v1/transactions/import",
        headers=auth,
        files={"file": ("s.csv", monthly_statement_csv(12), "text/csv")},
    )
    r = client.get("/api/v1/pei", headers=auth).json()
    assert r["basket_coverage"] == pytest.approx(1 - 120 / 2035.49, abs=1e-3)
    energy = next(s for s in r["sources"] if s["category"] == "energy")
    assert energy["status"] == "error"


# ---------------------------------------------------------------- forecasts


def test_forecast_scenarios_and_shocks(seeded, auth):
    r = seeded.post(
        "/api/v1/forecasts",
        headers=auth,
        json={
            "horizon_months": 12,
            "scenarios": [
                {"name": "baseline"},
                {"name": "hot", "inflation_shift_pp": 3},
                {"name": "energy_shock", "shocks": {"energy": 0.2}},
            ],
        },
    )
    assert r.status_code == 201
    body = r.json()
    sc = {s["scenario"]: s for s in body["scenarios"]}
    assert sc["hot"]["expected_total"] > sc["baseline"]["expected_total"]
    diff = sc["energy_shock"]["expected_total"] - sc["baseline"]["expected_total"]
    energy_base = sc["baseline"]["by_category_total"]["energy"]
    assert diff == pytest.approx(0.2 * energy_base, rel=1e-3)
    housing_m12 = sc["baseline"]["monthly"][11]["by_category"]["housing"]
    base_housing = body["baseline_monthly_by_category"]["housing"]
    assert housing_m12 == pytest.approx(base_housing * 1.04, rel=2e-3)
    assert "uncertainty_80pct" in sc["baseline"]
    got = seeded.get(f"/api/v1/forecasts/{body['id']}", headers=auth)
    assert got.status_code == 200 and got.json()["horizon_months"] == 12


def test_forecast_validation_and_ownership(seeded, auth, other_auth):
    r = seeded.post("/api/v1/forecasts", headers=auth, json={"horizon_months": 5})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    fid = seeded.post("/api/v1/forecasts", headers=auth, json={}).json()["id"]
    assert seeded.get(f"/api/v1/forecasts/{fid}", headers=other_auth).status_code == 404


def test_forecast_without_history(client, auth, providers):
    r = client.post("/api/v1/forecasts", headers=auth, json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "insufficient_data"


# ---------------------------------------------------------------- coverage


def test_coverage_flat_prices_matches_nominal(client, auth, providers):
    client.patch("/api/v1/me", headers=auth, json={"country": "BR"})  # no index -> explicit assumption
    client.post(
        "/api/v1/transactions/import",
        headers=auth,
        files={"file": ("s.csv", monthly_statement_csv(12), "text/csv")},
    )
    r = client.post(
        "/api/v1/coverage/calculate",
        headers=auth,
        json={
            "reserves": 10000,
            "currency": "USD",
            "assumed_annual_inflation": 0.0,
            "scenarios": [{"name": "flat"}],
        },
    ).json()
    assert r["nominal"]["months"] == pytest.approx(10000 / 2035.49, abs=0.01)
    assert r["projected"][0]["months"] == pytest.approx(r["nominal"]["months"], abs=0.01)
    assert r["projected"][0]["days"] == pytest.approx(r["projected"][0]["months"] * 30.436875, abs=0.2)
    assert r["reserves"]["verified"] is False


def test_coverage_inflation_reduces_months(seeded, auth):
    r = seeded.post(
        "/api/v1/coverage/calculate", headers=auth, json={"reserves": 100000, "currency": "USD"}
    ).json()
    proj = {p["scenario"]: p for p in r["projected"]}
    assert proj["higher_inflation"]["months"] < proj["baseline"]["months"] < proj["lower_inflation"]["months"]
    assert proj["baseline"]["months"] < r["nominal"]["months"]
    shares = sum(c["share"] for c in proj["baseline"]["by_category"])
    assert shares == pytest.approx(1, abs=1e-3)


def test_coverage_requires_rate_for_foreign_reserves(seeded, auth):
    r = seeded.post("/api/v1/coverage/calculate", headers=auth, json={"reserves": 1000, "currency": "EUR"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "conversion_unavailable"
