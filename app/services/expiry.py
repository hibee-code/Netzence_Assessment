"""Lazy, idempotent expiry.

Expired-but-unspent credits are written to the ledger (EXPIRY) the next time the driver's balance,
history or a redemption touches them. Correctness never depends on this having run: balance and
redemption always filter on `expires_at > now` themselves. This only makes history complete.
"""

from datetime import datetime

from sqlalchemy import Connection, text


def expire_lots(conn: Connection, driver_id: str, now: datetime) -> None:
    # FOR UPDATE: a concurrent caller waits here, then re-checks `remaining_milli > 0` against the
    # committed row and skips lots already expired. Locking in (expires_at, id) order, the same
    # order redemption uses, prevents deadlocks between the two.
    expired = conn.execute(
        text(
            """
            SELECT id, ride_id, remaining_milli, expires_at
            FROM credit_lots
            WHERE driver_id = :driver_id AND expires_at <= :now AND remaining_milli > 0
            ORDER BY expires_at, id
            FOR UPDATE
            """
        ),
        {"driver_id": driver_id, "now": now},
    ).all()
    if not expired:
        return

    # Not ledger.add_entry: EXPIRY needs ON CONFLICT DO NOTHING (one EXPIRY per lot) and a batch insert.
    conn.execute(
        text(
            """
            INSERT INTO ledger_entries (driver_id, entry_type, amount_milli, ride_id, lot_id, reason, created_at)
            VALUES (:driver_id, 'EXPIRY', :amount, :ride_id, :lot_id, :reason, :now)
            ON CONFLICT (lot_id) WHERE entry_type = 'EXPIRY' DO NOTHING
            """
        ),
        [
            {
                "driver_id": driver_id,
                "amount": -lot.remaining_milli,
                "ride_id": lot.ride_id,
                "lot_id": lot.id,
                "reason": f"Credits from ride {lot.ride_id} expired at {lot.expires_at.isoformat()}",
                "now": now,
            }
            for lot in expired
        ],
    )
    conn.execute(
        text("UPDATE credit_lots SET remaining_milli = 0 WHERE id = ANY(:lot_ids)"),
        {"lot_ids": [lot.id for lot in expired]},
    )
