from datetime import UTC, datetime, timedelta

# The instant every test starts at (the `clock` fixture); tests move it with set()/advance().
TEST_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeClock:
    """Controllable Clock for tests. Time only moves when the test moves it."""

    def __init__(self, now: datetime) -> None:
        self._now = self._require_aware(now)

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = self._require_aware(now)

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    @staticmethod
    def _require_aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("FakeClock needs a timezone-aware datetime")
        return value
