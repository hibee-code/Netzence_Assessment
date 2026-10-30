"""Real parallelism for race-condition tests.

Each worker thread holds ITS OWN database connection and transaction. A Barrier releases all
workers at the same instant, so their statements genuinely overlap inside Postgres. (TestClient is
deliberately not used: it can serialize requests and hide races.)
"""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, TypeVar

from sqlalchemy import Connection, Engine

T = TypeVar("T")


def run_concurrently(
    engine: Engine, workers: int, task: Callable[[Connection, int], T]
) -> list[T | Exception]:
    """Run task(conn, worker_index) in `workers` threads at once, one transaction each.

    Returns each worker's result, or the exception it raised (its transaction rolled back).
    The engine must allow `workers` simultaneous connections (use NullPool).
    """
    barrier = threading.Barrier(workers, timeout=30)

    def run_one(index: int) -> T | Exception:
        with engine.connect() as conn:  # connect BEFORE the barrier, so connecting isn't the race
            barrier.wait()
            try:
                with conn.begin():
                    return task(conn, index)
            except Exception as exc:  # noqa: BLE001 - returned to the test for assertions
                return exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run_one, range(workers)))


def run_interleaved(
    engine: Engine,
    first: Callable[[Connection], Any],
    second: Callable[[Connection], T],
    head_start_seconds: float = 0.5,
) -> T:
    """Force the riskiest interleaving deterministically, instead of hoping threads hit it.

    `first` runs in a transaction that stays OPEN while `second` starts on another connection.
    `second` gets `head_start_seconds` to either finish (nothing stopped it) or block on a lock held
    by `first`. Then `first` commits, and `second`'s result is returned.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            first(conn)

            def run_second() -> T:
                with engine.begin() as other:
                    return second(other)

            future = pool.submit(run_second)
            wait([future], timeout=head_start_seconds)
            transaction.commit()  # before leaving the pool: `second` may be waiting on our locks
        return future.result(timeout=30)
    finally:
        pool.shutdown()
