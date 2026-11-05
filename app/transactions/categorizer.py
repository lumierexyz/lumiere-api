"""Deterministic transaction classification (kind + category).

Rules run first and are explainable. Rows they cannot place are left as ``other`` with
``category_source = none`` so the AI classifier can look at them; user corrections are never touched.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from app.transactions.models import Category, Kind


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


_NOISE = re.compile(
    r"^(card payment|card purchase|debit card|pos|cb|carte|paiement|prlv sepa|prelevement|sepa|dd|"
    r"direct debit|"
    r"payment to|purchase|achat|lastschrift|kartenzahlung|compra|paypal|sq|tst|sp)\b\s*"
)


def merchant_key(merchant: str | None, description: str) -> str:
    """Stable key used for recurring-payment detection and duplicate detection."""
    base = normalize_text(merchant or description)
    for _ in range(3):
        base = _NOISE.sub("", base)
    base = re.sub(r"\b\d+\b", " ", base)
    tokens = [t for t in base.split() if len(t) > 1][:3]
    return " ".join(tokens)[:80]


TRANSFER = re.compile(
    r"\b(transfer|virement|vir sepa|uberweisung|transferencia|own account|savings|epargne|to savings|"
    r"from savings|internal transfer|credit card payment|payment thank you|card repayment|"
    r"remboursement carte|"
    r"revolut top up|top up|topup|wise transfer)\b"
)
REFUND = re.compile(
    r"\b(refund|remboursement|rembt|reversal|chargeback|erstattung|reembolso|returned purchase)\b"
)

RULES: list[tuple[Category, re.Pattern[str]]] = [
    (
        Category.HOUSING,
        re.compile(
            r"\b(rent|loyer|miete|alquiler|mortgage|hypotheque|pret immo|landlord|hoa|"
            r"syndic|property tax|taxe fonciere|council tax|real estate)\b"
        ),
    ),
    (
        Category.ENERGY,
        re.compile(
            r"\b(edf|engie|totalenergies|electricity|electricite|gas bill|pg e|con ed|"
            r"duke energy|octopus energy|british gas|eon|e on|iberdrola|endesa|vattenfall|"
            r"enel|utility|utilities|heating|fioul|strom|gaz)\b"
        ),
    ),
    (
        Category.GROCERIES,
        re.compile(
            r"\b(grocery|groceries|supermarket|supermarche|carrefour|leclerc|auchan|"
            r"lidl|aldi|intermarche|monoprix|franprix|casino|tesco|sainsbury|asda|"
            r"waitrose|whole foods|trader joe|safeway|kroger|costco|walmart|rewe|edeka|"
            r"mercadona|picard|biocoop|instacart)\b"
        ),
    ),
    (
        Category.RESTAURANTS,
        re.compile(
            r"\b(restaurant|resto|cafe|coffee|starbucks|mcdonald|burger|pizza|sushi|"
            r"uber eats|ubereats|deliveroo|doordash|grubhub|just eat|bar|brasserie|"
            r"boulangerie|bakery|kfc|subway|chipotle)\b"
        ),
    ),
    (
        Category.TRANSPORTATION,
        re.compile(
            r"\b(uber|lyft|bolt|taxi|sncf|ratp|navigo|tfl|metro|transit|train|"
            r"airline|air france|ryanair|easyjet|lufthansa|delta|united|fuel|petrol|"
            r"gasoline|shell|bp|esso|chevron|exxon|total station|parking|toll|"
            r"peage|car wash|blablacar|lime|bird)\b"
        ),
    ),
    (
        Category.HEALTHCARE,
        re.compile(
            r"\b(pharmacy|pharmacie|apotheke|farmacia|doctor|medecin|dentist|dentiste|"
            r"hospital|hopital|clinic|clinique|cvs|walgreens|boots|optician|opticien|"
            r"medical|health)\b"
        ),
    ),
    (
        Category.INSURANCE,
        re.compile(
            r"\b(insurance|assurance|versicherung|seguro|axa|allianz|maif|macif|matmut|"
            r"geico|state farm|progressive|mutuelle|aviva)\b"
        ),
    ),
    (
        Category.SUBSCRIPTIONS,
        re.compile(
            r"\b(netflix|spotify|disney|hulu|apple com bill|itunes|icloud|"
            r"google storage|youtube premium|amazon prime|prime video|deezer|canal|"
            r"adobe|microsoft 365|dropbox|chatgpt|openai|notion|subscription|"
            r"abonnement|mobile|orange|sfr|bouygues|free mobile|verizon|at t|"
            r"t mobile|vodafone|internet|broadband)\b"
        ),
    ),
    (
        Category.EDUCATION,
        re.compile(
            r"\b(tuition|school|ecole|university|universite|college|course|coursera|"
            r"udemy|books|librairie|education|frais de scolarite)\b"
        ),
    ),
    (
        Category.ENTERTAINMENT,
        re.compile(
            r"\b(cinema|movie|theatre|theater|concert|ticketmaster|fnac|steam|"
            r"playstation|xbox|nintendo|museum|musee|gym|fitness|sport|bowling)\b"
        ),
    ),
]

# Bank-provided category labels mapped onto Lumiere categories.
IMPORT_CATEGORY_MAP: dict[str, Category] = {
    **{k: Category.HOUSING for k in ("housing", "rent", "mortgage", "logement", "home")},
    **{k: Category.GROCERIES for k in ("groceries", "grocery", "alimentation", "courses", "food groceries")},
    **{k: Category.RESTAURANTS for k in ("restaurants", "dining", "eating out", "food drink", "restaurant")},
    **{
        k: Category.TRANSPORTATION
        for k in ("transport", "transportation", "travel", "auto transport", "fuel")
    },
    **{k: Category.ENERGY for k in ("energy", "utilities", "bills utilities", "energie")},
    **{k: Category.HEALTHCARE for k in ("health", "healthcare", "medical", "sante")},
    **{k: Category.INSURANCE for k in ("insurance", "assurance")},
    **{k: Category.SUBSCRIPTIONS for k in ("subscriptions", "subscription", "abonnements", "phone internet")},
    **{k: Category.EDUCATION for k in ("education", "school")},
    **{k: Category.ENTERTAINMENT for k in ("entertainment", "leisure", "loisirs", "fun")},
}


@dataclass(frozen=True)
class Classification:
    kind: Kind
    category: Category
    source: str  # rule | import | none
    confidence: float | None


def classify_kind(amount: Decimal, text: str) -> Kind:
    if TRANSFER.search(text):
        return Kind.TRANSFER
    if amount > 0:
        return Kind.REFUND if REFUND.search(text) else Kind.INCOME
    return Kind.EXPENSE


def classify(
    description: str, merchant: str | None, amount: Decimal, import_category: str | None
) -> Classification:
    text = normalize_text(f"{merchant or ''} {description}")
    kind = classify_kind(amount, text)
    if kind in (Kind.INCOME, Kind.TRANSFER):
        return Classification(kind, Category.OTHER, "rule", 1.0)
    if import_category:
        mapped = IMPORT_CATEGORY_MAP.get(normalize_text(import_category))
        if mapped:
            return Classification(kind, mapped, "import", 0.9)
    for category, pattern in RULES:
        if pattern.search(text):
            return Classification(kind, category, "rule", 0.85)
    return Classification(kind, Category.OTHER, "none", None)
