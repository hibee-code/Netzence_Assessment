from collections.abc import Callable

from sqlalchemy import Engine, text

EXPECTED_TABLES = {
    "drivers",
    "rides",
    "credit_lots",
    "redemptions",
    "redemption_allocations",
    "ledger_entries",
    "ride_cancellations",
    "alembic_version",
}


def _public_tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
        return {row.table_name for row in rows}


def _index_definitions(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"))
        return {row.indexname: row.indexdef for row in rows}


def test_upgrade_head_creates_all_tables(test_engine: Engine) -> None:
    assert _public_tables(test_engine) == EXPECTED_TABLES


def test_partial_and_lookup_indexes_exist(test_engine: Engine) -> None:
    indexes = _index_definitions(test_engine)

    assert "(driver_id, expires_at, id) WHERE (remaining_milli > 0)" in indexes["ix_credit_lots_spendable"]
    assert "UNIQUE" in indexes["uq_ledger_entries_expiry_lot_id"]
    assert "(lot_id) WHERE (entry_type = 'EXPIRY'::text)" in indexes["uq_ledger_entries_expiry_lot_id"]
    assert "UNIQUE" in indexes["uq_ledger_entries_reversal_ride_id"]
    assert "(ride_id) WHERE (entry_type = 'REVERSAL'::text)" in indexes["uq_ledger_entries_reversal_ride_id"]
    assert "(driver_id, created_at, id)" in indexes["ix_ledger_entries_driver_history"]
    assert "(lot_id)" in indexes["ix_redemption_allocations_lot_id"]


def test_all_milli_amounts_are_bigint(test_engine: Engine) -> None:
    with test_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name LIKE '%\\_milli'"
            )
        ).all()

    # rides.credits, lots.amount/remaining, redemptions.amount/balance_after, allocations, ledger
    assert len(rows) == 7
    assert {row.data_type for row in rows} == {"bigint"}


def test_all_timestamps_are_timezone_aware(test_engine: Engine) -> None:
    with test_engine.connect() as conn:
        types = conn.execute(
            text(
                "SELECT DISTINCT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name <> 'alembic_version' "
                "AND data_type LIKE 'timestamp%'"
            )
        ).scalars().all()

    assert types == ["timestamp with time zone"]


def test_downgrade_to_base_then_upgrade_to_head(
    test_engine: Engine, run_alembic: Callable[[str, str], None]
) -> None:
    try:
        run_alembic("downgrade", "base")
        assert _public_tables(test_engine) == {"alembic_version"}
    finally:
        # Always restore the schema so a failure here doesn't cascade into other tests.
        run_alembic("upgrade", "head")

    assert _public_tables(test_engine) == EXPECTED_TABLES
