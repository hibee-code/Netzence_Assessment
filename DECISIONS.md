# Decisions

This document explains my design choices behind the carbon credits service: the trade-offs, the data model, what's still missing, and one thing that didn't work. The code references are there to make each claim easy to check.

---

## 1. The three biggest trade-offs

### 1.1 Pessimistic per-driver locking, not optimistic concurrency or SERIALIZABLE

**What I did.**
- **Driver lock:** every transaction that spends or claws back a driver's credits starts with `SELECT … FROM drivers WHERE id = :id FOR UPDATE`.
- **Lot locks:** credit lots are then locked in `(expires_at, id)` order. That is the same order that first-in-first-out (FIFO) spending uses.
- **Ride lock:** completing and cancelling the *same ride* take a transaction-scoped advisory lock on the `ride_id` first.
- **Lock order:** all locks, and the order they're taken in, live in one file: [`app/services/locks.py`](app/services/locks.py).

**Why.**
- **Simple reasoning:** within one driver nothing interleaves. So "check the balance, then spend" is safe at Postgres's default READ COMMITTED isolation.
- **Deterministic outcomes:** in the key test, 10 parallel redemptions of 30 from a balance of 100 give exactly 3 successes. The other 7 get a clean `409 insufficient_balance` with the true remaining balance (`10.000`), not a serialization error.
- **No retry logic:** there are no retry loops to write, test or reason about.

**What it costs.**
- **Per-driver throughput:** redemptions for the same driver run one at a time. That's fine in practice: a single driver rarely redeems twice in the same instant, and different drivers never wait on each other.
- **Slow transactions:** a slow transaction holds its driver's lock. There is no `lock_timeout` yet (see §3).

**Rejected alternative: SERIALIZABLE isolation with retries on serialization failure (error 40001).** It's also correct. But every endpoint needs a retry wrapper, a conflict looks like a retry instead of a clear answer, and concurrency tests become probabilistic.

**One consequence: the lock comes before the idempotency check.** The brief says to check the Idempotency-Key first, then lock.
- **The bug:** take a redemption of the *whole* balance and a retry with the same key. The retry can pass the key check before the original commits. It then waits on the lock and sees a balance of 0. It answers `409 insufficient_balance` when it should replay the original `201`.
- **The fix:** locking first serializes the two, so the retry always sees the committed redemption.
- **Test:** `test_same_key_retry_of_a_full_balance_redemption_replays_instead_of_refusing` fails on every run under the brief's order.
- **Side effect:** the brief's "catch IntegrityError on the unique key" path can no longer be reached, so it isn't there.

### 1.2 Lazy expiry, not a scheduled job

**What I did.** Expired-but-unspent credit is written as an `EXPIRY` ledger entry inside the transaction of whatever touches that driver next: balance, history, redemption or cancellation. Balance and spending never depend on that write having happened, because they always filter on `expires_at > now` themselves. Expiry is idempotent in two ways:
- **Lock and re-check:** `FOR UPDATE` plus a re-check of `remaining_milli > 0`.
- **Database guard:** a partial unique index allows one EXPIRY per lot.

**Why.**
- **Nothing extra to run:** no worker container, no scheduler and no job-failure handling.
- **Never behind:** expiry can't lag behind a read, because it runs as part of the read.
- **Exact boundary:** at exactly `completed_at + 12 months` the credit is gone, and one microsecond earlier it isn't.

**What it costs.**
- **GET endpoints write:** they run in a transaction, and can briefly wait on a lot that a redemption has locked.
- **Inactive drivers:** expiry for a driver nobody looks at is never written. A cross-driver report such as "credits expired this month" would be incomplete.
- **Timestamp:** an EXPIRY entry's `created_at` is when it was recorded, not when the credit expired. The `reason` text carries the exact expiry instant.

### 1.3 Idempotency enforced by the database, with replayable outcomes stored

**What I did.** Each operation has a database-level identity:

| Operation | Identity | Duplicate | Conflicting duplicate |
|---|---|---|---|
| Ride completion | `rides.ride_id` primary key, inserted with `INSERT … ON CONFLICT DO NOTHING` | 200, original result | 409, with the conflicting fields listed |
| Redemption | `UNIQUE (driver_id, idempotency_key)` | 200, original result | 409 if the amount differs |
| Cancellation | `rides.cancelled_at` plus a unique REVERSAL entry per ride | 200, original outcome | n/a |

