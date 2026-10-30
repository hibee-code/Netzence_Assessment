"""Proves each test starts from an empty database with identities reset.

The same test runs twice. If clean_db did not truncate between tests, the second run
would see the first run's row and get id 2.
"""

import pytest
from sqlalchemy import Engine, text


@pytest.mark.parametrize("run", ["first", "second"])
def test_each_test_starts_with_empty_tables_and_reset_ids(test_engine: Engine, run: str) -> None:
    with test_engine.begin() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
        ).scalars().all()
        for table in tables:
            assert conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one() == 0, table

        conn.execute(text("INSERT INTO drivers (id) VALUES ('d1')"))
        redemption_id = conn.execute(
            text(
                "INSERT INTO redemptions (driver_id, amount_milli, idempotency_key, balance_after_milli) "
                "VALUES ('d1', 100, :key, 0) RETURNING id"
            ),
            {"key": run},
        ).scalar_one()

    assert redemption_id == 1
