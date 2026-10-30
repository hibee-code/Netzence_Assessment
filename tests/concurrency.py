"""Real parallelism for race-condition tests.

Each worker thread holds ITS OWN database connection and transaction. A Barrier releases all
workers at the same instant, so their statements genuinely overlap inside Postgres. (TestClient is
deliberately not used: it can serialize requests and hide races.)
"""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

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
