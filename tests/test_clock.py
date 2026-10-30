from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.clock import Clock, SystemClock, get_clock
from tests.fakes import FakeClock


def test_system_clock_returns_current_utc_time() -> None:
    before = datetime.now(UTC)
    now = SystemClock().now()
    after = datetime.now(UTC)

    assert now.utcoffset() == timedelta(0)
    assert before <= now <= after


def test_get_clock_defaults_to_system_clock() -> None:
    assert isinstance(get_clock(), SystemClock)


def test_fake_clock_only_moves_when_told() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    assert clock.now() == clock.now() == datetime(2026, 1, 1, tzinfo=UTC)

    clock.advance(timedelta(days=365))
    assert clock.now() == datetime(2027, 1, 1, tzinfo=UTC)

    clock.set(datetime(2024, 2, 29, tzinfo=UTC))
    assert clock.now() == datetime(2024, 2, 29, tzinfo=UTC)


def test_fake_clock_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 1, 1))
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2026, 1, 1))


def test_endpoints_receive_the_overridden_clock(clock: FakeClock) -> None:
    # A throwaway app proves the get_clock dependency is swappable, the same way
    # the `client` fixture swaps it on the real app.
    probe = FastAPI()

    @probe.get("/now")
    def read_now(c: Annotated[Clock, Depends(get_clock)]) -> dict[str, str]:
        return {"now": c.now().isoformat()}

    probe.dependency_overrides[get_clock] = lambda: clock
    with TestClient(probe) as tc:
        assert tc.get("/now").json() == {"now": "2026-06-01T12:00:00+00:00"}
        clock.advance(timedelta(hours=1))
        assert tc.get("/now").json() == {"now": "2026-06-01T13:00:00+00:00"}
