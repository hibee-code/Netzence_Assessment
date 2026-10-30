"""The drivers row doubles as the per-driver lock: every transaction that spends or claws back a
driver's credits locks it first, so those operations run one at a time per driver."""

from datetime import datetime

from sqlalchemy import Connection, text


def ensure_driver(conn: Connection, driver_id: str, now: datetime) -> None:
    conn.execute(
        text("INSERT INTO drivers (id, created_at) VALUES (:driver_id, :now) ON CONFLICT (id) DO NOTHING"),
        {"driver_id": driver_id, "now": now},
    )


def lock_driver(conn: Connection, driver_id: str) -> bool:
    """Lock the driver row until the transaction ends. False if the driver doesn't exist."""
    row = conn.execute(
        text("SELECT id FROM drivers WHERE id = :driver_id FOR UPDATE"), {"driver_id": driver_id}
    ).first()
    return row is not None
