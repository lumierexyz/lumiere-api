"""httpx MockTransport emulating FRED and Eurostat with deterministic synthetic index paths."""

import json

import httpx

from tests.fixtures import monthly_index

# Annual growth used to synthesise each FRED series (test fixture, not real data).
FRED_RATES = {
    "CPIAUCSL": 0.03,
    "CUSR0000SAH1": 0.04,
    "CUSR0000SAF11": 0.02,
    "CUSR0000SEFV": 0.035,
    "CUSR0000SAT": 0.025,
    "CUSR0000SA0E": 0.10,
    "CUSR0000SAM": 0.03,
    "CUSR0000SETE": 0.05,
    "CUSR0000SAE2": 0.01,
    "CUSR0000SAE1": 0.03,
    "CUSR0000SAR": 0.02,
}


class ProviderMock:
    def __init__(
        self, months: int = 105, failing: set[str] | None = None, flaky: dict[str, int] | None = None
    ):
        self.months = months  # 2018-01 .. 2026-09 by default
        self.failing = failing or set()
        self.flaky = dict(flaky or {})
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        p = dict(request.url.params)
        if "stlouisfed" in request.url.host:
            sid = p.get("series_id", "")
            if sid in self.failing:
                return httpx.Response(400, json={"error_message": "Bad series"})
            if self.flaky.get(sid, 0) > 0:
                self.flaky[sid] -= 1
                return httpx.Response(503)
            if request.url.path.endswith("/series"):
                return httpx.Response(
                    200,
                    json={
                        "seriess": [
                            {
                                "id": sid,
                                "title": f"Synthetic {sid}",
                                "units": "Index 1982-1984=100",
                                "frequency_short": "M",
                            }
                        ]
                    },
                )
            rate = FRED_RATES.get(sid, 0.03)
            obs = [
                {"date": d.isoformat(), "value": str(v)}
                for d, v in monthly_index("2018-01", self.months, 100, rate)
            ]
            obs.append({"date": "2026-10-01", "value": "."})  # FRED missing-value marker
            return httpx.Response(200, json={"observations": obs})
        if "europa.eu" in request.url.host:
            series = monthly_index("2018-01", self.months, 100, 0.02)
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "size": [1, 1, 1, 1, len(series)],
                        "dimension": {
                            "time": {
                                "category": {
                                    "index": {f"{d.year}-{d.month:02d}": i for i, (d, _) in enumerate(series)}
                                }
                            },
                            "coicop18": {"category": {"label": {p.get("coicop18", ""): "Synthetic"}}},
                        },
                        "value": {str(i): v for i, (_, v) in enumerate(series)},
                    }
                ),
            )
        return httpx.Response(404)
