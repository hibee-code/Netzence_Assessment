"""Recording completed rides. Idempotent: the same event, however often and concurrently it
arrives, creates exactly one ride, at most one credit lot, and at most one EARN ledger entry.

All writes go through the caller's connection, inside the caller's single transaction.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection, text

from app.config import CREDIT_LIFETIME, MAX_COMPLETED_AT_FUTURE_SKEW
from app.credits import calculate_credits_milli, normalize_vehicle_type
from app.services.drivers import ensure_driver


@dataclass(frozen=True)
class RideCompleted:
    ride_id: str
    driver_id: str
    distance_km: Decimal
    vehicle_type: str
    completed_at: datetime


@dataclass(frozen=True)
class RideResult:
    ride_id: str
    driver_id: str
    credits_milli: int
    expires_at: datetime | None  # None when the ride earned nothing
    created: bool  # False when this event was a duplicate of an already-recorded ride


class CompletedAtInFuture(Exception):
    pass


class RideConflict(Exception):
    """Same ride_id as a recorded ride, but different ride details."""

    def __init__(self, ride_id: str, fields: list[str]) -> None:
        super().__init__(f"ride {ride_id} already recorded with different {', '.join(fields)}")
        self.ride_id = ride_id
        self.fields = fields


def record_completed_ride(conn: Connection, event: RideCompleted, now: datetime) -> RideResult:
    if event.completed_at > now + MAX_COMPLETED_AT_FUTURE_SKEW:
        raise CompletedAtInFuture()

    vehicle_type = normalize_vehicle_type(event.vehicle_type)
    credits_milli = calculate_credits_milli(event.distance_km, vehicle_type)

    ensure_driver(conn, event.driver_id, now)

    # The primary key arbitrates duplicates. A concurrent inserter of the same ride_id blocks here
    # until the first transaction commits, then gets no row back and falls through to the replay.
    inserted = conn.execute(
        text(
            """
            INSERT INTO rides (ride_id, driver_id, distance_km, vehicle_type, completed_at,
                               credits_milli, received_at)
            VALUES (:ride_id, :driver_id, :distance_km, :vehicle_type, :completed_at,
                    :credits_milli, :now)
            ON CONFLICT (ride_id) DO NOTHING
            RETURNING ride_id
            """
        ),
        {
            "ride_id": event.ride_id,
            "driver_id": event.driver_id,
            "distance_km": event.distance_km,
            "vehicle_type": vehicle_type,
            "completed_at": event.completed_at,
            "credits_milli": credits_milli,
            "now": now,
        },
    ).first()

    if inserted is None:
        return _replay_duplicate(conn, event, vehicle_type)

    expires_at = None
    if credits_milli > 0:
        expires_at = _issue_credits(conn, event, credits_milli, now)

    return RideResult(event.ride_id, event.driver_id, credits_milli, expires_at, created=True)


def _issue_credits(conn: Connection, event: RideCompleted, credits_milli: int, now: datetime) -> datetime:
    lot = conn.execute(
        text(
            """
            INSERT INTO credit_lots (driver_id, ride_id, amount_milli, remaining_milli, earned_at, expires_at)
            VALUES (:driver_id, :ride_id, :amount, :amount, :completed_at,
                    :completed_at + CAST(:lifetime AS interval))
            RETURNING id, expires_at
            """
        ),
        {
            "driver_id": event.driver_id,
            "ride_id": event.ride_id,
            "amount": credits_milli,
            "completed_at": event.completed_at,
            "lifetime": CREDIT_LIFETIME,
        },
    ).one()

    conn.execute(
        text(
            """
            INSERT INTO ledger_entries (driver_id, entry_type, amount_milli, ride_id, lot_id, reason, created_at)
            VALUES (:driver_id, 'EARN', :amount, :ride_id, :lot_id, :reason, :now)
            """
        ),
        {
            "driver_id": event.driver_id,
            "amount": credits_milli,
            "ride_id": event.ride_id,
            "lot_id": lot.id,
            "reason": f"EV ride {event.ride_id}: {event.distance_km.normalize():f} km",
            "now": now,
        },
    )
    return lot.expires_at


def _replay_duplicate(conn: Connection, event: RideCompleted, vehicle_type: str) -> RideResult:
    original = conn.execute(
        text(
            """
            SELECT r.driver_id, r.distance_km, r.vehicle_type, r.completed_at, r.credits_milli, l.expires_at
            FROM rides r
            LEFT JOIN credit_lots l ON l.ride_id = r.ride_id
            WHERE r.ride_id = :ride_id
            """
        ),
        {"ride_id": event.ride_id},
    ).one()

    # Compared by value: 12.3 == 12.300, and the same instant in different offsets is equal.
    conflicting = [
        name
        for name, recorded, received in (
            ("driver_id", original.driver_id, event.driver_id),
            ("distance_km", original.distance_km, event.distance_km),
            ("vehicle_type", original.vehicle_type, vehicle_type),
            ("completed_at", original.completed_at, event.completed_at),
        )
        if recorded != received
    ]
    if conflicting:
        raise RideConflict(event.ride_id, conflicting)

    return RideResult(
        event.ride_id, original.driver_id, original.credits_milli, original.expires_at, created=False
    )
