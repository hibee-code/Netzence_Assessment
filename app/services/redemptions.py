"""Redeeming credits: oldest-expiring first (FIFO), never below zero, idempotent per
(driver, Idempotency-Key).

Concurrency: the driver row lock serializes all redemptions of a driver. It is taken BEFORE the
idempotency check, so a retry racing its original waits, then sees the committed redemption and
replays it, instead of spending again or wrongly answering "insufficient balance".
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, text

from app.money import from_milli
from app.services.drivers import lock_driver
from app.services.expiry import expire_lots


@dataclass(frozen=True)
class Allocation:
    lot_id: int
    ride_id: str
    amount_milli: int


@dataclass(frozen=True)
class Redemption:
    redemption_id: int
    driver_id: str
    amount_milli: int
    balance_after_milli: int
    allocations: tuple[Allocation, ...]
    created: bool  # False when replayed for a repeated Idempotency-Key


class InsufficientBalance(Exception):
    def __init__(self, available_milli: int) -> None:
        super().__init__(f"insufficient balance: {available_milli} milli available")
        self.available_milli = available_milli


class IdempotencyKeyReused(Exception):
    """The Idempotency-Key was already used for a redemption of a different amount."""


def redeem(
    conn: Connection,
    driver_id: str,
    amount_milli: int,
    idempotency_key: str,
    reason: str | None,
    now: datetime,
) -> Redemption:
    if not lock_driver(conn, driver_id):
        raise InsufficientBalance(available_milli=0)

    existing = conn.execute(
        text("SELECT id, amount_milli FROM redemptions WHERE driver_id = :driver_id AND idempotency_key = :key"),
        {"driver_id": driver_id, "key": idempotency_key},
    ).first()
    if existing is not None:
        if existing.amount_milli != amount_milli:
            raise IdempotencyKeyReused()
        return _load_redemption(conn, existing.id, created=False)

    expire_lots(conn, driver_id, now)
    lots = conn.execute(
        text(
            """
            SELECT id, remaining_milli
            FROM credit_lots
            WHERE driver_id = :driver_id AND expires_at > :now AND remaining_milli > 0
            ORDER BY expires_at, id
            FOR UPDATE
            """
        ),
        {"driver_id": driver_id, "now": now},
    ).all()

    available = sum(lot.remaining_milli for lot in lots)
    if available < amount_milli:
        raise InsufficientBalance(available)

    redemption_id = conn.execute(
        text(
            """
            INSERT INTO redemptions (driver_id, amount_milli, idempotency_key, reason, balance_after_milli, created_at)
            VALUES (:driver_id, :amount, :key, :reason, :balance_after, :now)
            RETURNING id
            """
        ),
        {
            "driver_id": driver_id,
            "amount": amount_milli,
            "key": idempotency_key,
            "reason": reason,
            "balance_after": available - amount_milli,
            "now": now,
        },
    ).scalar_one()

    # FIFO: drain lots in expiry order until the amount is covered.
    allocations = []
    left = amount_milli
    for lot in lots:
        if left == 0:
            break
        take = min(lot.remaining_milli, left)
        allocations.append({"redemption_id": redemption_id, "lot_id": lot.id, "amount": take})
        left -= take

    conn.execute(
        text("UPDATE credit_lots SET remaining_milli = remaining_milli - :amount WHERE id = :lot_id"),
        allocations,
    )
    conn.execute(
        text(
            "INSERT INTO redemption_allocations (redemption_id, lot_id, amount_milli) "
            "VALUES (:redemption_id, :lot_id, :amount)"
        ),
        allocations,
    )
    conn.execute(
        text(
            """
            INSERT INTO ledger_entries (driver_id, entry_type, amount_milli, redemption_id, reason, created_at)
            VALUES (:driver_id, 'REDEEM', :amount, :redemption_id, :reason, :now)
            """
        ),
        {
            "driver_id": driver_id,
            "amount": -amount_milli,
            "redemption_id": redemption_id,
            "reason": f"Redeemed {from_milli(amount_milli)} credits" + (f": {reason}" if reason else ""),
            "now": now,
        },
    )
    return _load_redemption(conn, redemption_id, created=True)


def _load_redemption(conn: Connection, redemption_id: int, created: bool) -> Redemption:
    """Single source for the response, so a replay is identical to the original."""
    redemption = conn.execute(
        text("SELECT id, driver_id, amount_milli, balance_after_milli FROM redemptions WHERE id = :id"),
        {"id": redemption_id},
    ).one()
    allocations = conn.execute(
        text(
            """
            SELECT a.lot_id, l.ride_id, a.amount_milli
            FROM redemption_allocations a
            JOIN credit_lots l ON l.id = a.lot_id
            WHERE a.redemption_id = :id
            ORDER BY l.expires_at, l.id
            """
        ),
        {"id": redemption_id},
    ).all()
    return Redemption(
        redemption_id=redemption.id,
        driver_id=redemption.driver_id,
        amount_milli=redemption.amount_milli,
        balance_after_milli=redemption.balance_after_milli,
        allocations=tuple(Allocation(a.lot_id, a.ride_id, a.amount_milli) for a in allocations),
        created=created,
    )
