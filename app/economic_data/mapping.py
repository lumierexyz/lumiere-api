"""Which official price index represents which spending category, per geography.

A national index measures an average basket; it is a proxy for a category, not a measurement of any
individual's prices. ``proxy`` marks mappings that are weaker still (a broader or narrower index used
because no direct category index exists). Unmapped categories fall back to the headline index and the
fallback is reported explicitly in every API response.

EU codes are COICOP 2018 (Eurostat ``prc_hicp_minr``, index 2015=100), verified against the live API.
US codes are BLS CPI-U series published on FRED (seasonally adjusted where available).
"""

from dataclasses import dataclass

from app.transactions.models import Category

HEADLINE = "headline"


@dataclass(frozen=True)
class SeriesSpec:
    source: str  # fred | eurostat
    code: str
    geography: str
    category: str
    note: str = ""
    proxy: bool = False


US_MAP: dict[str, tuple[str, str, bool]] = {
    HEADLINE: ("CPIAUCSL", "CPI-U all items", False),
    Category.HOUSING: ("CUSR0000SAH1", "CPI-U shelter", False),
    Category.GROCERIES: ("CUSR0000SAF11", "CPI-U food at home", False),
    Category.RESTAURANTS: ("CUSR0000SEFV", "CPI-U food away from home", False),
    Category.TRANSPORTATION: ("CUSR0000SAT", "CPI-U transportation", False),
    Category.ENERGY: ("CUSR0000SA0E", "CPI-U energy", False),
    Category.HEALTHCARE: ("CUSR0000SAM", "CPI-U medical care", False),
    Category.INSURANCE: ("CUSR0000SETE", "CPI-U motor vehicle insurance (proxy for all insurance)", True),
    Category.SUBSCRIPTIONS: ("CUSR0000SAE2", "CPI-U communication (proxy for subscriptions)", True),
    Category.EDUCATION: ("CUSR0000SAE1", "CPI-U education", False),
    Category.ENTERTAINMENT: ("CUSR0000SAR", "CPI-U recreation", False),
}

EU_MAP: dict[str, tuple[str, str, bool]] = {
    HEADLINE: ("TOTAL", "HICP all items", False),
    Category.HOUSING: ("CP041", "HICP actual rentals (owners' housing costs are not in HICP)", True),
    Category.GROCERIES: ("CP011", "HICP food", False),
    Category.RESTAURANTS: ("CP111", "HICP food and beverage serving services", False),
    Category.TRANSPORTATION: ("CP07", "HICP transport", False),
    Category.ENERGY: ("NRG", "HICP energy", False),
    Category.HEALTHCARE: ("CP06", "HICP health", False),
    Category.INSURANCE: ("CP121", "HICP insurance", False),
    Category.SUBSCRIPTIONS: ("CP083", "HICP information and communication services (proxy)", True),
    Category.EDUCATION: ("CP10", "HICP education services", False),
    Category.ENTERTAINMENT: ("CP09", "HICP recreation, sport and culture", False),
}

# Eurostat geography codes (note Greece = EL). Countries outside this list use the euro-area
# aggregate only if their currency is EUR; otherwise the geography is unsupported.
EUROSTAT_GEOS = {
    "AT",
    "BE",
    "BG",
    "CY",
    "CZ",
    "DE",
    "DK",
    "EE",
    "ES",
    "FI",
    "FR",
    "HR",
    "HU",
    "IE",
    "IT",
    "LT",
    "LU",
    "LV",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SE",
    "SI",
    "SK",
    "IS",
    "NO",
    "CH",
}
EURO_AREA = "EA20"


def resolve_geography(country: str) -> tuple[str, str] | None:
    """Return (source, geo code) for a profile country, or None when no supported index exists."""
    c = country.upper()
    if c == "US":
        return "fred", "US"
    if c == "GR":
        return "eurostat", "EL"
    if c in EUROSTAT_GEOS:
        return "eurostat", c
    return None


def spec_for(country: str, category: str) -> tuple[SeriesSpec | None, str | None]:
    """Series for a category, falling back to headline. Returns (spec, fallback_reason)."""
    geo = resolve_geography(country)
    if geo is None:
        return None, f"No supported price index for country {country}."
    source, code = geo
    table = US_MAP if source == "fred" else EU_MAP
    key = Category(category) if category != HEADLINE and category in Category._value2member_map_ else category
    if key in table:
        sid, note, proxy = table[key]
        return SeriesSpec(source, sid, code, category, note, proxy), None
    sid, note, _ = table[HEADLINE]
    return (
        SeriesSpec(source, sid, code, category, f"{note} (fallback: no category-specific index)", True),
        "No category-specific index; headline index used.",
    )
