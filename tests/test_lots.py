from types import SimpleNamespace

from app.services.lots import drain_fifo


def _lots(*remaining: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(id=i + 1, remaining_milli=r) for i, r in enumerate(remaining)]


def test_takes_from_lots_in_the_given_order() -> None:
    assert drain_fifo(_lots(10, 20, 30), 25) == [(1, 10), (2, 15)]  # type: ignore[arg-type]


def test_exact_cover_stops_at_the_last_needed_lot() -> None:
    assert drain_fifo(_lots(10, 20, 30), 30) == [(1, 10), (2, 20)]  # type: ignore[arg-type]


def test_covers_only_what_exists_when_lots_run_out() -> None:
    assert drain_fifo(_lots(10, 5), 100) == [(1, 10), (2, 5)]  # type: ignore[arg-type]


def test_skips_empty_lots_and_zero_amount() -> None:
    assert drain_fifo(_lots(0, 7), 5) == [(2, 5)]  # type: ignore[arg-type]
    assert drain_fifo(_lots(10), 0) == []  # type: ignore[arg-type]
