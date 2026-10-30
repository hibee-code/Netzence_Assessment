"""Credits expire 12 months after completed_at. Spendable means expires_at > now, so a lot is
already expired AT the exact 12-month instant. Expiry is written to the ledger lazily, once."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tests.factories import add_lot, ledger, remaining_by_lot
from tests.fakes import FakeClock

# The `clock` fixture is at 2026-06-01T12:00Z.
EXPIRED = datetime(2025, 5, 1, 12, 0, tzinfo=UTC)  # expired 2026-05-01
FRESH = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # expires 2027-01-01
ONE_MICROSECOND = timedelta(microseconds=1)


def _balance(client: TestClient) -> str:
    return client.get("/drivers/d1/balance").json()["balance"]


def _redeem(client: TestClient, amount: str, key: str = "key-1") -> dict:
    return client.post("/drivers/d1/redeem", json={"amount": amount}, headers={"Idempotency-Key": key}).json()


def test_expired_lots_are_excluded_from_balance(client: TestClient, test_engine: Engine) -> None:
    add_lot(test_engine, "r-old", 40_000, EXPIRED)
    add_lot(test_engine, "r-new", 25_000, FRESH)

    assert _balance(client) == "25.000"


def test_redeem_skips_expired_lots(client: TestClient, test_engine: Engine) -> None:
    add_lot(test_engine, "r-old", 40_000, EXPIRED)  # first in FIFO order, but expired
    fresh = add_lot(test_engine, "r-new", 25_000, FRESH)

    redemption = _redeem(client, "10")
    too_much = client.post("/drivers/d1/redeem", json={"amount": "16"}, headers={"Idempotency-Key": "key-2"})

    assert redemption["allocations"] == [{"lot_id": fresh, "ride_id": "r-new", "amount": "10.000"}]
    assert too_much.status_code == 409
    assert too_much.json()["available"] == "15.000"


@pytest.mark.parametrize(
    ("completed_at", "expires_at"),
    [
        (datetime(2025, 6, 1, 12, 0, tzinfo=UTC), datetime(2026, 6, 1, 12, 0, tzinfo=UTC)),
        # Leap day: 2025 has no Feb 29, so 12 months later is Feb 28.
        (datetime(2024, 2, 29, 10, 0, tzinfo=UTC), datetime(2025, 2, 28, 10, 0, tzinfo=UTC)),
    ],
    ids=["regular", "leap-day"],
)
def test_lot_is_spendable_until_but_not_at_the_12_month_instant(
    client: TestClient, test_engine: Engine, clock: FakeClock, completed_at: datetime, expires_at: datetime
) -> None:
    add_lot(test_engine, "r1", 10_000, completed_at)

    clock.set(expires_at - ONE_MICROSECOND)
    assert _balance(client) == "10.000"

    clock.set(expires_at)
    assert _balance(client) == "0.000"


def test_redeem_at_the_exact_expiry_instant_is_refused(
    client: TestClient, test_engine: Engine, clock: FakeClock
) -> None:
    add_lot(test_engine, "r1", 10_000, datetime(2025, 6, 1, 12, 0, tzinfo=UTC))
    clock.set(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))

    response = client.post("/drivers/d1/redeem", json={"amount": "1"}, headers={"Idempotency-Key": "k"})

    assert response.status_code == 409


def test_expiry_entry_is_written_once_however_often_queried(client: TestClient, test_engine: Engine) -> None:
    lot = add_lot(test_engine, "r-old", 40_000, EXPIRED)

    for _ in range(3):
        assert _balance(client) == "0.000"

    assert ledger(test_engine, "EXPIRY") == [(-40_000, lot)]
    assert remaining_by_lot(test_engine) == {lot: 0}


def test_expiry_records_only_the_unspent_remainder(
    client: TestClient, test_engine: Engine, clock: FakeClock
) -> None:
    lot = add_lot(test_engine, "r1", 40_000, FRESH)
    _redeem(client, "15")

    clock.set(datetime(2027, 1, 1, 12, 0, tzinfo=UTC))  # FRESH's expiry instant
    _balance(client)

    assert ledger(test_engine, "EXPIRY") == [(-25_000, lot)]


def test_fully_spent_lot_writes_no_expiry_entry(client: TestClient, test_engine: Engine, clock: FakeClock) -> None:
    add_lot(test_engine, "r1", 10_000, FRESH)
    _redeem(client, "10")

    clock.set(datetime(2027, 6, 1, tzinfo=UTC))
    _balance(client)

    assert ledger(test_engine, "EXPIRY") == []


def test_redeem_materializes_expiry_too(client: TestClient, test_engine: Engine) -> None:
    old = add_lot(test_engine, "r-old", 40_000, EXPIRED)
    add_lot(test_engine, "r-new", 25_000, FRESH)

    _redeem(client, "5")

    assert ledger(test_engine, "EXPIRY") == [(-40_000, old)]
