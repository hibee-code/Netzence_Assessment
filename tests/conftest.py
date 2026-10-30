from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from app.config import get_settings
from app.db import get_engine
from app.main import app


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    url = get_settings().test_database_url
    if not url:
        pytest.fail(
            "POSTGRES_TEST_DB is not set in .env. Run tests with: docker compose run --rm api pytest -v"
        )
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    app.dependency_overrides[get_engine] = lambda: test_engine
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
