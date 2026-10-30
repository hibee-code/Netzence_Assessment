"""Spending from credit lots, shared by redemption and cancellation clawback."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Connection, Row, text

Debit = tuple[int, int]  # (lot_id, amount_milli)


def lock_spendable_lots(
    conn: Connection, driver_id: str, now: datetime, include_ride_id: str | None = None
) -> Sequence[Row]:
    """Lock the driver's unexpired lots with credit left, oldest-expiring first (FIFO order).
    `include_ride_id` also locks that ride's own lot, whatever its state, in the same ordered pass."""
    return conn.execute(
        text(
            """
            SELECT id, ride_id, remaining_milli
            FROM credit_lots
            WHERE driver_id = :driver_id
              AND ((expires_at > :now AND remaining_milli > 0) OR ride_id = :include_ride_id)
            ORDER BY expires_at, id
            FOR UPDATE
            """
        ),
        {"driver_id": driver_id, "now": now, "include_ride_id": include_ride_id},
    ).all()


def drain_fifo(lots: Sequence[Row], amount_milli: int) -> list[Debit]:
    """Take up to `amount_milli` from lots in the given order. May cover less if lots run out."""
    debits = []
    left = amount_milli
    for lot in lots:
        if left == 0:
            break
        take = min(lot.remaining_milli, left)
        if take > 0:
            debits.append((lot.id, take))
            left -= take
    return debits


def debit_lots(conn: Connection, debits: list[Debit]) -> None:
    if debits:
        conn.execute(
            text("UPDATE credit_lots SET remaining_milli = remaining_milli - :amount WHERE id = :lot_id"),
            [{"lot_id": lot_id, "amount": amount} for lot_id, amount in debits],
        )
