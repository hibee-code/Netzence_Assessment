"""Writing to the append-only ledger. Entries are only ever inserted, never updated or deleted."""

from datetime import datetime

from sqlalchemy import Connection, text


def add_entry(
    conn: Connection,
    *,
    driver_id: str,
    entry_type: str,
    amount_milli: int,
    reason: str,
    now: datetime,
    ride_id: str | None = None,
    lot_id: int | None = None,
    redemption_id: int | None = None,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO ledger_entries
                (driver_id, entry_type, amount_milli, ride_id, lot_id, redemption_id, reason, created_at)
            VALUES (:driver_id, :entry_type, :amount, :ride_id, :lot_id, :redemption_id, :reason, :now)
            """
        ),
        {
            "driver_id": driver_id,
            "entry_type": entry_type,
            "amount": amount_milli,
            "ride_id": ride_id,
            "lot_id": lot_id,
            "redemption_id": redemption_id,
            "reason": reason,
            "now": now,
        },
    )
