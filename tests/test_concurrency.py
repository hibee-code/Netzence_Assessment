"""Races exercised with real threads, each on its own connection and transaction (see concurrency.py).

Ride races are run with the driver ALREADY existing: for a brand-new driver, the concurrent
`INSERT INTO drivers ... ON CONFLICT DO NOTHING` makes every worker wait for the first one to commit,
which serializes them and would hide a race on the ride insert itself.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, text

from app.services.rides import RideCompleted, RideConflict, RideResult, record_completed_ride
from tests.concurrency import run_concurrently

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 5, 31, 9, 0, tzinfo=UTC)


def _event(ride_id: str = "r1", distance_km: str = "12.3") -> RideCompleted:
    return RideCompleted(ride_id, "d1", Decimal(distance_km), "electric", COMPLETED_AT)


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("drivers", "rides", "credit_lots", "ledger_entries")
        }


@pytest.fixture
def existing_driver(test_engine: Engine) -> None:
    with test_engine.begin() as conn:
        conn.execute(text("INSERT INTO drivers (id) VALUES ('d1')"))


@pytest.mark.parametrize("driver_exists", [True, False], ids=["existing-driver", "new-driver"])
def test_20_concurrent_identical_completions_record_exactly_once(
    concurrent_engine: Engine, test_engine: Engine, request: pytest.FixtureRequest, driver_exists: bool
) -> None:
    if driver_exists:
        request.getfixturevalue("existing_driver")
    event = _event()

    results = run_concurrently(
        concurrent_engine, 20, lambda conn, _: record_completed_ride(conn, event, NOW)
    )

    assert [r for r in results if isinstance(r, Exception)] == []
    rides = [r for r in results if isinstance(r, RideResult)]
    assert sum(r.created for r in rides) == 1  # one winner; 19 replays of the original
    assert {(r.credits_milli, r.expires_at) for r in rides} == {(1476, datetime(2027, 5, 31, 9, 0, tzinfo=UTC))}
    assert _counts(test_engine) == {"drivers": 1, "rides": 1, "credit_lots": 1, "ledger_entries": 1}
    with test_engine.connect() as conn:
        earn_total = conn.execute(
            text("SELECT SUM(amount_milli) FROM ledger_entries WHERE entry_type = 'EARN'")
        ).scalar_one()
    assert earn_total == 1476


@pytest.mark.usefixtures("existing_driver")
def test_concurrent_conflicting_completions_keep_exactly_one_version(
    concurrent_engine: Engine, test_engine: Engine
) -> None:
    # Same ride_id, 10 different distances (1..10 km), all at once.
    def complete(conn: Connection, index: int) -> RideResult:
        return record_completed_ride(conn, _event(distance_km=str(index + 1)), NOW)

    results = run_concurrently(concurrent_engine, 10, complete)

    winners = [r for r in results if isinstance(r, RideResult)]
    conflicts = [r for r in results if isinstance(r, RideConflict)]
    assert len(winners) == 1 and winners[0].created
    assert len(conflicts) == 9
    assert all(c.fields == ["distance_km"] for c in conflicts)
    with test_engine.connect() as conn:
        lot_amount = conn.execute(text("SELECT amount_milli FROM credit_lots")).scalar_one()
    assert lot_amount == winners[0].credits_milli


def test_concurrent_first_rides_of_a_new_driver_all_succeed(
    concurrent_engine: Engine, test_engine: Engine
) -> None:
    # 20 different rides for a driver who doesn't exist yet: the driver upsert must not fail any of them.
    results = run_concurrently(
        concurrent_engine, 20, lambda conn, i: record_completed_ride(conn, _event(ride_id=f"r{i}"), NOW)
    )

    assert [r for r in results if isinstance(r, Exception)] == []
    assert _counts(test_engine) == {"drivers": 1, "rides": 20, "credit_lots": 20, "ledger_entries": 20}
