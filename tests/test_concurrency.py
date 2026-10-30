"""Races exercised with real threads, each on its own connection and transaction (see concurrency.py).

Ride races are run with the driver ALREADY existing: for a brand-new driver, the concurrent
`INSERT INTO drivers ... ON CONFLICT DO NOTHING` makes every worker wait for the first one to commit,
which serializes them and would hide a race on the ride insert itself.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, text

from app.services.balances import get_balance_milli
from app.services.cancellations import CancellationResult, cancel_ride
from app.services.redemptions import InsufficientBalance, Redemption, redeem
from app.services.rides import RideCompleted, RideConflict, RideResult, record_completed_ride
from tests.concurrency import run_concurrently, run_interleaved
from tests.factories import add_lot, ledger, ledger_and_lot_totals, remaining_by_lot

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


# --- redemption ----------------------------------------------------------------------

@pytest.fixture
def balance_of_100_credits(test_engine: Engine) -> None:
    """4 lots x 25 credits with different expiries, so each 30-credit redemption spans two lots."""
    for month in (1, 2, 3, 4):
        add_lot(test_engine, f"r{month}", 25_000, datetime(2026, month, 1, tzinfo=UTC))


def _balance(engine: Engine) -> int:
    with engine.begin() as conn:
        return get_balance_milli(conn, "d1", NOW)


@pytest.mark.usefixtures("balance_of_100_credits")
def test_key_10_concurrent_redeems_of_30_from_100_exactly_3_succeed(
    concurrent_engine: Engine, test_engine: Engine
) -> None:
    results = run_concurrently(
        concurrent_engine, 10, lambda conn, i: redeem(conn, "d1", 30_000, f"key-{i}", None, NOW)
    )

    succeeded = [r for r in results if isinstance(r, Redemption)]
    refused = [r for r in results if isinstance(r, InsufficientBalance)]
    assert len(succeeded) == 3
    assert len(refused) == 7  # and nothing else: no deadlocks, no constraint violations
    assert _balance(test_engine) == 10_000
    assert min(remaining_by_lot(test_engine).values()) >= 0
    assert sorted(r.balance_after_milli for r in succeeded) == [10_000, 40_000, 70_000]
    assert sum(amount for amount, _ in ledger(test_engine, "REDEEM")) == -90_000
    # Every refusal saw a real balance: 10 credits left after the third success.
    assert {r.available_milli for r in refused} == {10_000}


@pytest.mark.usefixtures("balance_of_100_credits")
def test_concurrent_redeems_with_the_same_key_charge_once(
    concurrent_engine: Engine, test_engine: Engine
) -> None:
    results = run_concurrently(
        concurrent_engine, 10, lambda conn, _: redeem(conn, "d1", 30_000, "same-key", None, NOW)
    )

    assert all(isinstance(r, Redemption) for r in results), results
    redemptions = [r for r in results if isinstance(r, Redemption)]
    assert sum(r.created for r in redemptions) == 1
    assert {(r.redemption_id, r.balance_after_milli) for r in redemptions} == {(1, 70_000)}
    assert _balance(test_engine) == 70_000
    assert ledger(test_engine, "REDEEM") == [(-30_000, None)]



def test_same_key_retry_of_a_full_balance_redemption_replays_instead_of_refusing(
    concurrent_engine: Engine, test_engine: Engine
) -> None:
    # Redeeming the WHOLE balance: a retry that re-checked the balance after the original
    # committed would see 0 and answer "insufficient". It must replay the original instead.
    add_lot(test_engine, "r1", 30_000, datetime(2026, 1, 1, tzinfo=UTC))

    results = run_concurrently(
        concurrent_engine, 10, lambda conn, _: redeem(conn, "d1", 30_000, "same-key", None, NOW)
    )

    assert all(isinstance(r, Redemption) for r in results), results
    assert {r.redemption_id for r in results if isinstance(r, Redemption)} == {1}
    assert _balance(test_engine) == 0

# --- expiry ---------------------------------------------------------------------------

def test_concurrent_balance_reads_write_one_expiry_entry(concurrent_engine: Engine, test_engine: Engine) -> None:
    lot = add_lot(test_engine, "r-old", 40_000, datetime(2025, 5, 1, tzinfo=UTC))  # expired at NOW

    results = run_concurrently(concurrent_engine, 10, lambda conn, _: get_balance_milli(conn, "d1", NOW))

    assert results == [0] * 10
    assert ledger(test_engine, "EXPIRY") == [(-40_000, lot)]


# --- cancellation ---------------------------------------------------------------------

def test_concurrent_identical_cancellations_reverse_once(concurrent_engine: Engine, test_engine: Engine) -> None:
    lot = add_lot(test_engine, "r1", 12_000, datetime(2026, 1, 1, tzinfo=UTC))

    results = run_concurrently(concurrent_engine, 10, lambda conn, _: cancel_ride(conn, "r1", None, NOW))

    assert set(results) == {CancellationResult("r1", "cancelled", 12_000, 0)}
    assert ledger(test_engine, "REVERSAL") == [(-12_000, lot)]
    assert remaining_by_lot(test_engine) == {lot: 0}


def _assert_recorded_and_cancelled_once(engine: Engine) -> None:
    with engine.connect() as conn:
        cancelled = conn.execute(text("SELECT count(*) FROM rides WHERE cancelled_at IS NOT NULL")).scalar_one()
        pending = conn.execute(text("SELECT count(*) FROM ride_cancellations WHERE applied_at IS NULL")).scalar_one()
    assert (cancelled, pending) == (1, 0)
    assert len(ledger(engine, "EARN")) == len(ledger(engine, "REVERSAL")) == 1
    assert ledger_and_lot_totals(engine) == (0, 0)


@pytest.mark.usefixtures("existing_driver")
def test_cancellation_arriving_while_its_completion_is_in_flight_is_not_lost(
    concurrent_engine: Engine, test_engine: Engine
) -> None:
    # The completion has inserted the ride and checked for a tombstone (none), but not committed.
    # Without the ride lock, the cancellation wouldn't see the ride, would write a tombstone nobody
    # ever reads, and the ride would stay uncancelled.
    result = run_interleaved(
        concurrent_engine,
        first=lambda conn: record_completed_ride(conn, _event(), NOW),
        second=lambda conn: cancel_ride(conn, "r1", None, NOW),
    )

    assert result == CancellationResult("r1", "cancelled", 1476, 0)
    _assert_recorded_and_cancelled_once(test_engine)


@pytest.mark.usefixtures("existing_driver")
def test_completion_arriving_while_its_cancellation_is_in_flight_applies_it(
    concurrent_engine: Engine, test_engine: Engine
) -> None:
    # The cancellation has written its tombstone, but not committed. Without the ride lock, the
    # completion wouldn't see the tombstone and would record the ride uncancelled.
    result = run_interleaved(
        concurrent_engine,
        first=lambda conn: cancel_ride(conn, "r1", None, NOW),
        second=lambda conn: record_completed_ride(conn, _event(), NOW),
    )

    assert result.created and result.cancelled
    _assert_recorded_and_cancelled_once(test_engine)


def test_cancellation_racing_redemptions_never_overdraws(concurrent_engine: Engine, test_engine: Engine) -> None:
    add_lot(test_engine, "r-cancel", 50_000, datetime(2026, 1, 1, tzinfo=UTC))
    add_lot(test_engine, "r-other", 50_000, datetime(2026, 2, 1, tzinfo=UTC))

    def cancel_or_redeem(conn: Connection, worker: int) -> object:
        if worker == 0:
            return cancel_ride(conn, "r-cancel", None, NOW)
        return redeem(conn, "d1", 10_000, f"key-{worker}", None, NOW)

    results = run_concurrently(concurrent_engine, 10, cancel_or_redeem)

    unexpected = [r for r in results if isinstance(r, Exception) and not isinstance(r, InsufficientBalance)]
    assert unexpected == []
    cancellation = next(r for r in results if isinstance(r, CancellationResult))
    assert cancellation.reversed_milli + cancellation.shortfall_milli == 50_000  # type: ignore[operator]
    assert min(remaining_by_lot(test_engine).values()) >= 0
    ledger_total, lot_total = ledger_and_lot_totals(test_engine)
    assert ledger_total == lot_total == _balance(test_engine)
