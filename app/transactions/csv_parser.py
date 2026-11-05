"""Bank-statement CSV normalisation.

Handles: delimiter sniffing, preamble lines before the header, multilingual column names, date
formats (with explicit day/month ambiguity handling), decimal comma vs decimal point, thousands
separators, parentheses / trailing-minus negatives, separate debit/credit columns and DR/CR
indicator columns. Interpretation choices are made once per file and returned as metadata.
"""

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

SignConvention = Literal["auto", "negative_is_outflow", "positive_is_outflow"]


class CsvFormatError(ValueError):
    pass


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


ALIASES: dict[str, set[str]] = {
    "date": {
        "date",
        "transaction date",
        "booking date",
        "posted date",
        "posting date",
        "value date",
        "trans date",
        "date operation",
        "date de l operation",
        "date d operation",
        "date valeur",
        "datum",
        "buchungstag",
        "buchungsdatum",
        "fecha",
        "fecha operacion",
        "data",
        "completed date",
        "started date",
    },
    "description": {
        "description",
        "details",
        "memo",
        "narrative",
        "libelle",
        "libelle operation",
        "label",
        "transaction description",
        "reference",
        "verwendungszweck",
        "buchungstext",
        "concepto",
        "transaction details",
        "text",
    },
    "amount": {
        "amount",
        "montant",
        "betrag",
        "importe",
        "transaction amount",
        "amount eur",
        "amount usd",
        "montant eur",
        "value",
        "sum",
    },
    "debit": {
        "debit",
        "debit amount",
        "withdrawal",
        "withdrawals",
        "money out",
        "paid out",
        "soll",
        "cargo",
        "montant debit",
        "debit eur",
    },
    "credit": {
        "credit",
        "credit amount",
        "deposit",
        "deposits",
        "money in",
        "paid in",
        "haben",
        "abono",
        "montant credit",
        "credit eur",
    },
    "currency": {"currency", "devise", "wahrung", "ccy", "moneda", "currency code"},
    "merchant": {
        "merchant",
        "payee",
        "counterparty",
        "beneficiary",
        "name",
        "contrepartie",
        "beneficiaire",
        "empfanger",
        "auftraggeber empfanger",
        "merchant name",
        "counter party",
    },
    "category": {"category", "categorie", "kategorie", "categoria"},
    "direction": {"type", "debit credit", "dr cr", "credit debit indicator", "transaction type", "sens"},
}

DATE_FORMATS: list[tuple[str, str]] = [
    ("%Y-%m-%d", "YMD"),
    ("%Y/%m/%d", "YMD"),
    ("%Y.%m.%d", "YMD"),
    ("%Y%m%d", "YMD"),
    ("%d/%m/%Y", "DMY"),
    ("%d.%m.%Y", "DMY"),
    ("%d-%m-%Y", "DMY"),
    ("%d/%m/%y", "DMY"),
    ("%d.%m.%y", "DMY"),
    ("%m/%d/%Y", "MDY"),
    ("%m-%d-%Y", "MDY"),
    ("%m/%d/%y", "MDY"),
    ("%d %b %Y", "DMY"),
    ("%d %B %Y", "DMY"),
    ("%b %d %Y", "MDY"),
    ("%B %d %Y", "MDY"),
    ("%d-%b-%Y", "DMY"),
]


@dataclass
class ParsedRow:
    line: int
    booked_on: date
    description: str
    amount: Decimal
    currency: str
    merchant: str | None
    category: str | None


@dataclass
class ParseResult:
    rows: list[ParsedRow]
    rejected: list[dict]
    normalization: dict
    warnings: list[str] = field(default_factory=list)


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise CsvFormatError("File encoding not recognised.")


def _sniff_delimiter(text: str) -> str:
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ",;\t|"}
        return max(counts, key=counts.get)


