"""Cancelling a completed ride: reverse the credits it earned.

Clawback policy, for the ride's credit lot:
  - Whatever is still in the lot is taken back.
  - Whatever was already redeemed from it is clawed back from the driver's OTHER spendable lots,
    oldest-expiring first.
  - Whatever expired unspent is not clawed back (the driver never got to use it).
  - The balance never goes negative: anything that can't be recovered is recorded as a shortfall.
The original EARN entry is never touched; ONE REVERSAL entry records what was actually reversed.

A cancellation that arrives before its ride completion is kept as a tombstone in
ride_cancellations and applied by the completion, in the completion's transaction.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import Connection, text

from app.money import from_milli
from app.services.expiry import expire_lots
from app.services.locks import lock_driver, lock_ride
from app.services.lots import debit_lots, drain_fifo, lock_spendable_lots


@dataclass(frozen=True)
class CancellationResult:
    ride_id: str
    status: Literal["cancelled", "pending_completion"]
    reversed_milli: int | None = None  # None while pending
    shortfall_milli: int | None = None


def cancel_ride(conn: Connection, ride_id: str, reason: str | None, now: datetime) -> CancellationResult:
    lock_ride(conn, ride_id)  # a concurrent completion of this ride waits, or has committed
    ride = conn.execute(
        text("SELECT driver_id, cancelled_at, reversed_milli, shortfall_milli FROM rides WHERE ride_id = :ride_id"),
        {"ride_id": ride_id},
    ).first()

    if ride is None:
        conn.execute(
            text(
                "INSERT INTO ride_cancellations (ride_id, reason, received_at) VALUES (:ride_id, :reason, :now) "
                "ON CONFLICT (ride_id) DO NOTHING"
            ),
            {"ride_id": ride_id, "reason": reason, "now": now},
        )
        return CancellationResult(ride_id, "pending_completion")

    if ride.cancelled_at is not None:
        return CancellationResult(ride_id, "cancelled", ride.reversed_milli, ride.shortfall_milli)

    return apply_cancellation(conn, ride_id, ride.driver_id, reason, now)


def apply_cancellation(
    conn: Connection, ride_id: str, driver_id: str, reason: str | None, now: datetime
) -> CancellationResult:
    """Reverse a recorded, not-yet-cancelled ride. The caller must hold lock_ride(ride_id)."""
    lock_driver(conn, driver_id)
    expire_lots(conn, driver_id, now)  # an expired lot's unspent remainder becomes EXPIRY, not clawback

    lots = lock_spendable_lots(conn, driver_id, now, include_ride_id=ride_id)
    own_lot = next((lot for lot in lots if lot.ride_id == ride_id), None)
    reversed_milli = shortfall_milli = 0

    if own_lot is not None:  # rides that earned nothing have no lot and get no ledger entry
        redeemed = conn.execute(
            text("SELECT COALESCE(SUM(amount_milli), 0)::bigint FROM redemption_allocations WHERE lot_id = :lot_id"),
            {"lot_id": own_lot.id},
        ).scalar_one()
        other_lots = [lot for lot in lots if lot.id != own_lot.id]
        clawback = drain_fifo(other_lots, redeemed)

        unspent = own_lot.remaining_milli  # already 0 if the lot expired
        debit_lots(conn, ([(own_lot.id, unspent)] if unspent else []) + clawback)

        target = unspent + redeemed
        reversed_milli = unspent + sum(amount for _, amount in clawback)
        shortfall_milli = target - reversed_milli
        conn.execute(
            text(
                """
                INSERT INTO ledger_entries (driver_id, entry_type, amount_milli, ride_id, lot_id, reason, created_at)
                VALUES (:driver_id, 'REVERSAL', :amount, :ride_id, :lot_id, :reason, :now)
                """
            ),
            {
                "driver_id": driver_id,
                "amount": -reversed_milli,
                "ride_id": ride_id,
                "lot_id": own_lot.id,
                "reason": _reversal_reason(ride_id, reason, reversed_milli, target),
                "now": now,
            },
        )

    conn.execute(
        text(
            "UPDATE rides SET cancelled_at = :now, reversed_milli = :reversed, shortfall_milli = :shortfall "
            "WHERE ride_id = :ride_id"
        ),
        {"ride_id": ride_id, "now": now, "reversed": reversed_milli, "shortfall": shortfall_milli},
    )
    conn.execute(
        text("UPDATE ride_cancellations SET applied_at = :now WHERE ride_id = :ride_id AND applied_at IS NULL"),
        {"ride_id": ride_id, "now": now},
    )
    return CancellationResult(ride_id, "cancelled", reversed_milli, shortfall_milli)


def _reversal_reason(ride_id: str, reason: str | None, reversed_milli: int, target_milli: int) -> str:
    """e.g. "Ride r1 cancelled (refund); reversed 8.000 of 10.000; shortfall 2.000 unrecoverable"."""
    text_reason = f"Ride {ride_id} cancelled"
    if reason:
        text_reason += f" ({reason})"
    text_reason += f"; reversed {from_milli(reversed_milli)} of {from_milli(target_milli)}"
    if reversed_milli < target_milli:
        text_reason += f"; shortfall {from_milli(target_milli - reversed_milli)} unrecoverable"
    return text_reason
