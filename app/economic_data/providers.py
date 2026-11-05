"""FRED and Eurostat clients. Missing observations are skipped, never filled in."""

from dataclasses import dataclass
from datetime import date

from app.core.config import get_settings
from app.core.errors import UpstreamError
from app.economic_data.http import get_json, get_text

FRED_BASE = "https://api.stlouisfed.org/fred"
FRED_GRAPH_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
EUROSTAT_BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"


@dataclass
class FetchedSeries:
    title: str
    unit: str
    frequency: str
    observations: list[tuple[date, float]]


def fetch_fred(series_id: str, start: str = "2000-01-01") -> FetchedSeries:
    key = get_settings().fred_api_key
    if not key:
        return fetch_fred_public_csv(series_id, start)
    meta = get_json(
        "fred", f"{FRED_BASE}/series", {"series_id": series_id, "api_key": key, "file_type": "json"}
    )
    info = (meta.get("seriess") or [{}])[0]
    data = get_json(
        "fred",
        f"{FRED_BASE}/series/observations",
        {"series_id": series_id, "api_key": key, "file_type": "json", "observation_start": start},
    )
    obs: list[tuple[date, float]] = []
    for o in data.get("observations", []):
        v = o.get("value")
        if v in (None, "", "."):  # FRED marks missing values with "."
            continue
        try:
            obs.append((date.fromisoformat(o["date"]), float(v)))
        except (KeyError, ValueError):
            continue
    return FetchedSeries(
        title=info.get("title", series_id),
        unit=info.get("units", ""),
        frequency=info.get("frequency_short", "M"),
        observations=obs,
    )


def fetch_fred_public_csv(series_id: str, start: str = "2000-01-01") -> FetchedSeries:
    """Keyless FRED download (the public ``fredgraph.csv`` export), used when no API key is configured.
    Same observations as the API; titles and units are not included in this export."""
    text = get_text("fred", FRED_GRAPH_CSV, {"id": series_id, "cosd": start})
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or "," not in lines[0] or not lines[0].lower().startswith(("observation_date", "date")):
        raise UpstreamError("FRED returned an unexpected file.", details={"provider": "fred"})
    obs: list[tuple[date, float]] = []
    for ln in lines[1:]:
        d, _, v = ln.partition(",")
        if v in ("", "."):  # missing observation: skipped, never filled
            continue
        try:
            obs.append((date.fromisoformat(d), float(v)))
        except ValueError:
            continue
    if len(obs) > 2:
        gap = (obs[-1][0] - obs[-2][0]).days
        frequency = "D" if gap < 7 else "W" if gap < 20 else "M" if gap < 45 else "Q" if gap < 120 else "A"
    else:
        frequency = "M"
    return FetchedSeries(title=series_id, unit="", frequency=frequency, observations=obs)


def fetch_eurostat_hicp(coicop: str, geo: str, unit: str = "I15", since: str = "2000-01") -> FetchedSeries:
    dataset = get_settings().eurostat_hicp_dataset
    params = {"geo": geo, "unit": unit, "freq": "M", "sinceTimePeriod": since, "format": "JSON", "lang": "EN"}
    params["coicop18" if dataset == "prc_hicp_minr" else "coicop"] = coicop
    data = get_json("eurostat", f"{EUROSTAT_BASE}/{dataset}", params)
    if "error" in data:
        raise UpstreamError("Eurostat rejected the request.", details={"provider": "eurostat"})
    dims = data.get("dimension", {})
    time_index: dict[str, int] = dims.get("time", {}).get("category", {}).get("index", {})
    values: dict[str, float] = data.get("value", {})
    # Single-series query: every non-time dimension has size 1, so the flat index equals the time index.
    sizes = data.get("size", [])
    if sizes and any(s != 1 for s in sizes[:-1]):
        raise UpstreamError("Unexpected Eurostat response shape.", details={"provider": "eurostat"})
    obs = []
    for period, pos in time_index.items():
        v = values.get(str(pos))
        if v is None:
            continue
        y, m = period.split("-")[:2]
        obs.append((date(int(y), int(m), 1), float(v)))
    label = (
        dims.get("coicop18", dims.get("coicop", {})).get("category", {}).get("label", {}).get(coicop, coicop)
    )
    return FetchedSeries(
        title=f"HICP {label} ({geo})",
        unit=f"Index, {unit[1:]}=100" if unit.startswith("I") else unit,
        frequency="M",
        observations=sorted(obs),
    )
