"""Injectable time source.

Endpoints get a Clock via the `get_clock` dependency and pass `clock.now()` into service
functions, so tests can control time (expiry boundaries, leap days) without patching.
"""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant as a timezone-aware datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


_system_clock = SystemClock()


def get_clock() -> Clock:
    return _system_clock
