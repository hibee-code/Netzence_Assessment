"""Credit calculator: how many milli-credits a completed ride earns. Pure, no I/O."""

from decimal import Decimal

from app.config import EARNING_VEHICLE_TYPE, EV_GRAMS_CO2_AVOIDED_PER_KM
from app.money import MILLI_PER_CREDIT, floor_to_milli


def normalize_vehicle_type(vehicle_type: str) -> str:
    return vehicle_type.strip().lower()


def calculate_credits_milli(distance_km: Decimal, vehicle_type: str) -> int:
    """1 credit = 1 kg CO2 avoided. Only electric rides earn; rounding happens once, down."""
    if not isinstance(distance_km, Decimal):
        raise TypeError(f"distance_km must be Decimal, got {type(distance_km).__name__}")
    if not distance_km > 0:
        raise ValueError("distance_km must be positive")
    if normalize_vehicle_type(vehicle_type) != EARNING_VEHICLE_TYPE:
        return 0

    credits = distance_km * EV_GRAMS_CO2_AVOIDED_PER_KM / MILLI_PER_CREDIT  # grams -> kg
    return floor_to_milli(credits)
