from decimal import Decimal

import pytest

from app.transactions.categorizer import classify, merchant_key
from app.transactions.csv_parser import CsvFormatError, parse_amount, parse_csv, sanitize_text
from app.transactions.models import Category, Kind
from tests.fixtures import monthly_statement_csv


def test_parse_amount_formats():
    assert parse_amount("1,234.56", ".") == Decimal("1234.56")
    assert parse_amount("1.234,56", ",") == Decimal("1234.56")
    assert parse_amount("(12.50)", ".") == Decimal("-12.50")
    assert parse_amount("12,50-", ",") == Decimal("-12.50")
    assert parse_amount("€ -3,20", ",") == Decimal("-3.20")
    assert parse_amount("abc", ".") is None


def test_european_csv_with_preamble_semicolons_and_debit_credit():
    raw = (
        "Compte courant;FR76...\n\nDate opération;Libellé;Débit;Crédit\n"
        "03/02/2026;CB CARREFOUR MARKET;45,90;\n"
        "15/02/2026;VIR SALAIRE ACME;;2 500,00\n"
        "28/02/2026;PRLV SEPA EDF;1 234,56;\n"
    ).encode("cp1252")
    res = parse_csv(raw, default_currency="EUR")
    assert res.normalization["decimal_separator"] == ","
    assert res.normalization["delimiter"] == ";"
    amounts = [r.amount for r in res.rows]
    assert amounts == [Decimal("-45.90"), Decimal("2500.00"), Decimal("-1234.56")]
    assert res.rows[0].booked_on.isoformat() == "2026-02-03"  # 15/02 disambiguates to DMY
    assert res.rows[0].currency == "EUR"


def test_us_dates_and_direction_column():
    raw = b"Posted Date,Payee,Amount,Type\n01/31/2026,Shell Oil,42.10,DR\n02/01/2026,Refund Amazon,10.00,CR\n"
    res = parse_csv(raw, default_currency="USD")
    assert [r.booked_on.isoformat() for r in res.rows] == ["2026-01-31", "2026-02-01"]
    assert [r.amount for r in res.rows] == [Decimal("-42.10"), Decimal("10.00")]


def test_ambiguous_dates_follow_preference_and_warn():
    raw = b"Date,Description,Amount\n03/04/2026,X,-1\n05/06/2026,Y,-2\n"
    res = parse_csv(raw, default_currency="USD", preferred_date_order="MDY")
    assert res.rows[0].booked_on.isoformat() == "2026-03-04"
    assert res.normalization["date_ambiguous"] is True and res.warnings


def test_malformed_rows_rejected_not_fatal():
    raw = b"Date,Description,Amount\n2026-01-01,ok,-1\nnot-a-date,bad,-2\n2026-01-03,bad amount,xx\n"
    res = parse_csv(raw, default_currency="USD")
    assert len(res.rows) == 1 and {r["reason"] for r in res.rejected} == {"invalid_date", "invalid_amount"}


def test_missing_required_columns():
    with pytest.raises(CsvFormatError):
        parse_csv(b"foo,bar\n1,2\n", default_currency="USD")


def test_sensitive_numbers_masked():
    assert "4111" not in sanitize_text("CARD 4111 1111 1111 1234 PAYMENT", 140).replace("1234", "")
    assert sanitize_text("FR7630006000011234567890189 rent", 140).startswith("••••0189")


def test_categorization_rules():
    assert classify("RENT PAYMENT", None, Decimal("-1500"), None).category == Category.HOUSING
    assert classify("UBER EATS ORDER", None, Decimal("-30"), None).category == Category.RESTAURANTS
    assert classify("UBER TRIP", None, Decimal("-12"), None).category == Category.TRANSPORTATION
    assert classify("TRANSFER TO SAVINGS", None, Decimal("-100"), None).kind == Kind.TRANSFER
    assert classify("AMAZON REFUND", None, Decimal("20"), None).kind == Kind.REFUND
    assert classify("ACME PAYROLL", None, Decimal("5000"), None).kind == Kind.INCOME
    unknown = classify("XYZQ 88", None, Decimal("-9"), None)
    assert unknown.category == Category.OTHER and unknown.source == "none"
    assert merchant_key(None, "CARD PAYMENT NETFLIX.COM 12/03") == merchant_key(None, "NETFLIX.COM")


def test_import_detects_duplicates_and_keeps_same_day_repeats(client, auth):
    csv1 = b"Date,Description,Amount\n2026-01-02,COFFEE SHOP,-3.50\n2026-01-02,COFFEE SHOP,-3.50\n"
    r1 = client.post("/api/v1/transactions/import", headers=auth, files={"file": ("a.csv", csv1, "text/csv")})
    assert r1.json()["imported"] == 2  # two real coffees
    csv2 = csv1 + b"2026-01-03,BAKERY,-4.00\n"
    r2 = client.post("/api/v1/transactions/import", headers=auth, files={"file": ("b.csv", csv2, "text/csv")})
    assert r2.json()["imported"] == 1 and r2.json()["duplicates"] == 2


