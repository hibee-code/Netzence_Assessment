"""Database engine. Exposed as a FastAPI dependency so tests can swap it out."""

from functools import lru_cache
from typing import Any

from sqlalchemy import URL, Engine, create_engine

from app.config import get_settings


def create_db_engine(url: URL, **kwargs: Any) -> Engine:
    # Pin the session time zone: `timestamptz + interval '12 months'` is calendar arithmetic in the
    # session zone, so expiry must not depend on the server's configured TimeZone.
    return create_engine(url, connect_args={"options": "-c timezone=UTC"}, **kwargs)


@lru_cache
def get_engine() -> Engine:
    return create_db_engine(get_settings().database_url, pool_pre_ping=True)
