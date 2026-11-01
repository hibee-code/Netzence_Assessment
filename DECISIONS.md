# Decisions

These are the design choices behind the carbon-credits service. The code references let you check each claim.

## 1. The three biggest trade-offs

### Per-driver locking, not optimistic concurrency

Any operation that spends or claws back a driver's credits takes locks in this order:
1. **Driver row:** `SELECT … FOR UPDATE` on the driver.
2. **Credit lots:** that driver's credit lots, in expiry order.
3. **Ride lock:** completion and cancellation of the same ride also take a per-ride advisory lock.

All locks, and the order they're taken in, are in [`app/services/locks.py`](app/services/locks.py).

- **Why:** within one driver nothing interleaves. So "check the balance, then spend" is safe at READ COMMITTED, and results are deterministic. Ten parallel redemptions of 30 from a balance of 100 give exactly 3 successes and 7 clean `409 insufficient_balance` responses, with no retry loops.
- **Cost:**
  - **Per-driver serialization:** a driver's redemptions run one at a time.
  - **Slow transactions:** a slow transaction holds that driver's lock. There's no `lock_timeout` yet.
- **Rejected:** SERIALIZABLE isolation with retries. It's also correct, but every endpoint needs a retry wrapper and the concurrency tests become probabilistic.
- **Departure from the brief: the lock comes before the Idempotency-Key check.** In the brief's order, a retry of a whole-balance redemption can pass the key check before the original commits. It then waits for the lock, finds a balance of 0, and returns 409 instead of replaying the original 201. A test reproduces this on every run under the brief's order.

### Lazy expiry, not a scheduled job

Expired, unspent credit is recorded as an `EXPIRY` ledger entry the next time the driver's balance, history, a redemption or a cancellation runs, inside that same transaction. Balance and spending never depend on it, because they always filter on `expires_at > now` themselves.

- **Why:** there's nothing extra to deploy or monitor, and expiry can never lag behind a read. A partial unique index allows only one EXPIRY entry per lot.
- **Cost:** GET endpoints write to the database. Expiry for inactive drivers is never recorded, so a report across all drivers would be incomplete.

### Idempotency in the database, with original outcomes stored

Every write has a database-enforced identity:

| Operation | Identity |
|---|---|
| Ride completion | The ride's primary key, written with `INSERT … ON CONFLICT DO NOTHING` |
| Redemption | `UNIQUE (driver_id, idempotency_key)` |
| Cancellation | One REVERSAL entry per ride |

A duplicate gets the original response (200). A conflicting duplicate gets 409.

- **Why:** if the server crashes after committing, the client simply retries and gets the identical response. No cache or extra infrastructure is needed.
- **Cost:** some values can't be recomputed later, so they're stored: `redemptions.balance_after_milli`, `rides.reversed_milli` and `rides.shortfall_milli`. A redemption replay compares only the amount, not the reason.

## 2. Data model

A driver's credits are held in two forms, always written in the same transaction:

- **`credit_lots`:** the spendable state, meaning how much of each ride's credit is left. This is what gets locked and spent.
- **`ledger_entries`:** the append-only history of every credit and debit, with its reason.

They're tied by one rule: `SUM(ledger_entries.amount_milli) = SUM(credit_lots.remaining_milli)`. Tests check it after every cancellation test and in the concurrency tests.

| Table | Purpose | Key constraints |
|---|---|---|
| `drivers` | One per driver; also the lock row | Primary key |
| `rides` | Every completion event, including 0-credit rides so duplicates are caught; plus the cancellation outcome | Primary key on `ride_id`; `distance_km > 0` |
| `credit_lots` | One per earning ride | One lot per ride; **`remaining_milli BETWEEN 0 AND amount_milli`**, so the database itself refuses to overspend; partial index for oldest-first spending |
| `redemptions` | One per idempotency key | `UNIQUE (driver_id, idempotency_key)` |
| `redemption_allocations` | Which lots paid for each redemption | Primary key `(redemption_id, lot_id)` |
| `ledger_entries` | Append-only history | Valid type; sign per type; required references per type; one EXPIRY per lot; one REVERSAL per ride |
| `ride_cancellations` | A cancellation that arrived before its ride | Primary key; no foreign key to `rides`, by design |

- **Representation:** amounts are `BIGINT` thousandths of a credit, never floating point. Timestamps are `TIMESTAMPTZ`, and database sessions are fixed to UTC, so 12 months after 2024-02-29 is 2025-02-28.
- **The required-references check matters.** Postgres treats NULLs as distinct in unique indexes. Without the check, an EXPIRY entry with a NULL `lot_id` would bypass "one EXPIRY per lot".

### Rejected alternative: ledger-only balances

Keep only `ledger_entries` and compute the balance as `SUM(amount)`. Rejected for three reasons:

1. **Spending order needs per-ride state.** Spending the oldest credits first requires knowing how much of each ride's credit remains. Deriving that from the ledger on every request means rebuilding `credit_lots` anyway.
2. **Nothing to lock.** Two concurrent redemptions would both see the same sum and both spend.
3. **No database guarantee.** A CHECK on `remaining_milli` makes overspending impossible even if the code has a bug. A ledger can't express "this sum never goes below zero" as a constraint.

## 3. With two more days

1. **Property-based tests (Hypothesis):** random sequences of earn, redeem, cancel and expire. After each step, assert that no lot is negative, the ledger matches the lots, and the balance equals the unexpired remaining credit.
2. **Fail fast under contention:** add `lock_timeout` and `statement_timeout`, plus one retry on deadlock. An unforeseen deadlock currently returns a 500.
3. **Reconciliation check and nightly expiry job:** check the ledger = lots rule in production, not only in tests, and record expiry for inactive drivers.
4. **Debt tracking for shortfalls:** recover credit that couldn't be clawed back from the driver's future earnings, instead of writing it off. The balance would still never show as negative.
5. **Clawback audit trail:** a per-lot record of what a clawback took from other lots, like `redemption_allocations` does for redemptions.
6. **Append-only enforced by the database:** a trigger, or a database role without UPDATE and DELETE on `ledger_entries`.
7. **Authentication, observability and CI:**
   - **Authentication:** signed ride events, and driver authentication.
   - **Observability:** structured logs and metrics.
   - **CI:** the test suite running automatically.

## 4. One thing that didn't work

**What I tried.** To test a cancellation racing its own completion, I ran both 20 times on real threads released at the same moment. With the per-ride lock deleted, **the test still passed every time**.

**Why it never failed.** The cancellation is only two statements, so it almost always committed before the completion checked for it. The ordering that loses the cancellation practically never happened.

**What replaced it.** `run_interleaved` in [`tests/concurrency.py`](tests/concurrency.py) forces the ordering:
1. Open transaction A.
2. Start transaction B on another connection and give it 0.5 seconds.
3. Commit A.

In both directions, these tests pass with the lock and fail on every run without it.

**Lesson:** a concurrency test proves nothing until you've seen it fail against a broken implementation. The key concurrency tests were each checked that way: with the database locks removed, the ride lock deleted, or the brief's lock order restored.

## Departures from the brief

- **`.env` is required.** Run `cp .env.example .env` before `docker compose up`, because the repository contains no credentials or defaults.
- **The lock comes before the Idempotency-Key check.** See §1.
- **A per-ride advisory lock**, which the brief doesn't mention. See §4.
