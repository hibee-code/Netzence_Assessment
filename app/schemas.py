"""Request/response models. Credit amounts leave the API as decimal strings with 3 dp."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator

from app.credits import normalize_vehicle_type

Identifier = Annotated[str, Field(min_length=1, max_length=255)]
Reason = Annotated[str | None, Field(max_length=500)]


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
    # "cancelled": recorded, and a cancellation that arrived earlier was applied straight away.
    status: Literal["recorded", "cancelled", "duplicate"]


class RideCancelledIn(BaseModel):
    ride_id: Identifier
    reason: Reason = None


class RideCancelledOut(BaseModel):
    ride_id: str
    status: Literal["cancelled", "pending_completion"]
    reversed: str | None  # None while pending_completion
    shortfall: str | None


class BalanceOut(BaseModel):
    driver_id: str
    balance: str


class LedgerEntryOut(BaseModel):
    id: int
    type: Literal["EARN", "REDEEM", "REVERSAL", "EXPIRY"]
    amount: str  # signed: "12.000" credit, "-5.000" debit
    ride_id: str | None
    redemption_id: int | None
    reason: str
    created_at: datetime


class HistoryOut(BaseModel):
    driver_id: str
    entries: list[LedgerEntryOut]


class RedeemIn(BaseModel):
    # Positive, at most 3 dp; 15 integer digits keeps milli-credits within BIGINT.
    amount: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=3)]
    reason: Reason = None


class AllocationOut(BaseModel):
    lot_id: int
    ride_id: str
    amount: str


class RedeemOut(BaseModel):
    redemption_id: int
    amount: str
    balance_after: str
    allocations: list[AllocationOut]
