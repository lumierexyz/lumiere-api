"""Sample statement: 15 months of plausible transactions so a new account can see every feature at once.

The rows are generated from the monthly amounts the user gave in the onboarding questionnaire (or a
default basket for their currency), with local merchant names and small deterministic variations. They
are imported like any statement, but the import batch is flagged ``sample`` so they are labelled as such
in the interface and can be removed in one call. Category labels are set on every expense row, so no
AI classification is needed.
"""

import random
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.transactions.csv_parser import ParsedRow, ParseResult
from app.transactions.models import Category

MONTHS = 15
SAMPLE_FILENAME = "lumiere-sample-statement.csv"

DEFAULT_BASKET = {
    "USD": {
        "housing": 1800,
        "groceries": 450,
        "transportation": 250,
        "restaurants": 150,
        "energy": 140,
        "subscriptions": 60,
        "insurance": 120,
        "healthcare": 60,
        "entertainment": 80,
        "other": 220,
    },
    "EUR": {
        "housing": 1100,
        "groceries": 400,
        "transportation": 200,
        "restaurants": 140,
        "energy": 120,
        "subscriptions": 45,
        "insurance": 70,
        "healthcare": 40,
        "entertainment": 60,
        "other": 180,
    },
    "GBP": {
        "housing": 1300,
        "groceries": 350,
        "transportation": 200,
        "restaurants": 160,
        "energy": 150,
        "subscriptions": 45,
        "insurance": 60,
        "healthcare": 25,
        "entertainment": 60,
        "other": 160,
    },
    "CHF": {
        "housing": 2000,
        "groceries": 600,
        "transportation": 250,
        "restaurants": 120,
        "energy": 110,
        "subscriptions": 60,
        "insurance": 380,
        "healthcare": 60,
        "entertainment": 80,
        "other": 250,
    },
}

US = {
    "housing": ["Rent - Oakwood Apartments"],
    "groceries": ["Whole Foods Market", "Trader Joe's", "Safeway"],
    "restaurants": ["Chipotle", "Sweetgreen", "Joe's Pizza"],
    "transportation": ["Shell", "Uber", "Chevron"],
    "energy": ["ConEdison Electric", "National Grid Gas"],
    "healthcare": ["CVS Pharmacy"],
    "insurance": ["Geico Insurance"],
    "subscriptions": ["Netflix", "Spotify", "Verizon Wireless"],
    "entertainment": ["AMC Theatres", "Ticketmaster"],
    "other": ["Amazon", "Target"],
    "income": "Payroll Deposit",
}
FR = {
    "housing": ["Loyer appartement"],
    "groceries": ["Carrefour", "Monoprix", "Lidl"],
    "restaurants": ["Le Comptoir", "Boulangerie Paul", "Sushi Shop"],
    "transportation": ["TotalEnergies", "SNCF", "RATP Navigo"],
    "energy": ["EDF Electricite", "Engie Gaz"],
    "healthcare": ["Pharmacie Centrale"],
    "insurance": ["AXA Assurance"],
    "subscriptions": ["Netflix", "Spotify", "Free Mobile"],
    "entertainment": ["UGC Cine Cite", "Fnac"],
    "other": ["Amazon", "Decathlon"],
    "income": "Virement salaire",
}
UK = {
    "housing": ["Rent - Foxtons"],
    "groceries": ["Tesco", "Sainsbury's", "Waitrose"],
    "restaurants": ["Pret A Manger", "Dishoom", "Wagamama"],
    "transportation": ["TfL Travel", "Shell", "Uber"],
    "energy": ["Octopus Energy", "British Gas"],
    "healthcare": ["Boots Pharmacy"],
    "insurance": ["Aviva Insurance"],
    "subscriptions": ["Netflix", "Spotify", "EE Mobile"],
    "entertainment": ["Odeon Cinemas", "Ticketmaster"],
    "other": ["Amazon", "John Lewis"],
    "income": "Salary",
}
CH = {
    "housing": ["Miete Wohnung"],
    "groceries": ["Migros", "Coop", "Denner"],
    "restaurants": ["Tibits", "Holy Cow", "Cafe Sprungli"],
    "transportation": ["SBB CFF FFS", "Shell", "Uber"],
    "energy": ["ewz Strom", "Energie 360 Gas"],
    "healthcare": ["Amavita Apotheke"],
    "insurance": ["CSS Krankenversicherung"],
    "subscriptions": ["Netflix", "Spotify", "Swisscom"],
    "entertainment": ["Pathe Kino", "Ticketcorner"],
    "other": ["Galaxus", "Manor"],
    "income": "Lohn",
}
MERCHANTS = {"USD": US, "EUR": FR, "GBP": UK, "CHF": CH}

# Purchases per month, and whether the amount is fixed (bills) or varies (shopping).
PATTERN = {
    "housing": (1, False),
    "energy": (2, True),
    "insurance": (1, False),
    "subscriptions": (3, False),
    "groceries": (6, True),
    "restaurants": (4, True),
    "transportation": (4, True),
    "healthcare": (1, True),
    "entertainment": (2, True),
    "education": (1, False),
    "other": (3, True),
}


def _money(v: float) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _month_start(d: date, back: int) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def build_sample(
    user_id: uuid.UUID, currency: str, monthly: dict[str, float] | None, today: date
) -> ParseResult:
    basket = {k: float(v) for k, v in (monthly or {}).items() if k in Category.__members__.values() and v > 0}
    if not basket:
        basket = dict(DEFAULT_BASKET.get(currency, DEFAULT_BASKET["USD"]))
    names = MERCHANTS.get(currency, US)
    rng = random.Random(user_id.int)  # same user, same sample
    rows: list[ParsedRow] = []
    monthly_total = sum(basket.values())
    line = 1
    for back in range(MONTHS, 0, -1):  # 15 complete months, ending last month
        start = _month_start(today, back)
        days = (_month_start(today, back - 1) - start).days
        rows.append(
            ParsedRow(
                line,
                start + timedelta(days=min(26, days - 1)),
                names["income"],
                _money(round(monthly_total * 1.35, -1)),
                currency,
                names["income"],
                None,
            )
        )
        line += 1
        for cat, amount in basket.items():
            n, varies = PATTERN.get(cat, (2, True))
            pool = names.get(cat) or names["other"]
            parts = [rng.uniform(0.6, 1.4) for _ in range(n)] if varies else [1.0] * n
            scale = amount / sum(parts)
            if varies:
                scale *= rng.uniform(0.9, 1.1)  # month-to-month noise around the declared level
            for i, p in enumerate(parts):
                merchant = pool[i % len(pool)] if not varies else rng.choice(pool)
                day = 1 if cat == "housing" else int(rng.uniform(1, days))
                rows.append(
                    ParsedRow(
                        line,
                        start + timedelta(days=day - 1),
                        merchant,
                        -_money(p * scale),
                        currency,
                        merchant,
                        cat if cat != "other" else None,
                    )
                )
                line += 1
    rows.sort(key=lambda r: r.booked_on)
    return ParseResult(
        rows=rows,
        rejected=[],
        normalization={"sample": True, "months": MONTHS, "basket": basket, "currency": currency},
        warnings=["This is a sample statement generated from your answers, not your real spending."],
    )
