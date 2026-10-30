"""Credit calculator: 120 g CO2 avoided per electric km; 1 milli-credit = 1 gram; floor once."""

from decimal import Decimal

import pytest

from app.credits import calculate_credits_milli


@pytest.mark.parametrize(
    ("distance_km", "milli"),
    [
        ("1", 120),
        ("12.3", 1476),
        ("10", 1200),
        ("0.5", 60),
        ("9999999.999", 1199999999),  # NUMERIC(10,3) maximum
    ],
)
def test_electric_ride_earns_120_grams_per_km(distance_km: str, milli: int) -> None:
    assert calculate_credits_milli(Decimal(distance_km), "electric") == milli


@pytest.mark.parametrize(
    ("distance_km", "milli"),
    [
        ("10.001", 1200),  # 1200.12 g -> 1200
        ("0.009", 1),  # 1.08 g -> 1
        ("0.008", 0),  # 0.96 g -> 0: never over-issue
        ("0.001", 0),  # 0.12 g -> 0
    ],
)
def test_fractional_grams_round_down(distance_km: str, milli: int) -> None:
    assert calculate_credits_milli(Decimal(distance_km), "electric") == milli


@pytest.mark.parametrize("vehicle_type", ["Electric", "ELECTRIC", "  electric  "])
def test_vehicle_type_is_case_and_whitespace_insensitive(vehicle_type: str) -> None:
    assert calculate_credits_milli(Decimal("10"), vehicle_type) == 1200


@pytest.mark.parametrize("vehicle_type", ["petrol", "diesel", "hybrid", "plug-in hybrid", "electrics"])
def test_non_electric_rides_earn_nothing(vehicle_type: str) -> None:
    assert calculate_credits_milli(Decimal("100"), vehicle_type) == 0


@pytest.mark.parametrize("distance_km", ["0", "-1"])
def test_rejects_non_positive_distance(distance_km: str) -> None:
    with pytest.raises(ValueError, match="positive"):
        calculate_credits_milli(Decimal(distance_km), "electric")


def test_rejects_float_distance() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        calculate_credits_milli(12.3, "electric")  # type: ignore[arg-type]
