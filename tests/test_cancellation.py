"""POST /rides/cancelled through the API. The `clock` fixture is at 2026-06-01T12:00Z."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from tests.factories import add_lot, ledger, ledger_and_lot_totals, remaining_by_lot
from tests.fakes import FakeClock

JAN, FEB, MAR = (datetime(2026, month, 1, tzinfo=UTC) for month in (1, 2, 3))


def _complete(client: TestClient, ride_id: str = "r1", **overrides: Any) -> Response:
    payload = {
        "ride_id": ride_id,
        "driver_id": "d1",
        "distance_km": "100",  # 12.000 credits
        "vehicle_type": "electric",
        "completed_at": "2026-05-31T09:00:00Z",
    }
    return client.post("/rides/completed", json=payload | overrides)


def _cancel(client: TestClient, ride_id: str = "r1", reason: str | None = None) -> Response:
    return client.post("/rides/cancelled", json={"ride_id": ride_id, "reason": reason})


def _redeem(client: TestClient, amount: str, key: str) -> None:
    response = client.post("/drivers/d1/redeem", json={"amount": amount}, headers={"Idempotency-Key": key})
    assert response.status_code == 201


def _balance(client: TestClient) -> str:
    return client.get("/drivers/d1/balance").json()["balance"]


def _reversal_reason(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT reason FROM ledger_entries WHERE entry_type = 'REVERSAL'")).scalar_one()


@pytest.fixture(autouse=True)
def ledger_always_matches_lots(test_engine: Engine) -> Any:
    yield
    ledger_total, lot_total = ledger_and_lot_totals(test_engine)
    assert ledger_total == lot_total


# --- full reversal --------------------------------------------------------------------

def test_full_reversal_keeps_the_original_earn_and_adds_a_reversal(client: TestClient, test_engine: Engine) -> None:
    _complete(client)
    with test_engine.connect() as conn:
        earn_before = conn.execute(text("SELECT * FROM ledger_entries WHERE entry_type = 'EARN'")).one()

    response = _cancel(client, reason="refund")

    assert response.status_code == 200
    assert response.json() == {"ride_id": "r1", "status": "cancelled", "reversed": "12.000", "shortfall": "0.000"}
    assert _balance(client) == "0.000"
    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT * FROM ledger_entries WHERE entry_type = 'EARN'")).one() == earn_before
    lot_id = earn_before.lot_id
    assert ledger(test_engine, "REVERSAL") == [(-12_000, lot_id)]
    assert _reversal_reason(test_engine) == "Ride r1 cancelled (refund); reversed 12.000 of 12.000"
    # And through the API: history still shows the original entry, followed by the reversal.
    history = client.get("/drivers/d1/history").json()["entries"]
    assert [(e["type"], e["amount"], e["ride_id"]) for e in history] == [
        ("EARN", "12.000", "r1"),
        ("REVERSAL", "-12.000", "r1"),
    ]


def test_duplicate_cancellation_replays_the_original_outcome(client: TestClient, test_engine: Engine) -> None:
    _complete(client)
    first = _cancel(client, reason="refund")

    again = _cancel(client, reason="a different reason")

    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(ledger(test_engine, "REVERSAL")) == 1
    assert _balance(client) == "0.000"


def test_cancelling_a_ride_that_earned_nothing_writes_no_ledger_entry(
    client: TestClient, test_engine: Engine
) -> None:
    _complete(client, vehicle_type="petrol")

    response = _cancel(client)

    assert response.json() == {"ride_id": "r1", "status": "cancelled", "reversed": "0.000", "shortfall": "0.000"}
    assert ledger(test_engine, "REVERSAL") == []
    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT cancelled_at FROM rides")).scalar_one() is not None


# --- cancel before complete (tombstone) -----------------------------------------------

def test_cancel_before_complete_is_applied_when_the_ride_arrives(client: TestClient, test_engine: Engine) -> None:
    pending = _cancel(client, reason="refund")
    pending_again = _cancel(client)

    completed = _complete(client)

    assert pending.status_code == pending_again.status_code == 202
    assert pending.json() == {"ride_id": "r1", "status": "pending_completion", "reversed": None, "shortfall": None}
    assert completed.status_code == 201
    assert completed.json()["status"] == "cancelled"
    assert completed.json()["credits"] == "12.000"  # what was earned, then reversed
    lot_id = remaining_by_lot(test_engine).popitem()[0]
    assert ledger(test_engine, "EARN") == [(12_000, lot_id)]
    assert ledger(test_engine, "REVERSAL") == [(-12_000, lot_id)]
    assert _balance(client) == "0.000"
    assert _reversal_reason(test_engine).startswith("Ride r1 cancelled (refund)")  # tombstone's reason
    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM ride_cancellations WHERE applied_at IS NOT NULL")).scalar() == 1


def test_after_a_tombstone_is_applied_retries_replay(client: TestClient, test_engine: Engine) -> None:
    _cancel(client)
    _complete(client)

    assert _cancel(client).json() == {"ride_id": "r1", "status": "cancelled", "reversed": "12.000", "shortfall": "0.000"}
    assert _complete(client).json()["status"] == "duplicate"
    assert len(ledger(test_engine, "REVERSAL")) == 1


# --- partial clawback -----------------------------------------------------------------

def test_partially_redeemed_ride_claws_back_from_other_lots(client: TestClient, test_engine: Engine) -> None:
    cancelled = add_lot(test_engine, "r-cancel", 10_000, JAN)
    other = add_lot(test_engine, "r-other", 20_000, FEB)
    _redeem(client, "4", "k1")  # FIFO: 4 from r-cancel, leaving 6 there

    response = _cancel(client, "r-cancel")

    # 6 taken back from the lot itself + 4 clawed back from r-other = the full 10
    assert response.json() == {"ride_id": "r-cancel", "status": "cancelled", "reversed": "10.000", "shortfall": "0.000"}
    assert remaining_by_lot(test_engine) == {cancelled: 0, other: 16_000}
    assert _balance(client) == "16.000"


def test_clawback_takes_from_the_oldest_expiring_other_lot_first(client: TestClient, test_engine: Engine) -> None:
    add_lot(test_engine, "r-cancel", 10_000, JAN)
    later = add_lot(test_engine, "r-later", 10_000, MAR)
    sooner = add_lot(test_engine, "r-sooner", 10_000, FEB)
    _redeem(client, "6", "k1")  # all from r-cancel

    _cancel(client, "r-cancel")

    remaining = remaining_by_lot(test_engine)
    assert (remaining[sooner], remaining[later]) == (4_000, 10_000)


def test_shortfall_is_recorded_and_the_balance_never_goes_negative(client: TestClient, test_engine: Engine) -> None:
    add_lot(test_engine, "r-cancel", 10_000, JAN)
    _redeem(client, "10", "k1")  # fully spent
    add_lot(test_engine, "r-other", 8_000, FEB)  # only 8 left to claw back from

    response = _cancel(client, "r-cancel")

    assert response.json() == {"ride_id": "r-cancel", "status": "cancelled", "reversed": "8.000", "shortfall": "2.000"}
    assert _balance(client) == "0.000"
    assert min(remaining_by_lot(test_engine).values()) == 0
    assert _reversal_reason(test_engine) == (
        "Ride r-cancel cancelled; reversed 8.000 of 10.000; shortfall 2.000 unrecoverable"
    )


def test_shortfall_when_nothing_at_all_is_left(client: TestClient, test_engine: Engine) -> None:
    lot = add_lot(test_engine, "r-cancel", 10_000, JAN)
    _redeem(client, "10", "k1")

    response = _cancel(client, "r-cancel")

    assert response.json()["reversed"] == "0.000"
    assert response.json()["shortfall"] == "10.000"
    assert ledger(test_engine, "REVERSAL") == [(0, lot)]  # the cancellation still shows in history
    assert _balance(client) == "0.000"


def test_credits_that_expired_unspent_are_not_clawed_back(
    client: TestClient, test_engine: Engine, clock: FakeClock
) -> None:
    cancelled = add_lot(test_engine, "r-cancel", 10_000, datetime(2025, 5, 1, tzinfo=UTC))  # expires 2026-05-01
    other = add_lot(test_engine, "r-other", 20_000, JAN)
    clock.set(datetime(2026, 4, 1, tzinfo=UTC))
    _redeem(client, "4", "k1")  # from r-cancel, before it expired
    clock.set(datetime(2026, 6, 1, tzinfo=UTC))  # r-cancel has now expired with 6 unspent

    response = _cancel(client, "r-cancel")

    # Only the 4 the driver actually used is clawed back; the 6 simply expired.
    assert response.json() == {"ride_id": "r-cancel", "status": "cancelled", "reversed": "4.000", "shortfall": "0.000"}
    assert ledger(test_engine, "EXPIRY") == [(-6_000, cancelled)]
    assert remaining_by_lot(test_engine) == {cancelled: 0, other: 16_000}


# --- validation -----------------------------------------------------------------------

@pytest.mark.parametrize("body", [{}, {"ride_id": ""}, {"ride_id": "r1", "reason": "x" * 501}])
def test_invalid_cancellation_returns_422(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/rides/cancelled", json=body).status_code == 422
