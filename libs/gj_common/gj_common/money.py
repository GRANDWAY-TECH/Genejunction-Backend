from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
CURRENCY = "MYR"


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def pct_of(amount: Decimal, pct: Decimal) -> Decimal:
    return money(Decimal(amount) * Decimal(pct) / Decimal(100))