A replay must return the *original* response, so values that can't be recomputed later are stored when they happen:
- **`redemptions.balance_after_milli`:** later earnings and expiries change the balance.
- **`rides.reversed_milli` and `rides.shortfall_milli`:** later activity changes what a clawback would find.

**Why.** The safe sequence is: crash after commit, client retries, client gets the identical response. That needs no cache or Redis layer, and every endpoint stays a single all-or-nothing transaction.

**What it costs.**
- **Stored derived values:** three extra columns, added in two migrations after the initial schema.
- **Narrow conflict check:** a redemption replay only compares the amount. A retry with a different `reason` replays silently.

---

## 2. Data model

There are two representations of a driver's credits, always written together in one transaction:

- **`credit_lots`** is the spendable state: how much of each earning is left. Redemption, cancellation and expiry lock and update these rows.
- **`ledger_entries`** is the append-only history: every credit and debit with its reason. The application never updates or deletes these rows.

They are tied by one invariant: **`SUM(ledger_entries.amount_milli) = SUM(credit_lots.remaining_milli)`**. Every change to a lot's `remaining_milli` is matched by exactly one ledger entry. The invariant is checked after every cancellation test and in the concurrency tests.

| Table | Purpose | Key constraints |
|---|---|---|
| `drivers` | Created on a driver's first ride. Also the per-driver lock row. | `pk_drivers` |
| `rides` | Every completion event, including non-electric rides (0 credits), so duplicates are always detected. Also stores the cancellation outcome. | `pk_rides` (deduplication); `distance_km > 0`; `credits_milli >= 0`; `ck_rides_cancellation_outcome` (cancelled_at, reversed and shortfall are set together or not at all) |
| `credit_lots` | One per earning ride: `amount_milli`, `remaining_milli`, `expires_at`. | `uq_credit_lots_ride_id` (one lot per ride); **`remaining_milli BETWEEN 0 AND amount_milli`**, so the database itself refuses to overspend; `expires_at > earned_at`; partial index `(driver_id, expires_at, id) WHERE remaining_milli > 0` for FIFO |
| `redemptions` | One per Idempotency-Key, with the stored `balance_after_milli`. | `uq_redemptions_driver_id_idempotency_key`; amount > 0 |
| `redemption_allocations` | Which lots each redemption consumed, and how much from each. | Primary key `(redemption_id, lot_id)`; amount > 0 |
| `ledger_entries` | Append-only history: EARN, REDEEM, REVERSAL, EXPIRY. | Valid `entry_type`; **sign per type** (EARN > 0; REDEEM and EXPIRY < 0; REVERSAL ≤ 0); **required references per type** (EARN needs ride and lot; EXPIRY needs a lot; and so on); partial unique indexes allowing one EXPIRY per lot and one REVERSAL per ride |
| `ride_cancellations` | Tombstone for a cancellation that arrives before its ride. Applied, and stamped with `applied_at`, by the completion. | `pk_ride_cancellations`; deliberately no foreign key to `rides` |

Two details matter:
- **The required-references check isn't just tidiness.** Postgres never treats two NULLs as duplicates in a unique index. Without the check, an EXPIRY with a NULL `lot_id` would slip past "one EXPIRY per lot".
- **Representation:** money is `BIGINT` milli-credits everywhere, never floating point, and timestamps are `TIMESTAMPTZ`. Tests assert both across the whole schema. DB sessions are fixed to UTC, so `+ interval '12 months'` uses the UTC calendar. For example, 2024-02-29 expires on 2025-02-28.

### Rejected alternative: ledger-only (event-sourced) balances

In this design only `ledger_entries` would exist, with no mutable `remaining_milli`, and the balance would be `SUM(amount)`. It's attractive: one source of truth, nothing ever updated. I rejected it for three reasons:

1. **FIFO-by-expiry needs per-earning state.** To know which earnings a redemption draws from, and what is left to expire or claw back, you have to know how much of each earning remains. With only a ledger, that means replaying every allocation on every request. The alternative is maintaining a projection of it, which is `credit_lots` under another name.
2. **Nothing to lock.** Two concurrent redemptions would both sum the same ledger and both insert a debit. Preventing overspend would still need a lock row or SERIALIZABLE isolation.
3. **No database-level guarantee.** `CHECK (remaining_milli BETWEEN 0 AND amount_milli)` makes overspending impossible even if the application has a bug. A pure ledger can't express "this sum must never go below zero" as a constraint.

The cost I accepted is two representations to keep consistent. They're written in the same transaction, and the invariant above is tested.

