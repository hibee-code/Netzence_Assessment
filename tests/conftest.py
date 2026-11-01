from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.pool import NullPool

from app.clock import get_clock
from app.config import get_settings
from app.db import create_db_engine, get_engine
from app.main import app
from tests.fakes import TEST_NOW, FakeClock

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _run_alembic(engine: Engine, action: str, revision: str) -> None:
    """Run `alembic <action> <revision>` (upgrade/downgrade) against the given engine."""
    config = Config(str(ALEMBIC_INI))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, action)(config, revision)


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    url = get_settings().test_database_url
    if url is None:
        pytest.fail(
            "POSTGRES_TEST_DB is not set in .env. Run tests with: docker compose run --rm api pytest -v"
        )
    engine = create_db_engine(url, pool_pre_ping=True)
    _run_alembic(engine, "upgrade", "head")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def concurrent_engine(test_engine: Engine) -> Iterator[Engine]:
    """Engine for concurrency tests: NullPool, so every thread gets its own real connection
    (the default pool caps simultaneous connections and would queue the workers)."""
    engine = create_db_engine(test_engine.url, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def truncate_sql(test_engine: Engine) -> str:
    """TRUNCATE for every table the migrations created, so new tables are covered automatically."""
    with test_engine.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version' ORDER BY tablename"
            )
        ).scalars().all()
        quote = conn.dialect.identifier_preparer.quote
    return f"TRUNCATE {', '.join(quote(t) for t in tables)} RESTART IDENTITY CASCADE"


@pytest.fixture(autouse=True)
def clean_db(test_engine: Engine, truncate_sql: str) -> None:
    with test_engine.begin() as conn:
        conn.execute(text(truncate_sql))


@pytest.fixture
def run_alembic(test_engine: Engine) -> Callable[[str, str], None]:
    return lambda action, revision: _run_alembic(test_engine, action, revision)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(TEST_NOW)


@pytest.fixture
def client(test_engine: Engine, clock: FakeClock) -> Iterator[TestClient]:
    app.dependency_overrides[get_engine] = lambda: test_engine
    app.dependency_overrides[get_clock] = lambda: clock
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
