"""GET /drivers/{id}/history. The `clock` fixture starts at 2026-06-01T12:00Z."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.factories import add_lot, ledger
from tests.fakes import FakeClock

HOUR = timedelta(hours=1)


def _complete(client: TestClient, ride_id: str, distance_km: str, completed_at: str, driver_id: str = "d1") -> None:
    response = client.post(
        "/rides/completed",
        json={
            "ride_id": ride_id,
            "driver_id": driver_id,
            "distance_km": distance_km,
            "vehicle_type": "electric",
            "completed_at": completed_at,
        },
    )
    assert response.status_code == 201


def _history(client: TestClient, driver_id: str = "d1", **params: Any) -> list[dict[str, Any]]:
    response = client.get(f"/drivers/{driver_id}/history", params=params)
    assert response.status_code == 200
    return response.json()["entries"]


def test_history_shows_every_credit_and_debit_with_its_reason(client: TestClient, clock: FakeClock) -> None:
    _complete(client, "r1", "100", "2026-05-31T09:00:00Z")  # 12.000, expires 2027-05-31T09:00Z
    clock.advance(HOUR)
    _complete(client, "r2", "50", "2026-05-31T10:00:00Z")  # 6.000
    clock.advance(HOUR)
    client.post("/drivers/d1/redeem", json={"amount": "5", "reason": "coffee"}, headers={"Idempotency-Key": "k1"})
    clock.advance(HOUR)
    client.post("/rides/cancelled", json={"ride_id": "r2", "reason": "refund"})
    clock.set(datetime(2027, 5, 31, 9, 0, tzinfo=UTC))  # r1's remaining 7.000 expires

    assert _history(client) == [
        {"id": 1, "type": "EARN", "amount": "12.000", "ride_id": "r1", "redemption_id": None,
         "reason": "EV ride r1: 100 km", "created_at": "2026-06-01T12:00:00Z"},
        {"id": 2, "type": "EARN", "amount": "6.000", "ride_id": "r2", "redemption_id": None,
         "reason": "EV ride r2: 50 km", "created_at": "2026-06-01T13:00:00Z"},
        {"id": 3, "type": "REDEEM", "amount": "-5.000", "ride_id": None, "redemption_id": 1,
         "reason": "Redeemed 5.000 credits: coffee", "created_at": "2026-06-01T14:00:00Z"},
        {"id": 4, "type": "REVERSAL", "amount": "-6.000", "ride_id": "r2", "redemption_id": None,
         "reason": "Ride r2 cancelled (refund); reversed 6.000 of 6.000", "created_at": "2026-06-01T15:00:00Z"},
        {"id": 5, "type": "EXPIRY", "amount": "-7.000", "ride_id": "r1", "redemption_id": None,
         "reason": "Credits from ride r1 expired at 2027-05-31T09:00:00+00:00", "created_at": "2027-05-31T09:00:00Z"},
    ]
    assert client.get("/drivers/d1/balance").json()["balance"] == "0.000"


def test_history_is_ordered_by_created_at_then_id(client: TestClient, clock: FakeClock) -> None:
    _complete(client, "recorded-first", "1", "2026-05-31T09:00:00Z")  # id 1, created 12:00
    clock.set(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    _complete(client, "recorded-second", "1", "2026-05-31T08:00:00Z")  # id 2, created 10:00
    _complete(client, "recorded-third", "1", "2026-05-31T08:00:00Z")  # id 3, created 10:00

    assert [e["ride_id"] for e in _history(client)] == ["recorded-second", "recorded-third", "recorded-first"]


def test_history_materializes_expiry_once(client: TestClient, test_engine: Engine) -> None:
    lot = add_lot(test_engine, "r-old", 40_000, datetime(2025, 5, 1, tzinfo=UTC))  # expired at clock time

    first = _history(client)
    second = _history(client)

    assert [e["type"] for e in first] == ["EARN", "EXPIRY"]
    assert second == first
    assert ledger(test_engine, "EXPIRY") == [(-40_000, lot)]


def test_history_only_includes_the_requested_driver(client: TestClient) -> None:
    _complete(client, "mine", "1", "2026-05-31T09:00:00Z", driver_id="d1")
    _complete(client, "theirs", "1", "2026-05-31T09:00:00Z", driver_id="d2")

    assert [e["ride_id"] for e in _history(client, "d1")] == ["mine"]


def test_unknown_driver_has_empty_history(client: TestClient) -> None:
    response = client.get("/drivers/nobody/history")

    assert response.status_code == 200
    assert response.json() == {"driver_id": "nobody", "entries": []}


# --- paging ---------------------------------------------------------------------------

@pytest.fixture
def five_rides(client: TestClient, clock: FakeClock) -> None:
    for i in range(1, 6):
        _complete(client, f"r{i}", "1", "2026-05-31T09:00:00Z")
        clock.advance(HOUR)


@pytest.mark.usefixtures("five_rides")
@pytest.mark.parametrize(
    ("params", "ride_ids"),
    [
        ({}, ["r1", "r2", "r3", "r4", "r5"]),
        ({"limit": 2}, ["r1", "r2"]),
        ({"limit": 2, "offset": 2}, ["r3", "r4"]),
        ({"offset": 4}, ["r5"]),
        ({"offset": 5}, []),
    ],
)
def test_limit_and_offset_page_through_history(
    client: TestClient, params: dict[str, int], ride_ids: list[str]
) -> None:
    assert [e["ride_id"] for e in _history(client, **params)] == ride_ids


def test_default_limit_is_100(client: TestClient, test_engine: Engine) -> None:
    with test_engine.begin() as conn:  # 101 rides with lots and EARN entries, in bulk
        conn.execute(text("INSERT INTO drivers (id) VALUES ('d1')"))
        conn.execute(
            text(
                "INSERT INTO rides (ride_id, driver_id, distance_km, vehicle_type, completed_at, credits_milli) "
                "SELECT 'r' || g, 'd1', 1, 'electric', '2026-05-31T09:00Z', 120 FROM generate_series(1, 101) g"
            )
        )
        conn.execute(
            text(
                "INSERT INTO credit_lots (driver_id, ride_id, amount_milli, remaining_milli, earned_at, expires_at) "
                "SELECT 'd1', ride_id, 120, 120, completed_at, completed_at + interval '12 months' FROM rides"
            )
        )
        conn.execute(
            text(
                "INSERT INTO ledger_entries (driver_id, entry_type, amount_milli, ride_id, lot_id, reason) "
                "SELECT 'd1', 'EARN', 120, ride_id, id, 'bulk' FROM credit_lots"
            )
        )

    assert len(_history(client)) == 100
    assert len(_history(client, limit=500)) == 101


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"limit": "x"}, {"offset": -1}])
def test_invalid_paging_returns_422(client: TestClient, params: dict[str, Any]) -> None:
    assert client.get("/drivers/d1/history", params=params).status_code == 422