def _map_columns(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, col in enumerate(header):
        n = _norm(col)
        for field_name, aliases in ALIASES.items():
            if field_name in mapping:
                continue
            if n in aliases:
                mapping[field_name] = idx
                break
    return mapping


def _find_header(records: list[list[str]]) -> tuple[int, dict[str, int]]:
    for i, rec in enumerate(records[:15]):
        mapping = _map_columns(rec)
        has_amount = "amount" in mapping or "debit" in mapping or "credit" in mapping
        if "date" in mapping and has_amount:
            return i, mapping
    raise CsvFormatError(
        "Could not find a header row with a date column and an amount (or debit/credit) column."
    )


_AMOUNT_CLEAN = re.compile(r"[^\d,.\-+()]")


def _infer_decimal_separator(samples: list[str]) -> str:
    comma_votes = dot_votes = 0
    for s in samples:
        s = _AMOUNT_CLEAN.sub("", s)
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                comma_votes += 2
            else:
                dot_votes += 2
        elif "," in s:
            tail = s.split(",")[-1].strip("-+()")
            if len(tail) in (1, 2):
                comma_votes += 1
            elif len(tail) == 3 and s.count(",") >= 1:
                dot_votes += 0  # 1,234 is ambiguous on its own
        elif "." in s:
            tail = s.split(".")[-1].strip("-+()")
            if len(tail) in (1, 2):
                dot_votes += 1
            elif len(tail) == 3 and s.count(".") > 1:
                comma_votes += 1  # 1.234.567 => dots are thousands
    return "," if comma_votes > dot_votes else "."


def parse_amount(value: str, decimal_sep: str) -> Decimal | None:
    s = value.strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.endswith("-"):
        negative, s = True, s[:-1]
    s = _AMOUNT_CLEAN.sub("", s).replace("(", "").replace(")", "")
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    s = s.lstrip("+")
    thousands = "." if decimal_sep == "," else ","
    s = s.replace(thousands, "").replace(decimal_sep, ".")
    if not s or s.count(".") > 1:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if negative else d


def _infer_date_format(samples: list[str], preferred_order: str | None) -> tuple[str, bool]:
    cleaned = [re.sub(r"[T ]\d{1,2}:\d{2}(:\d{2})?.*$", "", s.strip()) for s in samples if s.strip()]
    # A few malformed rows must not decide the format: keep the formats that parse the most rows,
    # provided they parse a majority of them. Rows that still fail are rejected individually.
    scores: list[tuple[int, str, str]] = []
    for fmt, order in DATE_FORMATS:
        ok = 0
        for s in cleaned:
            try:
                datetime.strptime(s, fmt)
                ok += 1
            except ValueError:
                pass
        scores.append((ok, fmt, order))
    best = max((s[0] for s in scores), default=0)
    if not cleaned or best <= 0.5 * len(cleaned):
        raise CsvFormatError("Date format not recognised.")
    candidates = [(fmt, order) for ok, fmt, order in scores if ok == best]
    orders = {o for _, o in candidates}
    ambiguous = len(orders - {"YMD"}) > 1
    if ambiguous and preferred_order in orders:
        return next(f for f, o in candidates if o == preferred_order), True
    return candidates[0][0], ambiguous


def parse_date(value: str, fmt: str) -> date | None:
    s = re.sub(r"[T ]\d{1,2}:\d{2}(:\d{2})?.*$", "", value.strip())
    try:
        return datetime.strptime(s, fmt).date()
    except ValueError:
        return None


_CARD_LIKE = re.compile(r"\b(?:\d[ -]?){8,}\d\b")
_IBAN_LIKE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_text(s: str, limit: int) -> str:
    """Strip control characters and mask long account/card numbers before storage."""
    s = _CTRL.sub(" ", s or "")
    s = _IBAN_LIKE.sub(lambda m: "••••" + m.group()[-4:], s)
    s = _CARD_LIKE.sub(lambda m: "••••" + re.sub(r"\D", "", m.group())[-4:], s)
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _direction_sign(value: str) -> int | None:
    v = _norm(value)
    if v in {"dr", "debit", "d", "withdrawal", "out", "sortie", "soll", "debito", "payment"}:
        return -1
    if v in {"cr", "credit", "c", "deposit", "in", "entree", "haben", "credito"}:
        return 1
    return None


def parse_csv(
    raw: bytes,
    *,
    default_currency: str,
    preferred_date_order: str | None = None,
    sign_convention: SignConvention = "auto",
    max_rows: int = 50_000,
) -> ParseResult:
    text = decode(raw)
    delimiter = _sniff_delimiter(text)
    records = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    records = [r for r in records if any(c.strip() for c in r)]
    if not records:
        raise CsvFormatError("The file is empty.")
    header_idx, cols = _find_header(records)
    body = records[header_idx + 1 :]
    if len(body) > max_rows:
        raise CsvFormatError(f"Too many rows ({len(body)}); the limit is {max_rows}.")

    def cell(rec: list[str], key: str) -> str:
        i = cols.get(key)
        return rec[i].strip() if i is not None and i < len(rec) else ""

    amount_samples = [cell(r, k) for r in body for k in ("amount", "debit", "credit") if cell(r, k)]
    decimal_sep = _infer_decimal_separator(amount_samples[:2000])
    date_fmt, ambiguous = _infer_date_format(
        [cell(r, "date") for r in body[:2000] if cell(r, "date")], preferred_date_order
    )

    rows: list[ParsedRow] = []
    rejected: list[dict] = []
    for offset, rec in enumerate(body):
        line = header_idx + offset + 2
        d = parse_date(cell(rec, "date"), date_fmt)
        if d is None:
            rejected.append({"line": line, "reason": "invalid_date"})
            continue
        if "amount" in cols:
            amt = parse_amount(cell(rec, "amount"), decimal_sep)
            if amt is not None and "direction" in cols:
                sign = _direction_sign(cell(rec, "direction"))
                if sign is not None:
                    amt = abs(amt) * sign
        else:
            debit = parse_amount(cell(rec, "debit"), decimal_sep)
            credit = parse_amount(cell(rec, "credit"), decimal_sep)
            amt = None if debit is None and credit is None else (abs(credit or 0) - abs(debit or 0))
        if amt is None:
            rejected.append({"line": line, "reason": "invalid_amount"})
            continue
        if sign_convention == "positive_is_outflow" and "amount" in cols and "direction" not in cols:
            amt = -amt
        currency = (cell(rec, "currency") or default_currency).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            rejected.append({"line": line, "reason": "invalid_currency"})
            continue
        rows.append(
            ParsedRow(
                line=line,
                booked_on=d,
                description=sanitize_text(cell(rec, "description"), 140),
                amount=amt.quantize(Decimal("0.0001")),
                currency=currency,
                merchant=sanitize_text(cell(rec, "merchant"), 120) or None,
                category=cell(rec, "category") or None,
            )
        )

    warnings = []
    if ambiguous:
        warnings.append(
            f"Dates were ambiguous between day/month orders; interpreted with format {date_fmt}. "
            "Set date_order on your profile or in the upload to override."
        )
    if rows and "amount" in cols and sign_convention == "auto" and all(r.amount >= 0 for r in rows):
        warnings.append(
            "All amounts are positive. If this is a card statement where purchases are positive, "
            "re-upload with sign_convention=positive_is_outflow."
        )
    header = records[header_idx]
    return ParseResult(
        rows=rows,
        rejected=rejected,
        warnings=warnings,
        normalization={
            "delimiter": delimiter,
            "header_line": header_idx + 1,
            "columns": {k: header[v] for k, v in cols.items()},
            "date_format": date_fmt,
            "date_ambiguous": ambiguous,
            "decimal_separator": decimal_sep,
            "sign_convention": sign_convention,
            "default_currency": default_currency,
        },
    )
