"""The database itself rejects invalid rows. Each test asserts WHICH named constraint fired."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

COMPLETED_AT = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2027, 1, 15, 10, 0, tzinfo=UTC)

INSERT_DRIVER = "INSERT INTO drivers (id) VALUES (:id)"
INSERT_RIDE = (
    "INSERT INTO rides (ride_id, driver_id, distance_km, vehicle_type, completed_at, credits_milli) "
    "VALUES (:ride_id, :driver_id, :distance_km, 'electric', :completed_at, :credits_milli)"
)
INSERT_LOT = (
    "INSERT INTO credit_lots (driver_id, ride_id, amount_milli, remaining_milli, earned_at, expires_at) "
    "VALUES ('d1', :ride_id, :amount, :remaining, :earned_at, :expires_at)"
)
INSERT_REDEMPTION = (
    "INSERT INTO redemptions (driver_id, amount_milli, idempotency_key) VALUES (:driver_id, :amount, :key)"
)
INSERT_ALLOCATION = (
    "INSERT INTO redemption_allocations (redemption_id, lot_id, amount_milli) VALUES (1, 1, :amount)"
)
INSERT_LEDGER = (
    "INSERT INTO ledger_entries (driver_id, entry_type, amount_milli, ride_id, lot_id, redemption_id, reason) "
    "VALUES ('d1', :entry_type, :amount, :ride_id, :lot_id, :redemption_id, :reason)"
)


def _execute(engine: Engine, sql: str, params: dict[str, Any]) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _violated_constraint(engine: Engine, sql: str, params: dict[str, Any]) -> str:
    with pytest.raises(IntegrityError) as exc_info:
        _execute(engine, sql, params)
    return exc_info.value.orig.diag.constraint_name  # type: ignore[union-attr]


def _ride(ride_id: str, driver_id: str = "d1", distance_km: str = "5.000", credits_milli: int = 600) -> dict[str, Any]:
    return {
        "ride_id": ride_id,
        "driver_id": driver_id,
        "distance_km": Decimal(distance_km),
        "completed_at": COMPLETED_AT,
        "credits_milli": credits_milli,
    }


def _lot(ride_id: str, amount: int, remaining: int, expires_at: datetime = EXPIRES_AT) -> dict[str, Any]:
    return {
        "ride_id": ride_id,
        "amount": amount,
        "remaining": remaining,
        "earned_at": COMPLETED_AT,
        "expires_at": expires_at,
    }


def _ledger(
    entry_type: str,
    amount: int,
    ride_id: str | None = None,
    lot_id: int | None = None,
    redemption_id: int | None = None,
    reason: str | None = "test",
) -> dict[str, Any]:
    return {
        "entry_type": entry_type,
        "amount": amount,
        "ride_id": ride_id,
        "lot_id": lot_id,
        "redemption_id": redemption_id,
        "reason": reason,
    }


@pytest.fixture
def seeded(test_engine: Engine) -> Engine:
    """Driver d1 with ride r1 and its credit lot (id 1, 1.200 credits)."""
    _execute(test_engine, INSERT_DRIVER, {"id": "d1"})
    _execute(test_engine, INSERT_RIDE, _ride("r1", distance_km="10.000", credits_milli=1200))
    _execute(
        test_engine,
        "INSERT INTO credit_lots (driver_id, ride_id, amount_milli, remaining_milli, earned_at, expires_at) "
        "VALUES ('d1', 'r1', 1200, 1200, :t, :t + interval '12 months')",
        {"t": COMPLETED_AT},
    )
    return test_engine


# --- defaults -------------------------------------------------------------------------

def test_defaults_fill_ids_and_timestamps(seeded: Engine) -> None:
    with seeded.connect() as conn:
        lot = conn.execute(text("SELECT id, expires_at FROM credit_lots")).one()
        driver_created_at = conn.execute(text("SELECT created_at FROM drivers")).scalar_one()

    assert lot.id == 1  # BIGSERIAL, reset by TRUNCATE ... RESTART IDENTITY
    assert lot.expires_at == EXPIRES_AT  # completed_at + interval '12 months', computed in SQL
    assert driver_created_at is not None


# --- rides ----------------------------------------------------------------------------

def test_ride_distance_must_be_positive(seeded: Engine) -> None:
    violated = _violated_constraint(seeded, INSERT_RIDE, _ride("r2", distance_km="0"))
    assert violated == "ck_rides_distance_km_positive"


def test_ride_credits_cannot_be_negative(seeded: Engine) -> None:
    violated = _violated_constraint(seeded, INSERT_RIDE, _ride("r2", credits_milli=-1))
    assert violated == "ck_rides_credits_milli_non_negative"


def test_ride_requires_existing_driver(seeded: Engine) -> None:
    violated = _violated_constraint(seeded, INSERT_RIDE, _ride("r2", driver_id="ghost"))
    assert violated == "fk_rides_driver_id_drivers"


def test_ride_id_is_unique(seeded: Engine) -> None:
    assert _violated_constraint(seeded, INSERT_RIDE, _ride("r1")) == "pk_rides"


# --- credit_lots ----------------------------------------------------------------------

@pytest.mark.parametrize("remaining", [-1, 1201])
def test_lot_remaining_stays_between_zero_and_amount(seeded: Engine, remaining: int) -> None:
    sql = "UPDATE credit_lots SET remaining_milli = :remaining WHERE id = 1"
    violated = _violated_constraint(seeded, sql, {"remaining": remaining})
    assert violated == "ck_credit_lots_remaining_milli_within_amount"


def test_lot_amount_must_be_positive(seeded: Engine) -> None:
    _execute(seeded, INSERT_RIDE, _ride("r2", credits_milli=0))
    violated = _violated_constraint(seeded, INSERT_LOT, _lot("r2", amount=0, remaining=0))
    assert violated == "ck_credit_lots_amount_milli_positive"


def test_one_lot_per_ride(seeded: Engine) -> None:
    violated = _violated_constraint(seeded, INSERT_LOT, _lot("r1", amount=1200, remaining=1200))
    assert violated == "uq_credit_lots_ride_id"


def test_lot_must_expire_after_it_is_earned(seeded: Engine) -> None:
    _execute(seeded, INSERT_RIDE, _ride("r2"))
    violated = _violated_constraint(seeded, INSERT_LOT, _lot("r2", 600, 600, expires_at=COMPLETED_AT))
    assert violated == "ck_credit_lots_expires_after_earned"


# --- redemptions & allocations --------------------------------------------------------

def test_idempotency_key_unique_per_driver(seeded: Engine) -> None:
    _execute(seeded, INSERT_DRIVER, {"id": "d2"})
    _execute(seeded, INSERT_REDEMPTION, {"driver_id": "d1", "amount": 100, "key": "key-1"})
    # Same key for a different driver is allowed.
    _execute(seeded, INSERT_REDEMPTION, {"driver_id": "d2", "amount": 100, "key": "key-1"})

    violated = _violated_constraint(
        seeded, INSERT_REDEMPTION, {"driver_id": "d1", "amount": 100, "key": "key-1"}
    )
    assert violated == "uq_redemptions_driver_id_idempotency_key"


def test_redemption_amount_must_be_positive(seeded: Engine) -> None:
    violated = _violated_constraint(
        seeded, INSERT_REDEMPTION, {"driver_id": "d1", "amount": 0, "key": "key-1"}
    )
    assert violated == "ck_redemptions_amount_milli_positive"


def test_allocation_amount_positive_and_one_row_per_lot(seeded: Engine) -> None:
    _execute(seeded, INSERT_REDEMPTION, {"driver_id": "d1", "amount": 100, "key": "key-1"})

    violated = _violated_constraint(seeded, INSERT_ALLOCATION, {"amount": 0})
    assert violated == "ck_redemption_allocations_amount_milli_positive"

    _execute(seeded, INSERT_ALLOCATION, {"amount": 100})
    assert _violated_constraint(seeded, INSERT_ALLOCATION, {"amount": 100}) == "pk_redemption_allocations"


# --- ledger_entries -------------------------------------------------------------------

def test_ledger_rejects_unknown_entry_type(seeded: Engine) -> None:
    violated = _violated_constraint(seeded, INSERT_LEDGER, _ledger("BONUS", 100, ride_id="r1", lot_id=1))
    assert violated == "ck_ledger_entries_entry_type"


@pytest.mark.parametrize(
    "entry",
    [
        _ledger("EARN", -1200, ride_id="r1", lot_id=1),
        _ledger("EARN", 0, ride_id="r1", lot_id=1),
        _ledger("EXPIRY", 1200, lot_id=1),
        _ledger("REVERSAL", 1200, ride_id="r1"),
    ],
    ids=["earn-negative", "earn-zero", "expiry-positive", "reversal-positive"],
)
def test_ledger_amount_sign_matches_entry_type(seeded: Engine, entry: dict[str, Any]) -> None:
    assert _violated_constraint(seeded, INSERT_LEDGER, entry) == "ck_ledger_entries_amount_sign"


def test_ledger_redeem_must_be_negative(seeded: Engine) -> None:
    _execute(seeded, INSERT_REDEMPTION, {"driver_id": "d1", "amount": 100, "key": "key-1"})
    violated = _violated_constraint(seeded, INSERT_LEDGER, _ledger("REDEEM", 100, redemption_id=1))
    assert violated == "ck_ledger_entries_amount_sign"


def test_ledger_allows_zero_reversal_for_full_shortfall(seeded: Engine) -> None:
    _execute(seeded, INSERT_LEDGER, _ledger("REVERSAL", 0, ride_id="r1"))


@pytest.mark.parametrize(
    "entry",
    [
        _ledger("EARN", 1200, ride_id="r1"),
        _ledger("EARN", 1200, lot_id=1),
        _ledger("EXPIRY", -1200),
        _ledger("REVERSAL", -1200, lot_id=1),
        _ledger("REDEEM", -100),
    ],
    ids=["earn-no-lot", "earn-no-ride", "expiry-no-lot", "reversal-no-ride", "redeem-no-redemption"],
)
def test_ledger_requires_references_for_entry_type(seeded: Engine, entry: dict[str, Any]) -> None:
    assert _violated_constraint(seeded, INSERT_LEDGER, entry) == "ck_ledger_entries_required_refs"


def test_lot_can_expire_only_once(seeded: Engine) -> None:
    _execute(seeded, INSERT_LEDGER, _ledger("EXPIRY", -1200, lot_id=1))
    violated = _violated_constraint(seeded, INSERT_LEDGER, _ledger("EXPIRY", -1200, lot_id=1))
    assert violated == "uq_ledger_entries_expiry_lot_id"


def test_ride_can_be_reversed_only_once(seeded: Engine) -> None:
    _execute(seeded, INSERT_LEDGER, _ledger("REVERSAL", -1200, ride_id="r1"))
    violated = _violated_constraint(seeded, INSERT_LEDGER, _ledger("REVERSAL", -500, ride_id="r1"))
    assert violated == "uq_ledger_entries_reversal_ride_id"


def test_ledger_reason_is_required(seeded: Engine) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        _execute(seeded, INSERT_LEDGER, _ledger("EARN", 1200, ride_id="r1", lot_id=1, reason=None))
    assert exc_info.value.orig.diag.column_name == "reason"  # type: ignore[union-attr]


# --- ride_cancellations ---------------------------------------------------------------

def test_cancellation_tombstone_needs_no_ride_and_is_unique(test_engine: Engine) -> None:
    tombstone = "INSERT INTO ride_cancellations (ride_id, reason) VALUES (:ride_id, 'refund')"
    _execute(test_engine, tombstone, {"ride_id": "not-yet-completed"})  # no FK: ride may not exist yet
    assert _violated_constraint(test_engine, tombstone, {"ride_id": "not-yet-completed"}) == "pk_ride_cancellations"
