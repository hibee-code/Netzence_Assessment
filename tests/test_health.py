from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from app.db import get_engine
from app.main import app


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


@pytest.fixture
def unreachable_engine() -> Iterator[Engine]:
    # Port 1 on loopback: connection is refused immediately, no DNS or network wait.
    engine = create_engine("postgresql+psycopg://x:x@127.0.0.1:1/x?connect_timeout=1")
    yield engine
    engine.dispose()


def test_health_db_unavailable(client: TestClient, unreachable_engine: Engine) -> None:
    app.dependency_overrides[get_engine] = lambda: unreachable_engine

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
