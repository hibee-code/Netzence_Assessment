from datetime import datetime

from sqlalchemy import Connection, text


def ensure_driver(conn: Connection, driver_id: str, now: datetime) -> None:
    conn.execute(
        text("INSERT INTO drivers (id, created_at) VALUES (:driver_id, :now) ON CONFLICT (id) DO NOTHING"),
        {"driver_id": driver_id, "now": now},
    )
