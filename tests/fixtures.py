"""Deterministic demo fixtures for tests. Not used by production code."""

from datetime import date


def monthly_statement_csv(
    months: int = 12,
    start_year: int = 2025,
    start_month: int = 1,
    rent: float = 1500.0,
    groceries: float = 400.0,
    energy: float = 120.0,
) -> bytes:
    lines = ["Date,Description,Amount,Currency"]
    y, m = start_year, start_month
    for _ in range(months):
        lines += [
            f"{date(y, m, 1).isoformat()},RENT PAYMENT LANDLORD,-{rent:.2f},USD",
            f"{date(y, m, 5).isoformat()},WHOLE FOODS MARKET #123,-{groceries * 0.6:.2f},USD",
            f"{date(y, m, 19).isoformat()},TRADER JOE'S,-{groceries * 0.4:.2f},USD",
            f"{date(y, m, 12).isoformat()},CON ED ELECTRICITY BILL,-{energy:.2f},USD",
            f"{date(y, m, 15).isoformat()},NETFLIX.COM,-15.49,USD",
            f"{date(y, m, 25).isoformat()},ACME CORP PAYROLL,5000.00,USD",
            f"{date(y, m, 26).isoformat()},TRANSFER TO SAVINGS,-1000.00,USD",
        ]
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return ("\n".join(lines) + "\n").encode()


def monthly_index(
    start: str, months: int, start_value: float, annual_rate: float
) -> list[tuple[date, float]]:
    y, m = map(int, start.split("-"))
    out = []
    for i in range(months):
        out.append((date(y, m, 1), round(start_value * (1 + annual_rate) ** (i / 12), 4)))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out
