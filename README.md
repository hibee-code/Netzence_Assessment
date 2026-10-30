# Carbon Credits Service

Records carbon credits earned from completed EV rides and lets drivers redeem them.
Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2.0 · Alembic.

## Configure

All configuration lives in `.env` (gitignored). Nothing is defaulted in code or in `docker-compose.yml`.

```bash
cp .env.example .env   # then fill in every value
```

| Variable            | Purpose                                                        | Typical value  |
|---------------------|----------------------------------------------------------------|----------------|
| `POSTGRES_USER`     | DB user (created on first start, used by the api)              | `credits`      |
| `POSTGRES_PASSWORD` | DB password (any characters; escaped when building the URL)    | random string  |
| `POSTGRES_DB`       | Application database                                           | `credits`      |
| `POSTGRES_TEST_DB`  | Test database, created on first start                          | `credits_test` |
| `POSTGRES_HOST`     | Host the api connects to (the compose service name)            | `db`           |
| `POSTGRES_PORT`     | Port the api connects to (Postgres's container port)           | `5432`         |
| `API_PORT`          | Host port the API is published on                              | `8000`         |

Postgres only reads `POSTGRES_USER/PASSWORD/DB/TEST_DB` when its data volume is first created.
After changing them, recreate the volume: `docker compose down -v`.

If `.env` is missing, `docker compose` refuses to start and tells you which variable is not set.

## Run

```bash
docker compose up --build
```

This starts Postgres (waits for its healthcheck), applies migrations (`alembic upgrade head`), then serves the API on
`http://localhost:$API_PORT`. Interactive docs: `/docs`.

```bash
curl http://localhost:8000/health
# {"status":"ok","database":"ok"}
```

Postgres is not published to the host; the api and the tests reach it over the compose network.
To open a SQL shell: `docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`.

## Migrations

Migrations run automatically every time the api container starts. To run them yourself
(with the stack up, use `exec`; with it down, use `run --rm api` instead):

```bash
docker compose exec api alembic current            # revision the DB is at
docker compose exec api alembic history --verbose  # all revisions
docker compose exec api alembic upgrade head       # apply everything pending
docker compose exec api alembic downgrade -1       # roll back one revision
```

To create a new revision file, mount `migrations/` so the generated file lands in your working tree:

```bash
docker compose run --rm -v "$PWD/migrations:/app/migrations" api alembic revision -m "describe change"
```

Migrations are hand-written (no ORM models to autogenerate from).

## Test

Tests run inside the api container against a real PostgreSQL database (`$POSTGRES_TEST_DB`):

```bash
docker compose run --rm api pytest -v
```

The image contains a copy of the code, so after changing code rebuild first: `docker compose build api`.
