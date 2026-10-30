"""POST /rides/completed through the API (TestClient + real Postgres + FakeClock at 2026-06-01T12:00Z)."""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.fakes import FakeClock


def _ride(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ride_id": "r1",
        "driver_id": "d1",
        "distance_km": "12.3",
        "vehicle_type": "electric",
        "completed_at": "2026-05-31T09:00:00Z",
    }
    return payload | overrides


def _count(engine: Engine, sql: str, **params: Any) -> int:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def _balance_milli(engine: Engine, driver_id: str = "d1") -> int:
    return _count(
        engine, "SELECT COALESCE(SUM(remaining_milli), 0) FROM credit_lots WHERE driver_id = :d", d=driver_id
    )


# --- recording ------------------------------------------------------------------------

def test_electric_ride_records_lot_and_earn_entry(client: TestClient, test_engine: Engine) -> None:
    response = client.post("/rides/completed", json=_ride())

    assert response.status_code == 201
    assert response.json() == {
        "ride_id": "r1",
        "driver_id": "d1",
        "credits": "1.476",  # 12.3 km * 120 g/km = 1476 g
        "expires_at": "2027-05-31T09:00:00Z",
        "status": "recorded",
    }
    with test_engine.connect() as conn:
        lot = conn.execute(text("SELECT * FROM credit_lots")).one()
        entry = conn.execute(text("SELECT * FROM ledger_entries")).one()
    assert (lot.driver_id, lot.ride_id, lot.amount_milli, lot.remaining_milli) == ("d1", "r1", 1476, 1476)
    assert (entry.entry_type, entry.amount_milli, entry.ride_id, entry.lot_id) == ("EARN", 1476, "r1", lot.id)
    assert entry.reason == "EV ride r1: 12.3 km"


def test_distance_as_json_number_is_parsed_exactly(client: TestClient) -> None:
    response = client.post("/rides/completed", json=_ride(distance_km=12.3))
    assert response.json()["credits"] == "1.476"


def test_credits_round_down_once_at_ingestion(client: TestClient, test_engine: Engine) -> None:
    response = client.post("/rides/completed", json=_ride(distance_km="10.001"))  # 1200.12 g

    assert response.json()["credits"] == "1.200"
    assert _balance_milli(test_engine) == 1200


@pytest.mark.parametrize(
    ("completed_at", "expires_at"),
    [
        ("2026-05-31T09:00:00Z", "2027-05-31T09:00:00Z"),
        ("2024-02-29T10:00:00Z", "2025-02-28T10:00:00Z"),  # leap day: no Feb 29 in 2025
        ("2026-01-31T10:00:00+05:00", "2027-01-31T05:00:00Z"),  # offset input, UTC calendar
    ],
)
def test_expires_12_months_after_completion(client: TestClient, completed_at: str, expires_at: str) -> None:
    response = client.post("/rides/completed", json=_ride(completed_at=completed_at))
    assert response.json()["expires_at"] == expires_at


@pytest.mark.parametrize("vehicle_type", ["petrol", "diesel", "hybrid"])
def test_non_electric_ride_is_recorded_without_credits(
    client: TestClient, test_engine: Engine, vehicle_type: str
) -> None:
    response = client.post("/rides/completed", json=_ride(vehicle_type=vehicle_type))

    assert response.status_code == 201
    assert response.json()["credits"] == "0.000"
    assert response.json()["expires_at"] is None
    assert _count(test_engine, "SELECT count(*) FROM rides WHERE credits_milli = 0") == 1
    assert _count(test_engine, "SELECT count(*) FROM credit_lots") == 0
    assert _count(test_engine, "SELECT count(*) FROM ledger_entries") == 0


def test_electric_ride_too_short_to_earn_creates_no_lot(client: TestClient, test_engine: Engine) -> None:
    response = client.post("/rides/completed", json=_ride(distance_km="0.008"))  # 0.96 g

    assert response.status_code == 201
    assert response.json()["credits"] == "0.000"
    assert _count(test_engine, "SELECT count(*) FROM credit_lots") == 0


