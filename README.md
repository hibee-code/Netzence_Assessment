# Carbon Credits Service

A ride-hailing platform awards drivers carbon credits for completed electric-vehicle rides. This service records those credits and lets drivers redeem them. It guarantees the following:

- **Each ride is credited once.** This holds even when the same completion event arrives twice, including at the same moment.
- **No balance ever goes below zero.** This holds even under concurrent redemptions.
- **Credits expire 12 months after the ride, and the oldest are spent first.**
- **Cancellations never rewrite history.** A cancelled ride's credits are reversed with a new ledger entry, and the original entry stays.

Built with Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy 2.0 and Alembic. Everything runs in Docker.

**Contents:** [Quick start](#quick-start) · [Configuration](#configuration) · [How credits work](#how-credits-work) · [API reference](#api-reference) · [Walkthrough](#walkthrough) · [Running the tests](#running-the-tests) · [Database migrations](#database-migrations) · [Project layout](#project-layout) · [Design decisions](#design-decisions) · [Troubleshooting](#troubleshooting)

---

## Quick start

**Prerequisite:** Docker with Docker Compose v2.24 or later. You don't need Python or PostgreSQL on your machine.

1. **Create your configuration.** The service ships with no default credentials, so create `.env` from the template and fill in every value:

   ```bash
   cp .env.example .env
   ```

   For local use, these values work. Choose your own password:

   ```dotenv
   POSTGRES_USER=credits
   POSTGRES_PASSWORD=change-me
   POSTGRES_DB=credits
   POSTGRES_TEST_DB=credits_test
   POSTGRES_HOST=db
   POSTGRES_PORT=5432
   API_PORT=8000
   ```

2. **Start the service.**

   ```bash
   docker compose up --build
   ```

   Compose starts PostgreSQL and waits for it to report healthy. Then it applies the database migrations and starts the API.

3. **Check that it's running.**

   ```bash
   curl http://localhost:8000/health
   # {"status":"ok","database":"ok"}
   ```

   Interactive API documentation is at <http://localhost:8000/docs>.

To stop, press `Ctrl+C`, or run `docker compose down`. Your data is kept. To also delete the database, run `docker compose down -v`.

---

## Configuration

All configuration comes from `.env`, which is gitignored and never copied into the image. Neither the code nor `docker-compose.yml` has defaults. If `.env` is missing or a value is empty, Compose refuses to start and names the missing variable.

| Variable | Purpose | Example |
|---|---|---|
| `POSTGRES_USER` | Database user. Created on first start and used by the API. | `credits` |
| `POSTGRES_PASSWORD` | Database password. Any characters are allowed; they are escaped when the connection URL is built. | a random string |
| `POSTGRES_DB` | Application database. | `credits` |
| `POSTGRES_TEST_DB` | Test database. Created on first start. | `credits_test` |
| `POSTGRES_HOST` | Host the API connects to: the Compose service name. | `db` |
| `POSTGRES_PORT` | Port the API connects to: PostgreSQL's port inside the container. | `5432` |
| `API_PORT` | Port the API is published on, on your machine. | `8000` |

> **Note:** PostgreSQL reads `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` and `POSTGRES_TEST_DB` only when its data volume is first created. If you change them later, recreate the volume with `docker compose down -v`. This deletes all data.

Business rules are constants in [`app/config.py`](app/config.py), not environment variables, so the same ride earns the same credits in every deployment:

| Constant | Value |
|---|---|
| `EV_GRAMS_CO2_AVOIDED_PER_KM` | `120` |
| `CREDIT_LIFETIME` | `12 months` |
| `MAX_COMPLETED_AT_FUTURE_SKEW` | `5 minutes` |

---

## How credits work

### Earning

One credit represents 1 kg of CO2 avoided. Credits are calculated as:

```
credits = distance_km × 120 g/km ÷ 1000, rounded down to 0.001 credit
```

- **The 120 g/km rate is an assumption.** It's the CO2 an electric vehicle avoids per kilometre compared with an average petrol car.
- **Rounding:** happens once, when the ride is recorded, and always downwards, so the service never issues more credit than was earned.
- **Example:** a 12.3 km electric ride earns `1.476` credits.
- **Electric only:** only rides with `vehicle_type` `electric` earn credits. The check ignores case and surrounding spaces.
- **Other vehicles:** rides are still recorded, with 0 credits, so a duplicate event for them is still detected.

### Expiry

- **When:** credits expire 12 months after the ride's `completed_at`, using UTC calendar months. A ride completed on 2024-02-29 expires on 2025-02-28.
- **The boundary:** credits are spendable until that instant and expired *at* it.
- **Recording:** the service writes the expiry to the driver's history automatically, the next time that driver's balance, history or a redemption is requested. Expired credits never count towards a balance, even before that entry is written.

### Spending

- **Oldest first:** a redemption spends the credits closest to expiry first (first-in, first-out by expiry date).
- **One redemption at a time:** redemptions for the same driver are processed one after another, so concurrent requests can never overspend.
- **All or nothing:** a redemption either succeeds in full or is refused.

### Cancellation

When a completed ride is cancelled, its credits are taken back:

1. **Unspent credits** still left from that ride are reversed.
2. **Spent credits** that were already redeemed are clawed back from the driver's other credits, oldest first.
3. **Expired credits** that expired unspent are not clawed back.
4. **Shortfall:** if the driver doesn't have enough credits left, the balance stops at zero and never goes negative. The amount that couldn't be recovered is reported as a `shortfall`.

The original credit entry is never changed; a reversal entry is added beside it. If a cancellation arrives before its ride completion, it's stored and applied the moment the completion arrives.

### Amounts

- **Precision:** credits are exact to 0.001. The database stores whole numbers of thousandths, never floating-point numbers.
- **Responses:** amounts are returned as strings with exactly three decimal places, e.g. `"12.345"`.
- **Requests:** amounts and distances can be sent as numbers or strings. Values with more than three decimal places are rejected.

---

## API reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | [`/rides/completed`](#post-ridescompleted) | Record a completed ride and award its credits |
| `POST` | [`/rides/cancelled`](#post-ridescancelled) | Reverse the credits of a cancelled ride |
| `GET` | [`/drivers/{driver_id}/balance`](#get-driversdriver_idbalance) | A driver's spendable balance |
| `POST` | [`/drivers/{driver_id}/redeem`](#post-driversdriver_idredeem) | Spend credits |
| `GET` | [`/drivers/{driver_id}/history`](#get-driversdriver_idhistory) | Every credit and debit, with its reason |
| `GET` | [`/health`](#get-health) | Service and database status |

**Conventions**

- **Bodies:** requests and responses are JSON.
- **Timestamps:** ISO 8601 with a timezone offset. Responses use UTC, e.g. `2027-10-30T09:00:00Z`.
- **Errors:** `{"detail": "<code>", ...}`. Validation errors (`422`) use FastAPI's standard format: `{"detail": [{"type", "loc", "msg"}]}`.
- **Retries:** every write is idempotent, so retrying a request after a timeout is always safe.

### POST /rides/completed

Records a completed ride. Sending the same event again is safe.

| Field | Type | Rules |
|---|---|---|
| `ride_id` | string | Required, 1–255 characters. Identifies the ride. |
| `driver_id` | string | Required, 1–255 characters. |
| `distance_km` | number or string | Greater than 0, at most 3 decimal places, less than 10,000,000. |
| `vehicle_type` | string | Required. Only `electric` earns credits; case doesn't matter. |
| `completed_at` | datetime | Must include a timezone offset. No more than 5 minutes in the future. |

| Status | Meaning | Body |
|---|---|---|
| `201` | Ride recorded. | `{ride_id, driver_id, credits, expires_at, status}`. `status` is `recorded`, or `cancelled` if a cancellation for this ride arrived first. `expires_at` is `null` when the ride earned nothing. |
| `200` | Duplicate of a recorded ride. | The original result, with `status: "duplicate"`. |
| `409` | `ride_id` already recorded with different details. | `{"detail": "ride_conflict", "ride_id", "conflicting_fields": [...]}` |
| `422` | Invalid request. | Validation errors. |

"Different details" means a different `driver_id`, `distance_km`, `vehicle_type` or `completed_at`. Equivalent forms count as identical: `12.3` and `"12.300"`, `Electric` and `electric`, or the same instant written in a different timezone offset.

### POST /rides/cancelled

Reverses the credits of a cancelled or refunded ride. Sending the same cancellation again is safe.

| Field | Type | Rules |
|---|---|---|
| `ride_id` | string | Required, 1–255 characters. |
| `reason` | string | Optional, up to 500 characters. Recorded in the driver's history. |

| Status | Meaning | Body |
|---|---|---|
| `200` | Ride cancelled. A repeated cancellation returns the original outcome. | `{ride_id, status: "cancelled", reversed, shortfall}` |
| `202` | The ride hasn't been recorded yet. The cancellation is stored and applied when the ride arrives. | `{ride_id, status: "pending_completion", reversed: null, shortfall: null}` |
| `422` | Invalid request. | Validation errors. |

`reversed` is the amount taken back. `shortfall` is the amount the driver had already spent and that couldn't be recovered.

### GET /drivers/{driver_id}/balance

Returns the driver's spendable (unexpired) credits. An unknown driver has a balance of `"0.000"`.

| Status | Body |
|---|---|
| `200` | `{driver_id, balance}` |

### POST /drivers/{driver_id}/redeem

Spends credits, oldest-expiring first.

**Header:** `Idempotency-Key` (required). Use a unique value per redemption, such as an order ID. Retrying with the same key returns the original redemption instead of charging again. Keys are scoped per driver.

| Field | Type | Rules |
|---|---|---|
| `amount` | number or string | Greater than 0, at most 3 decimal places. |
| `reason` | string | Optional, up to 500 characters. Recorded in the driver's history. |

| Status | Meaning | Body |
|---|---|---|
| `201` | Credits redeemed. | `{redemption_id, amount, balance_after, allocations: [{lot_id, ride_id, amount}]}` |
| `200` | Same key and amount as an earlier redemption. | The original redemption, unchanged. |
| `400` | `Idempotency-Key` header missing or empty. | `{"detail": "idempotency_key_required"}` |
| `409` | Not enough credits. | `{"detail": "insufficient_balance", "available": "<balance>"}` |
| `409` | The key was already used for a different amount. | `{"detail": "idempotency_key_reused_with_different_amount"}` |
| `422` | Invalid amount. | Validation errors. |

`allocations` shows which rides' credits paid for the redemption.

### GET /drivers/{driver_id}/history

Returns every credit and debit for the driver, oldest first.

| Query parameter | Default | Rules |
|---|---|---|
| `limit` | `100` | 1–500 |
| `offset` | `0` | 0 or more |

| Status | Body |
|---|---|
| `200` | `{driver_id, entries: [{id, type, amount, ride_id, redemption_id, reason, created_at}]}` |
| `422` | `limit` or `offset` out of range. |

| `type` | `amount` | Written when |
|---|---|---|
| `EARN` | positive | An electric ride is recorded. |
| `REDEEM` | negative | Credits are redeemed. |
| `REVERSAL` | negative or zero | A ride is cancelled. Zero when nothing could be reversed. |
| `EXPIRY` | negative | Unspent credits expire. |

### GET /health

| Status | Body |
|---|---|
| `200` | `{"status": "ok", "database": "ok"}` |
| `503` | `{"status": "error", "database": "unavailable"}` |

---

## Walkthrough

These requests run against a freshly started service. The responses are real output.

**1. Two electric rides are completed.** The first event is delivered twice, then once more with a different distance.

```bash
curl -X POST localhost:8000/rides/completed -H 'Content-Type: application/json' \
  -d '{"ride_id":"ride-101","driver_id":"driver-7","distance_km":100,"vehicle_type":"electric","completed_at":"2026-10-30T09:00:00Z"}'
```
```json
201 {"ride_id":"ride-101","driver_id":"driver-7","credits":"12.000","expires_at":"2027-10-30T09:00:00Z","status":"recorded"}
```

Sending the same request again records nothing new:
```json
200 {"ride_id":"ride-101","driver_id":"driver-7","credits":"12.000","expires_at":"2027-10-30T09:00:00Z","status":"duplicate"}
```

The same `ride_id` with `"distance_km":90` is rejected:
```json
409 {"detail":"ride_conflict","ride_id":"ride-101","conflicting_fields":["distance_km"]}
```

`ride-102` (50 km, completed an hour later) earns `6.000` credits, so the balance is now:
```bash
curl localhost:8000/drivers/driver-7/balance
```
```json
200 {"driver_id":"driver-7","balance":"18.000"}
```

**2. The driver redeems 5 credits.** The credits come from `ride-101`, which expires first.

```bash
curl -X POST localhost:8000/drivers/driver-7/redeem -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-555' -d '{"amount":"5","reason":"coffee voucher"}'
```
```json
201 {"redemption_id":1,"amount":"5.000","balance_after":"13.000","allocations":[{"lot_id":1,"ride_id":"ride-101","amount":"5.000"}]}
```

Retrying with the same `Idempotency-Key` returns the same redemption. The driver isn't charged twice:
```json
200 {"redemption_id":1,"amount":"5.000","balance_after":"13.000","allocations":[{"lot_id":1,"ride_id":"ride-101","amount":"5.000"}]}
```

Asking for more than the balance is refused:
```json
409 {"detail":"insufficient_balance","available":"13.000"}
```

**3. `ride-101` is refunded.** Its 7 unspent credits are reversed. The 5 the driver already spent are clawed back from `ride-102`.

```bash
curl -X POST localhost:8000/rides/cancelled -H 'Content-Type: application/json' \
  -d '{"ride_id":"ride-101","reason":"refund"}'
```
```json
200 {"ride_id":"ride-101","status":"cancelled","reversed":"12.000","shortfall":"0.000"}
```
```json
200 {"driver_id":"driver-7","balance":"1.000"}
```

**4. The history shows every step.** The original EARN entry is still there, followed by its REVERSAL. Timestamps are abbreviated.

```bash
curl localhost:8000/drivers/driver-7/history
```
```json
{
  "driver_id": "driver-7",
  "entries": [
    {"id": 1, "type": "EARN",     "amount": "12.000",  "ride_id": "ride-101", "redemption_id": null, "reason": "EV ride ride-101: 100 km", "created_at": "2026-10-30T16:29:25Z"},
    {"id": 2, "type": "EARN",     "amount": "6.000",   "ride_id": "ride-102", "redemption_id": null, "reason": "EV ride ride-102: 50 km", "created_at": "2026-10-30T16:29:26Z"},
    {"id": 3, "type": "REDEEM",   "amount": "-5.000",  "ride_id": null,       "redemption_id": 1,    "reason": "Redeemed 5.000 credits: coffee voucher", "created_at": "2026-10-30T16:29:26Z"},
    {"id": 4, "type": "REVERSAL", "amount": "-12.000", "ride_id": "ride-101", "redemption_id": null, "reason": "Ride ride-101 cancelled (refund); reversed 12.000 of 12.000", "created_at": "2026-10-30T16:29:26Z"}
  ]
}
```

**5. A cancellation arrives before its ride.** It's stored until the ride completes. The completion then returns `status: "cancelled"`, and the history shows an EARN and a REVERSAL that cancel out.

```bash
curl -X POST localhost:8000/rides/cancelled -H 'Content-Type: application/json' -d '{"ride_id":"ride-999"}'
```
```json
202 {"ride_id":"ride-999","status":"pending_completion","reversed":null,"shortfall":null}
```

---

## Running the tests

The tests run inside the API container against a real PostgreSQL database: `POSTGRES_TEST_DB`, which is separate from the application database. Compose starts the database automatically if it isn't running.

```bash
docker compose run --rm api pytest -v
```

The image contains a copy of the code. After changing code, rebuild before testing:

```bash
docker compose build api
```

The session migrates the test database once. Before each test, every table is emptied (`TRUNCATE … RESTART IDENTITY CASCADE`). Time is controlled by a fake clock, so expiry tests are exact.

| Area | Test files | What's covered |
|---|---|---|
| Amounts and credit formula | `test_money.py`, `test_credits.py` | Exact conversion, rounding down, floats rejected |
| Ride completion | `test_rides.py` | Duplicates (200), conflicts (409), validation (422) |
| Redemption | `test_redeem.py` | Oldest-first spending, insufficient balance, idempotency, missing key |
| Expiry | `test_expiry.py` | The exact 12-month instant, the leap day, expiry recorded once |
| Cancellation | `test_cancellation.py` | Full reversal, clawback, shortfall, cancellation before completion |
| History | `test_history.py` | Every entry type, ordering, paging |
| Concurrency | `test_concurrency.py` | See below |
| Schema and infrastructure | `test_schema.py`, `test_migrations.py`, `test_isolation.py`, `test_clock.py`, `test_health.py`, `test_lots.py` | Each database constraint, migration round trip, test isolation |

**Concurrency tests use real parallelism.**
- **Setup:** each worker thread has its own database connection and transaction, and a barrier releases them all at the same moment. FastAPI's test client isn't used here, because it can serialize requests and hide races.
- **What they cover:**
  - 20 identical ride events at once record exactly one credit.
  - 10 redemptions of 30 from a balance of 100 give exactly 3 successes and a final balance of 10.
  - Retries with the same key charge once.
  - A cancellation and its completion are forced into both possible orders.
- **Proof they catch races:** each of these tests was confirmed to fail against a deliberately broken version of the code, for example with the database locks removed.

---

## Database migrations

The schema is managed by hand-written Alembic migrations in [`migrations/versions/`](migrations/versions). Migrations run automatically every time the API container starts. To run them yourself while the stack is up:

```bash
docker compose exec api alembic current        # revision the database is at
docker compose exec api alembic history        # all revisions
docker compose exec api alembic upgrade head   # apply pending migrations
docker compose exec api alembic downgrade -1   # roll back one revision
```

To create a new migration, mount `migrations/` so the file is written to your working copy:

```bash
docker compose run --rm -v "$PWD/migrations:/app/migrations" api alembic revision -m "describe the change"
```

---

## Project layout

```
app/
├── main.py              Application: routers and error handlers
├── config.py            Settings from the environment; business-rule constants
├── db.py                Database engine (sessions pinned to UTC)
├── clock.py             Injectable clock
├── money.py             Credit amount conversion and formatting
├── credits.py           Credit calculator
├── schemas.py           Request and response models
├── api/                 HTTP layer: rides, drivers, health, error mapping
└── services/            Business logic: explicit SQL, one transaction per request
    ├── rides.py             Ride completion
    ├── cancellations.py     Reversal and clawback
    ├── redemptions.py       Redemption
    ├── balances.py          Balance and history
    ├── expiry.py            Expiry
    ├── lots.py              Shared oldest-first spending
    ├── locks.py             Every lock, and the order they're taken in
    └── drivers.py           Driver registration
migrations/              Alembic migrations
docker/postgres/init/    Creates the test database on first start
tests/                   Test suite (real PostgreSQL)
```

---

## Design decisions

[DECISIONS.md](DECISIONS.md) explains the main trade-offs, the data model, and what's still missing. In brief:

- **Locking:** each driver's account is locked for the duration of any operation that spends or claws back credits. Correctness doesn't depend on retries or timing.
- **Two records of credit:** the spendable credit from each ride and an append-only history are always written together. A tested rule keeps them in agreement.
- **Exact replays:** every write has a database-enforced identity, so a retried request returns exactly the original response.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `required variable API_PORT is missing a value` | `.env` is missing or incomplete. Run `cp .env.example .env` and fill in every value. |
| `port is already allocated` | Another process is using `API_PORT`. Choose a different port in `.env`. |
| `password authentication failed` after editing `.env` | PostgreSQL kept the credentials from its first start. Run `docker compose down -v`; this deletes all data. |
| Tests don't reflect your code changes | The image contains a copy of the code. Run `docker compose build api` first. |
| You need a SQL shell | PostgreSQL isn't exposed on your machine. Run `docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`. |
