from datetime import datetime

from sqlalchemy import Connection, text

from app.services.expiry import expire_lots


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
