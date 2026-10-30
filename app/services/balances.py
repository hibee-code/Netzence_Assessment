"""Read side of a driver's account: balance and history. Both materialize lazy expiry first, so
they run in a (writing) transaction."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, text

from app.services.expiry import expire_lots


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    entry_type: str
    amount_milli: int  # signed: + credit, - debit
    ride_id: str | None
    redemption_id: int | None
    reason: str
    created_at: datetime


def get_balance_milli(conn: Connection, driver_id: str, now: datetime) -> int:
    """Spendable (unexpired) credits. Unknown drivers simply have 0."""
    expire_lots(conn, driver_id, now)
    return conn.execute(
        text(
            """
            SELECT COALESCE(SUM(remaining_milli), 0)::bigint  -- SUM(bigint) is NUMERIC in Postgres
            FROM credit_lots
            WHERE driver_id = :driver_id AND expires_at > :now AND remaining_milli > 0
            """
        ),
        {"driver_id": driver_id, "now": now},
    ).scalar_one()


def get_history(conn: Connection, driver_id: str, now: datetime, limit: int, offset: int) -> list[LedgerEntry]:
    """Every credit and debit with its reason, oldest first. Unknown drivers have none."""
    expire_lots(conn, driver_id, now)
    rows = conn.execute(
        text(
            """
            SELECT id, entry_type, amount_milli, ride_id, redemption_id, reason, created_at
            FROM ledger_entries
            WHERE driver_id = :driver_id
            ORDER BY created_at, id
            LIMIT :limit OFFSET :offset
            """
        ),
        {"driver_id": driver_id, "limit": limit, "offset": offset},
    )
    return [LedgerEntry(*row) for row in rows]