def test_vehicle_type_is_stored_lowercase(client: TestClient, test_engine: Engine) -> None:
    response = client.post("/rides/completed", json=_ride(vehicle_type="Electric"))

    assert response.json()["credits"] == "1.476"
    assert _count(test_engine, "SELECT count(*) FROM rides WHERE vehicle_type = 'electric'") == 1


# --- duplicates -----------------------------------------------------------------------

def test_sequential_duplicate_returns_200_and_balance_unchanged(
    client: TestClient, test_engine: Engine, clock: FakeClock
) -> None:
    first = client.post("/rides/completed", json=_ride())
    clock.advance(timedelta(hours=1))  # a retry arrives later
    second = client.post("/rides/completed", json=_ride())

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json() | {"status": "duplicate"}
    assert _balance_milli(test_engine) == 1476
    assert _count(test_engine, "SELECT count(*) FROM credit_lots") == 1
    assert _count(test_engine, "SELECT count(*) FROM ledger_entries") == 1


def test_duplicate_of_non_electric_ride_is_detected(client: TestClient) -> None:
    client.post("/rides/completed", json=_ride(vehicle_type="petrol"))
    second = client.post("/rides/completed", json=_ride(vehicle_type="petrol"))

    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_equivalent_representations_are_duplicates_not_conflicts(client: TestClient) -> None:
    client.post("/rides/completed", json=_ride())
    second = client.post(
        "/rides/completed",
        json=_ride(
            distance_km="12.300",  # same value
            vehicle_type="ELECTRIC",  # same after normalization
            completed_at="2026-05-31T11:00:00+02:00",  # same instant
        ),
    )
    assert second.status_code == 200


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("driver_id", "d2"),
        ("distance_km", "12.4"),
        ("vehicle_type", "petrol"),
        ("completed_at", "2026-05-31T09:00:01Z"),
    ],
)
def test_conflicting_duplicate_returns_409_and_writes_nothing(
    client: TestClient, test_engine: Engine, field: str, value: str
) -> None:
    client.post("/rides/completed", json=_ride())

    response = client.post("/rides/completed", json=_ride(**{field: value}))

    assert response.status_code == 409
    assert response.json() == {"detail": "ride_conflict", "ride_id": "r1", "conflicting_fields": [field]}
    assert _balance_milli(test_engine) == 1476
    assert _count(test_engine, "SELECT count(*) FROM ledger_entries") == 1
    # The transaction rolled back: not even the driver upsert for "d2" survived.
    assert _count(test_engine, "SELECT count(*) FROM drivers") == 1


# --- validation -----------------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides",
    [
        {"completed_at": "2026-05-31T09:00:00"},  # no timezone
        {"distance_km": "1.2345"},  # more than 3 dp
        {"distance_km": "0"},
        {"distance_km": "-5"},
        {"distance_km": "abc"},
        {"distance_km": "NaN"},
        {"distance_km": "10000000"},  # exceeds NUMERIC(10,3)
        {"ride_id": ""},
        {"vehicle_type": ""},
    ],
    ids=lambda o: f"{next(iter(o))}={next(iter(o.values()))!r}",
)
def test_invalid_payload_returns_422(client: TestClient, test_engine: Engine, overrides: dict[str, Any]) -> None:
    response = client.post("/rides/completed", json=_ride(**overrides))

    assert response.status_code == 422
    assert _count(test_engine, "SELECT count(*) FROM rides") == 0


@pytest.mark.parametrize("missing", ["ride_id", "driver_id", "distance_km", "vehicle_type", "completed_at"])
def test_missing_field_returns_422(client: TestClient, missing: str) -> None:
    payload = _ride()
    del payload[missing]
    assert client.post("/rides/completed", json=payload).status_code == 422


def test_completed_at_more_than_5_minutes_ahead_is_rejected(client: TestClient, test_engine: Engine) -> None:
    # Clock is 2026-06-01T12:00:00Z.
    response = client.post("/rides/completed", json=_ride(completed_at="2026-06-01T12:05:00.001Z"))

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "completed_at_in_future"
    assert _count(test_engine, "SELECT count(*) FROM drivers") == 0


def test_completed_at_exactly_5_minutes_ahead_is_accepted(client: TestClient) -> None:
    response = client.post("/rides/completed", json=_ride(completed_at="2026-06-01T12:05:00Z"))
    assert response.status_code == 201