def test_upload_limits_and_type(client, auth, monkeypatch):
    from app.core.config import get_settings

    r = client.post(
        "/api/v1/transactions/import", headers=auth, files={"file": ("x.pdf", b"%PDF", "application/pdf")}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_file"
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 100)
    big = b"Date,Description,Amount\n" + b"2026-01-01,x,-1\n" * 50
    r = client.post("/api/v1/transactions/import", headers=auth, files={"file": ("big.csv", big, "text/csv")})
    assert r.status_code == 413


def test_user_correction_is_never_overwritten_by_ai(client, auth):
    from app.ai import claude_client
    from tests.fake_claude import FakeClaude, message, text_block

    client.post(
        "/api/v1/transactions/import",
        headers=auth,
        files={
            "file": (
                "a.csv",
                b"Date,Description,Amount\n2026-01-02,ZZZ UNKNOWN,-50\n2026-01-03,QQQ UNKNOWN,-60\n",
                "text/csv",
            )
        },
    )
    items = client.get("/api/v1/transactions", headers=auth).json()["items"]
    target = next(i for i in items if i["description"].startswith("ZZZ"))
    client.patch(f"/api/v1/transactions/{target['id']}", headers=auth, json={"category": "education"})

    def respond(kw):
        n = kw["messages"][0]["content"].count('"id"')
        return message(
            text_block(
                {"results": [{"id": i, "category": "entertainment", "confidence": 0.7} for i in range(n)]}
            )
        )

    fake = FakeClaude(respond)
    claude_client.set_client(fake)
    try:
        r = client.post("/api/v1/transactions/categorize", headers=auth)
    finally:
        claude_client.set_client(None)
    assert r.json() == {"considered": 1, "updated": 1}
    req = fake.requests[0]
    sent = req["messages"][0]["content"]
    assert req["fallbacks"] == "default" and req["output_config"]["format"]["type"] == "json_schema"
    assert "QQQ" in sent and "ZZZ" not in sent and "-60" not in sent  # minimal data, no amounts
    by_desc = {i["description"]: i for i in client.get("/api/v1/transactions", headers=auth).json()["items"]}
    assert by_desc["ZZZ UNKNOWN"]["category"] == "education" and by_desc["ZZZ UNKNOWN"]["user_corrected"]
    assert by_desc["QQQ UNKNOWN"]["category"] == "entertainment"
    assert by_desc["QQQ UNKNOWN"]["category_source"] == "ai"


def test_ai_unavailable_without_key(client, auth):
    r = client.post("/api/v1/transactions/categorize", headers=auth)
    assert r.status_code in (200, 503)  # 200 when nothing to classify


def test_expense_summary_separates_income_transfers(client, auth):
    client.post(
        "/api/v1/transactions/import",
        headers=auth,
        files={"file": ("s.csv", monthly_statement_csv(12), "text/csv")},
    )
    s = client.get("/api/v1/expenses/summary", headers=auth).json()
    assert s["has_data"]
    # rent 1500 + groceries 400 + energy 120 + netflix 15.49 per month; payroll/transfer excluded
    assert s["average_monthly_expenses"] == pytest.approx(2035.49, abs=0.01)
    cats = {c["category"]: c for c in s["by_category"]}
    assert cats["housing"]["monthly_average"] == 1500
    assert s["transfers_excluded"] == 12 and s["income_total"] == 60000
    rec = client.get("/api/v1/expenses/recurring", headers=auth).json()
    keys = {i["category"] for i in rec["items"]}
    assert {"housing", "subscriptions", "energy"} <= keys
    hist = client.get("/api/v1/expenses/history?limit=3", headers=auth).json()
    assert len(hist["items"]) == 3 and hist["items"][0]["month"] == "2025-12"


def test_multi_currency_not_aggregated_without_rate(client, auth):
    raw = b"Date,Description,Amount,Currency\n2026-01-02,RENT,-1000,USD\n2026-01-05,LOYER PARIS,-800,EUR\n"
    client.post("/api/v1/transactions/import", headers=auth, files={"file": ("m.csv", raw, "text/csv")})
    s = client.get("/api/v1/expenses/summary", headers=auth).json()
    assert s["total_expenses"] == 1000
    assert s["unconverted"] == [{"currency": "EUR", "transactions": 1, "net_amount": -800.0}]
    native = {n["currency"]: n for n in s["by_currency_native"]}
    assert native["EUR"]["expenses"] == 800


def test_multi_currency_converted_with_explicit_rate(client, auth, db):
    from datetime import date

    from app.economic_data.models import FxRate

    db.add(
        FxRate(
            base="EUR",
            quote="USD",
            rate_date=date(2026, 1, 2),
            rate=1.10,
            source="fred",
            series_code="DEXUSEU",
        )
    )
    db.commit()
    raw = b"Date,Description,Amount,Currency\n2026-01-02,RENT,-1000,USD\n2026-01-05,LOYER PARIS,-800,EUR\n"
    client.post("/api/v1/transactions/import", headers=auth, files={"file": ("m.csv", raw, "text/csv")})
    s = client.get("/api/v1/expenses/summary", headers=auth).json()
    assert s["total_expenses"] == pytest.approx(1880.0)  # rate dated 3 days before the transaction
    assert s["unconverted"] == []
