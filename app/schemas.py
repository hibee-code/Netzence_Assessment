"""Request/response models. Credit amounts leave the API as decimal strings with 3 dp."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator

from app.credits import normalize_vehicle_type

Identifier = Annotated[str, Field(min_length=1, max_length=255)]


class RideCompletedIn(BaseModel):
    ride_id: Identifier
    driver_id: Identifier
    # Matches rides.distance_km NUMERIC(10,3). Accepts JSON numbers or strings; parsed as Decimal.
    distance_km: Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=3)]
    vehicle_type: Annotated[str, Field(min_length=1, max_length=50)]
    completed_at: AwareDatetime

    @field_validator("vehicle_type")
    @classmethod
    def _normalize_vehicle_type(cls, value: str) -> str:
        return normalize_vehicle_type(value)


class RideCompletedOut(BaseModel):
    ride_id: str
    driver_id: str
    credits: str
    expires_at: datetime | None
    status: Literal["recorded", "duplicate"]


class BalanceOut(BaseModel):
    driver_id: str
    balance: str


class RedeemIn(BaseModel):
    # Positive, at most 3 dp; 15 integer digits keeps milli-credits within BIGINT.
    amount: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=3)]
    reason: Annotated[str | None, Field(max_length=500)] = None


class AllocationOut(BaseModel):
    lot_id: int
    ride_id: str
    amount: str


class RedeemOut(BaseModel):
    redemption_id: int
    amount: str
    balance_after: str
    allocations: list[AllocationOut]
