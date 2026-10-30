"""Credit amounts.

Stored and computed as integer milli-credits (1 credit = 1000 milli). Decimal is used only at the
edges (parsing input, formatting output). Floats are rejected outright.
"""

from decimal import ROUND_FLOOR, Decimal

MILLI_PER_CREDIT = 1000


def _require_finite_decimal(amount: Decimal) -> None:
    if not isinstance(amount, Decimal):
        raise TypeError(f"expected Decimal, got {type(amount).__name__}")
    if not amount.is_finite():
        raise ValueError("amount must be a finite number")


def to_milli(amount: Decimal) -> int:
    """Exact conversion of a credit amount to milli-credits. Rejects more than 3 decimal places."""
    _require_finite_decimal(amount)
    milli = amount * MILLI_PER_CREDIT
    if milli != milli.to_integral_value():
        raise ValueError("amount must have at most 3 decimal places")
    return int(milli)


def floor_to_milli(amount: Decimal) -> int:
    """Credit amount of any precision to milli-credits, rounding DOWN (never over-issue)."""
    _require_finite_decimal(amount)
    return int((amount * MILLI_PER_CREDIT).to_integral_value(rounding=ROUND_FLOOR))


def from_milli(milli: int) -> str:
    """Milli-credits to a decimal string with exactly 3 decimal places, e.g. 12345 -> "12.345"."""
    sign = "-" if milli < 0 else ""
    whole, fraction = divmod(abs(milli), MILLI_PER_CREDIT)
    return f"{sign}{whole}.{fraction:03d}"
