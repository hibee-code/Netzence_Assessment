"""POST /drivers/{id}/redeem and GET /drivers/{id}/balance through the API.
The `clock` fixture is at 2026-06-01T12:00Z; every lot below is unexpired unless stated."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine

from tests.factories import add_lot, ledger, remaining_by_lot


def _redeem(client: TestClient, amount: Any, key: str | None = "key-1", driver_id: str = "d1", **body: Any) -> Response:
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(f"/drivers/{driver_id}/redeem", json={"amount": amount, **body}, headers=headers)


def _balance(client: TestClient, driver_id: str = "d1") -> str:
    return client.get(f"/drivers/{driver_id}/balance").json()["balance"]


def _completed(year: int, month: int, day: int = 1) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=UTC)


# --- balance --------------------------------------------------------------------------

def test_balance_sums_unexpired_lots(client: TestClient, test_engine: Engine) -> None:
    add_lot(test_engine, "r1", 12_345, _completed(2026, 1))
    add_lot(test_engine, "r2", 655, _completed(2026, 2))

    assert client.get("/drivers/d1/balance").json() == {"driver_id": "d1", "balance": "13.000"}


def test_unknown_driver_has_zero_balance(client: TestClient) -> None:
    response = client.get("/drivers/nobody/balance")

    assert response.status_code == 200
    assert response.json() == {"driver_id": "nobody", "balance": "0.000"}


# --- FIFO -----------------------------------------------------------------------------

def test_redeem_spends_oldest_expiring_lots_first(client: TestClient, test_engine: Engine) -> None:
    # Inserted in an order that differs from expiry order, so id order != FIFO order.
    late = add_lot(test_engine, "r-late", 50_000, _completed(2026, 5))
    early = add_lot(test_engine, "r-early", 20_000, _completed(2025, 9))
    middle = add_lot(test_engine, "r-middle", 30_000, _completed(2026, 1))

    response = _redeem(client, "35")

    assert response.status_code == 201
    assert response.json() == {
        "redemption_id": 1,
        "amount": "35.000",
        "balance_after": "65.000",
        "allocations": [
            {"lot_id": early, "ride_id": "r-early", "amount": "20.000"},
            {"lot_id": middle, "ride_id": "r-middle", "amount": "15.000"},
        ],
    }
    assert remaining_by_lot(test_engine) == {late: 50_000, early: 0, middle: 15_000}
    assert _balance(client) == "65.000"


def test_lots_expiring_at_the_same_time_are_spent_in_id_order(client: TestClient, test_engine: Engine) -> None:
    first = add_lot(test_engine, "r1", 10_000, _completed(2026, 1))
    second = add_lot(test_engine, "r2", 10_000, _completed(2026, 1))

    allocations = _redeem(client, "12").json()["allocations"]

    assert [(a["lot_id"], a["amount"]) for a in allocations] == [(first, "10.000"), (second, "2.000")]


def test_redeem_writes_one_negative_redeem_entry(client: TestClient, test_engine: Engine) -> None:
    add_lot(test_engine, "r1", 10_000, _completed(2026, 1))
    add_lot(test_engine, "r2", 10_000, _completed(2026, 2))

    _redeem(client, "15.5", reason="Coffee voucher")

    assert ledger(test_engine, "REDEEM") == [(-15_500, None)]


def test_redeeming_the_exact_balance_leaves_zero(client: TestClient, test_engine: Engine) -> None:
    add_lot(test_engine, "r1", 10_000, _completed(2026, 1))

    assert _redeem(client, 10).json()["balance_after"] == "0.000"
    assert _balance(client) == "0.000"


# --- insufficient balance -------------------------------------------------------------

def test_insufficient_balance_returns_409_and_changes_nothing(client: TestClient, test_engine: Engine) -> None:
    lot = add_lot(test_engine, "r1", 10_000, _completed(2026, 1))

    response = _redeem(client, "10.001")

    assert response.status_code == 409
    assert response.json() == {"detail": "insufficient_balance", "available": "10.000"}
    assert remaining_by_lot(test_engine) == {lot: 10_000}
    assert ledger(test_engine, "REDEEM") == []


def test_unknown_driver_cannot_redeem(client: TestClient) -> None:
    response = _redeem(client, "1", driver_id="nobody")

    assert response.status_code == 409
    assert response.json() == {"detail": "insufficient_balance", "available": "0.000"}


# --- idempotency ----------------------------------------------------------------------

@pytest.mark.parametrize("key", [None, ""], ids=["missing", "empty"])
def test_idempotency_key_is_required(client: TestClient, test_engine: Engine, key: str | None) -> None:
    add_lot(test_engine, "r1", 10_000, _completed(2026, 1))

    response = _redeem(client, "1", key=key)

    assert response.status_code == 400
    assert response.json() == {"detail": "idempotency_key_required"}
    assert _balance(client) == "10.000"


def test_same_key_replays_the_original_and_charges_once(client: TestClient, test_engine: Engine) -> None:
    add_lot(test_engine, "r1", 100_000, _completed(2026, 1))
    first = _redeem(client, "30", key="abc")
    add_lot(test_engine, "r2", 5_000, _completed(2026, 2))  # balance changes before the retry

    retry = _redeem(client, "30", key="abc")

    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json() == first.json()  # including the ORIGINAL balance_after (70.000)
    assert _balance(client) == "75.000"
    assert ledger(test_engine, "REDEEM") == [(-30_000, None)]


def test_same_key_with_different_amount_returns_409(client: TestClient, test_engine: Engine) -> None:
    add_lot(test_engine, "r1", 100_000, _completed(2026, 1))
    _redeem(client, "30", key="abc")

    response = _redeem(client, "31", key="abc")

    assert response.status_code == 409
    assert response.json() == {"detail": "idempotency_key_reused_with_different_amount"}
    assert _balance(client) == "70.000"


def test_idempotency_keys_are_scoped_per_driver(client: TestClient, test_engine: Engine) -> None:
    add_lot(test_engine, "r1", 10_000, _completed(2026, 1), driver_id="d1")
    add_lot(test_engine, "r2", 10_000, _completed(2026, 1), driver_id="d2")

    assert _redeem(client, "4", key="same", driver_id="d1").status_code == 201
    assert _redeem(client, "4", key="same", driver_id="d2").status_code == 201


# --- validation -----------------------------------------------------------------------

@pytest.mark.parametrize("amount", ["0", "-1", "1.0001", "abc", "NaN", None])
def test_invalid_amount_returns_422(client: TestClient, test_engine: Engine, amount: Any) -> None:
    add_lot(test_engine, "r1", 10_000, _completed(2026, 1))

    assert _redeem(client, amount).status_code == 422
    assert _balance(client) == "10.000"