---

## 3. What I would do with two more days

In priority order:

1. **Property-based tests of the core invariants.** Use Hypothesis to generate random sequences of earn, redeem, cancel, expire and clock moves, then assert after each step that no lot is negative, the ledger matches the lots, and the balance equals the sum of unexpired remaining credit. Example-based tests cover the cases I thought of; this would cover the ones I didn't.
2. **Fail fast under contention.** Set `lock_timeout` and `statement_timeout` per transaction, and add a single retry for deadlock (40P01) and serialization (40001) errors. The documented lock order should prevent deadlocks, but an unforeseen one currently surfaces as a 500.
3. **Audit trail for clawbacks.** Clawing back from *other* lots reduces their `remaining_milli`, and the REVERSAL entry records the total, but there's no per-lot record. A `reversal_allocations` table (mirroring `redemption_allocations`) would make every lot's history fully explainable.
4. **Enforce append-only in the database.** Either a trigger rejecting `UPDATE`/`DELETE` on `ledger_entries`, or an application role without those privileges. Today it's a code convention.
5. **Authentication.** The ride-event endpoints should only accept signed requests from the ride service (e.g. HMAC). The driver endpoints need a check that the caller is that driver. Neither exists.
6. **Operational polish.**
   - **Expiry sweep:** a nightly job so expiry reporting is complete for inactive drivers. Lazy expiry would stay the correctness mechanism.
   - **History paging:** keyset pagination instead of `offset`.
   - **Observability:** structured logs with request IDs, and metrics for lock-wait time on redemption.
   - **CI:** running `docker compose run --rm api pytest`.

---

## 4. One thing I tried that did not work

**Finding a race by firing threads at it.** I tried to test the race where a ride's cancellation and its completion arrive at the same moment by running both 20 times, with real threads and separate connections released together by a barrier. Then I checked whether the test could catch the bug by deleting the per-ride lock. **The test still passed on every run.**

**Why it never failed.** The bug needs a precise interleaving. The completion must check for a tombstone *after* the cancellation looked for the ride, but *before* the cancellation committed. The cancellation is only two statements, so it almost always committed before the completion reached its check. Firing threads at the problem made the bad ordering astronomically unlikely, not impossible.

**What replaced it.** `run_interleaved` in [`tests/concurrency.py`](tests/concurrency.py) forces the dangerous ordering on purpose:
1. Run transaction A and leave it **open**.
2. Start transaction B on a second connection and give it 0.5 s.
3. With the lock, B blocks until A commits. Without it, B completes and the cancellation is lost.
4. Commit A and assert the final state.

Run in both directions, these tests pass with the lock and **fail on every run** without it.

**The lesson.** A concurrency test proves nothing until you've seen it fail against a broken implementation. It had happened once before:
- **The earlier case:** the first duplicate-ride test also passed against a deliberately racy version. For a brand-new driver, the concurrent driver insert made all threads wait for the first one, which hid the race.
- **The rule since:** the key concurrency tests were each checked against a deliberately broken copy. The duplicate-ride test against a check-then-insert version, the redemption tests with the `FOR UPDATE` locks removed, the same-key test under the brief's lock order, and the cancel/complete tests with the ride lock deleted. The expiry-read and cancellation-vs-redemption concurrency tests haven't had that check yet.

---

## Appendix: other departures from the brief

| Brief | What this implementation does | Why |
|---|---|---|
| Single `docker compose up` | Needs `cp .env.example .env` first. Compose fails fast and names the missing variable. | No credentials or defaults hardcoded in code or compose (the project owner's call). |
| Check the Idempotency-Key, then lock | Lock the driver, then check the key | See §1.1 |
| (not specified) | A per-ride advisory lock in completion and cancellation | Otherwise a cancellation arriving together with its completion is lost (§4) |
| Ride with 0 credits: no ledger entry | Same; but a ride whose credits all expired or can't be recovered still gets a REVERSAL of 0 | The cancellation stays visible in history next to its EARN |
| Response shapes | Cancellation responses include `status`; a completion that applies a waiting tombstone returns `status: "cancelled"`; history is `{driver_id, entries}` with an added `offset` | Clearer clients; lets entries past the first 500 be reached |
| `created_at DEFAULT now()` | Services write every timestamp from the injected clock | Deterministic tests; one consistent time source in history |
| Business constants | 120 g/km, 12 months and 5 minutes are constants in code, not environment variables | The same ride must earn the same credits in every deployment |
