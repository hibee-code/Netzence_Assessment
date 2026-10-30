"""Configuration.

Deployment settings (credentials, hosts) come from environment variables, supplied by docker compose
from .env. Business rules are constants: they must be identical in every deployment, otherwise the
same ride would earn different credits depending on where it was processed.
"""

from datetime import timedelta
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

DRIVER = "postgresql+psycopg"

# --- Business rules -------------------------------------------------------------------
# 1 credit = 1 kg CO2 avoided, so 1 milli-credit = 1 gram.
# Assumption: an EV avoids ~120 g CO2/km compared with an average petrol car.
EV_GRAMS_CO2_AVOIDED_PER_KM = 120
EARNING_VEHICLE_TYPE = "electric"
# Postgres interval literal; expires_at = completed_at + this, computed in SQL.
CREDIT_LIFETIME = "12 months"
# Reject completion events dated further than this into the future (clock skew allowance).
MAX_COMPLETED_AT_FUTURE_SKEW = timedelta(minutes=5)

# --- API limits -----------------------------------------------------------------------
HISTORY_DEFAULT_LIMIT = 100
HISTORY_MAX_LIMIT = 500


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
