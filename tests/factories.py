"""Test data shared across modules. Lots are inserted directly so tests can pick exact amounts
(the 120 g/km formula can't produce e.g. exactly 25.000 credits)."""

from datetime import datetime

from sqlalchemy import Engine, text

from app.config import CREDIT_LIFETIME


def add_lot(
    engine: Engine, ride_id: str, amount_milli: int, completed_at: datetime, driver_id: str = "d1"
) -> int:
    """A completed EV ride with its credit lot and EARN entry. Returns the lot id."""
    params = {
        "driver_id": driver_id,
        "ride_id": ride_id,
        "amount": amount_milli,
        "completed_at": completed_at,
        "lifetime": CREDIT_LIFETIME,
    }
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO drivers (id) VALUES (:driver_id) ON CONFLICT DO NOTHING"), params)
        conn.execute(
            text(
                "INSERT INTO rides (ride_id, driver_id, distance_km, vehicle_type, completed_at, credits_milli) "
                "VALUES (:ride_id, :driver_id, 1, 'electric', :completed_at, :amount)"
            ),
            params,
        )
        lot_id: int = conn.execute(
            text(
                "INSERT INTO credit_lots (driver_id, ride_id, amount_milli, remaining_milli, earned_at, expires_at) "
                "VALUES (:driver_id, :ride_id, :amount, :amount, :completed_at, "
                "        :completed_at + CAST(:lifetime AS interval)) "
                "RETURNING id"
            ),
            params,
        ).scalar_one()
        # created_at set explicitly (as the app does) rather than the DB's now(), so history
        # ordering is consistent with the entries the app writes from the injected clock.
        conn.execute(
            text(
                "INSERT INTO ledger_entries (driver_id, entry_type, amount_milli, ride_id, lot_id, reason, created_at) "
                "VALUES (:driver_id, 'EARN', :amount, :ride_id, :lot_id, 'test earn', :completed_at)"
            ),
            params | {"lot_id": lot_id},
        )
    return lot_id


def remaining_by_lot(engine: Engine) -> dict[int, int]:
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT id, remaining_milli FROM credit_lots ORDER BY id")).tuples().all())


def ledger(engine: Engine, entry_type: str) -> list[tuple[int, int | None]]:
    """(amount_milli, lot_id) of every ledger entry of a type, oldest first."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT amount_milli, lot_id FROM ledger_entries WHERE entry_type = :t ORDER BY id"),
                {"t": entry_type},
            ).tuples()
        )


def ledger_and_lot_totals(engine: Engine) -> tuple[int, int]:
    """(sum of all ledger amounts, sum of all lots' remaining). Must always be equal: every change
    to a lot's remaining is recorded by exactly one ledger entry (EARN/REDEEM/REVERSAL/EXPIRY)."""
    with engine.connect() as conn:
        ledger_total, lot_total = conn.execute(
            text(
                "SELECT (SELECT COALESCE(SUM(amount_milli), 0)::bigint FROM ledger_entries), "
                "       (SELECT COALESCE(SUM(remaining_milli), 0)::bigint FROM credit_lots)"
            )
        ).one()
    return ledger_total, lot_total
