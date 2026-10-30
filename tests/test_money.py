from decimal import Decimal

import pytest

from app.money import floor_to_milli, from_milli, to_milli


@pytest.mark.parametrize(
    ("amount", "milli"),
    [
        ("0", 0),
        ("0.001", 1),
        ("1", 1000),
        ("1.5", 1500),
        ("12.345", 12345),
        ("1.2300", 1230),  # trailing zeros are still 3 dp of value
        ("-2", -2000),
        ("123456789012.345", 123456789012345),  # beyond float's exact range for 3 dp
    ],
)
def test_to_milli_is_exact(amount: str, milli: int) -> None:
    assert to_milli(Decimal(amount)) == milli


@pytest.mark.parametrize("amount", ["0.0001", "1.2345", "12.3451"])
def test_to_milli_rejects_more_than_three_decimal_places(amount: str) -> None:
    with pytest.raises(ValueError, match="at most 3 decimal places"):
        to_milli(Decimal(amount))


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity"])
def test_to_milli_rejects_non_finite(amount: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        to_milli(Decimal(amount))


def test_money_helpers_reject_floats() -> None:
    with pytest.raises(TypeError):
        to_milli(0.1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        floor_to_milli(0.1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("amount", "milli"),
    [
        ("1.4769", 1476),
        ("1.4761", 1476),
        ("0.0009", 0),
        ("1.476", 1476),  # already exact: unchanged
        ("1.2000001", 1200),
    ],
)
def test_floor_to_milli_always_rounds_down(amount: str, milli: int) -> None:
    assert floor_to_milli(Decimal(amount)) == milli


@pytest.mark.parametrize(
    ("milli", "text"),
    [
        (0, "0.000"),
        (1, "0.001"),
        (10, "0.010"),
        (1000, "1.000"),
        (12345, "12.345"),
        (-2000, "-2.000"),
        (-1, "-0.001"),
        (123456789012345, "123456789012.345"),
    ],
)
def test_from_milli_formats_exactly_three_decimal_places(milli: int, text: str) -> None:
    assert from_milli(milli) == text


@pytest.mark.parametrize("milli", [0, 1, 999, 1000, 1001, 987654321, -5])
def test_round_trip(milli: int) -> None:
    assert to_milli(Decimal(from_milli(milli))) == milli
