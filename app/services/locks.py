"""Every lock the service takes, in one place.

Lock order (always acquired in this order, never the reverse, so transactions can't deadlock):
  1. ride      - completion and cancellation of the same ride_id
  2. driver    - anything that spends or claws back a driver's credits
  3. lots      - in (expires_at, id) order (see app/services/lots.py and expiry.py)
All locks are transaction-scoped: released on commit or rollback.
"""

from sqlalchemy import Connection, text


def lock_ride(conn: Connection, ride_id: str) -> None:
    """Serialize completion and cancellation of one ride_id, even before the ride row exists
    (a cancellation can arrive first). Advisory lock on a 64-bit hash of the id; a hash collision
    only makes two unrelated rides wait for each other, never breaks correctness."""
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:ride_id, 0))"), {"ride_id": ride_id})


def lock_driver(conn: Connection, driver_id: str) -> bool:
    """Lock the driver row. False if the driver doesn't exist."""
    row = conn.execute(
        text("SELECT id FROM drivers WHERE id = :driver_id FOR UPDATE"), {"driver_id": driver_id}
    ).first()
    return row is not None
