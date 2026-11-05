"""Concurrency and error-path behaviour seen in production."""

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.economic_data import service
from app.economic_data.mapping import SeriesSpec
from app.economic_data.models import EconomicSeries


def test_refresh_race_uses_the_winner_row(db, monkeypatch):
    spec = SeriesSpec("fred", "CPIAUCSL", "US", "headline")
    winner = EconomicSeries(
        source="fred", series_code="CPIAUCSL", geography="US", category="headline", status="ok"
    )
    db.add(winner)
    db.commit()

    def lose(*_):
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    monkeypatch.setattr(service, "_refresh_series", lose)
    assert service.refresh_series(db, spec).id == winner.id


def test_unexpected_error_keeps_cors_headers(app, auth, monkeypatch):
    from app.expenses import routes

    def boom(*_, **__):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(routes.analytics, "summary", boom)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/v1/expenses/summary", headers={**auth, "Origin": "http://localhost:3000"})
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
