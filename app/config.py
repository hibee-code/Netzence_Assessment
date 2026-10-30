"""Runtime configuration, read from environment variables (supplied by docker compose from .env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

DRIVER = "postgresql+psycopg"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True)

    postgres_user: str
    postgres_password: str
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_test_db: str | None = None

    def _url(self, database: str) -> URL:
        # URL.create escapes special characters in the password; string concatenation would not.
        return URL.create(
            DRIVER,
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=database,
        )

    @property
    def database_url(self) -> URL:
        return self._url(self.postgres_db)

    @property
    def test_database_url(self) -> URL | None:
        return self._url(self.postgres_test_db) if self.postgres_test_db else None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # fields come from env
