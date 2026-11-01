"""create schema

Amounts are BIGINT milli-credits (1 credit = 1000 milli). All timestamps are TIMESTAMPTZ.
Constraints are named so violations are identifiable (and assertable in tests).

Revision ID: 177858290214
Revises:
Create Date: 2026-10-30 10:35:29.742758

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '177858290214'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)
NOW = sa.text("now()")


def upgrade() -> None:
    # Upserted on a driver's first ride; also serves as the per-driver lock row.
    op.create_table(
        "drivers",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_drivers"),
    )

    # Every completed-ride event, electric or not, so duplicates are always detected.
    op.create_table(
        "rides",
        sa.Column("ride_id", sa.Text(), nullable=False),
        sa.Column("driver_id", sa.Text(), nullable=False),
        sa.Column("distance_km", sa.Numeric(10, 3), nullable=False),
        sa.Column("vehicle_type", sa.Text(), nullable=False),
        sa.Column("completed_at", TIMESTAMPTZ, nullable=False),
        sa.Column("credits_milli", sa.BigInteger(), nullable=False),
        sa.Column("cancelled_at", TIMESTAMPTZ, nullable=True),
        sa.Column("received_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("ride_id", name="pk_rides"),
        sa.ForeignKeyConstraint(["driver_id"], ["drivers.id"], name="fk_rides_driver_id_drivers"),
        sa.CheckConstraint("distance_km > 0", name="ck_rides_distance_km_positive"),
        sa.CheckConstraint("credits_milli >= 0", name="ck_rides_credits_milli_non_negative"),
    )

    # One lot per credit-earning ride. remaining_milli is what is still spendable.
    op.create_table(
        "credit_lots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("driver_id", sa.Text(), nullable=False),
        sa.Column("ride_id", sa.Text(), nullable=False),
        sa.Column("amount_milli", sa.BigInteger(), nullable=False),
        sa.Column("remaining_milli", sa.BigInteger(), nullable=False),
        sa.Column("earned_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_credit_lots"),
        sa.ForeignKeyConstraint(["driver_id"], ["drivers.id"], name="fk_credit_lots_driver_id_drivers"),
        sa.ForeignKeyConstraint(["ride_id"], ["rides.ride_id"], name="fk_credit_lots_ride_id_rides"),
        sa.UniqueConstraint("ride_id", name="uq_credit_lots_ride_id"),
        sa.CheckConstraint("amount_milli > 0", name="ck_credit_lots_amount_milli_positive"),
        sa.CheckConstraint(
            "remaining_milli BETWEEN 0 AND amount_milli",
            name="ck_credit_lots_remaining_milli_within_amount",
        ),
        sa.CheckConstraint("expires_at > earned_at", name="ck_credit_lots_expires_after_earned"),
    )
    # FIFO scan of a driver's spendable lots: ORDER BY expires_at, id.
    op.create_index(
        "ix_credit_lots_spendable",
        "credit_lots",
        ["driver_id", "expires_at", "id"],
        postgresql_where=sa.text("remaining_milli > 0"),
    )

    op.create_table(
        "redemptions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("driver_id", sa.Text(), nullable=False),
        sa.Column("amount_milli", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_redemptions"),
        sa.ForeignKeyConstraint(["driver_id"], ["drivers.id"], name="fk_redemptions_driver_id_drivers"),
        sa.UniqueConstraint(
            "driver_id", "idempotency_key", name="uq_redemptions_driver_id_idempotency_key"
        ),
        sa.CheckConstraint("amount_milli > 0", name="ck_redemptions_amount_milli_positive"),
    )

    # Which lots a redemption consumed, and how much from each.
    op.create_table(
        "redemption_allocations",
        sa.Column("redemption_id", sa.BigInteger(), nullable=False),
        sa.Column("lot_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_milli", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("redemption_id", "lot_id", name="pk_redemption_allocations"),
        sa.ForeignKeyConstraint(
            ["redemption_id"], ["redemptions.id"],
            name="fk_redemption_allocations_redemption_id_redemptions",
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["credit_lots.id"], name="fk_redemption_allocations_lot_id_credit_lots"
        ),
        sa.CheckConstraint("amount_milli > 0", name="ck_redemption_allocations_amount_milli_positive"),
    )
    # Cancellation sums how much of a given lot was already redeemed.
    op.create_index("ix_redemption_allocations_lot_id", "redemption_allocations", ["lot_id"])

    # Append-only history: application code never UPDATEs or DELETEs rows here.
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("driver_id", sa.Text(), nullable=False),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("amount_milli", sa.BigInteger(), nullable=False),  # signed: +credit / -debit
        sa.Column("ride_id", sa.Text(), nullable=True),
        sa.Column("redemption_id", sa.BigInteger(), nullable=True),
        sa.Column("lot_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_entries"),
        sa.ForeignKeyConstraint(["driver_id"], ["drivers.id"], name="fk_ledger_entries_driver_id_drivers"),
        sa.ForeignKeyConstraint(["ride_id"], ["rides.ride_id"], name="fk_ledger_entries_ride_id_rides"),
        sa.ForeignKeyConstraint(
            ["redemption_id"], ["redemptions.id"], name="fk_ledger_entries_redemption_id_redemptions"
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["credit_lots.id"], name="fk_ledger_entries_lot_id_credit_lots"),
        sa.CheckConstraint(
            "entry_type IN ('EARN', 'REDEEM', 'REVERSAL', 'EXPIRY')",
            name="ck_ledger_entries_entry_type",
        ),
        # Direction per type. REVERSAL may be 0 when the whole reversal is an unrecoverable shortfall.
        # CASE yields NULL (passes) for an unknown type, so only ck_ledger_entries_entry_type reports it.
        sa.CheckConstraint(
            """
            CASE entry_type
                WHEN 'EARN'     THEN amount_milli > 0
                WHEN 'REDEEM'   THEN amount_milli < 0
                WHEN 'EXPIRY'   THEN amount_milli < 0
                WHEN 'REVERSAL' THEN amount_milli <= 0
            END
            """,
            name="ck_ledger_entries_amount_sign",
        ),
        # Required references per type. Also load-bearing for the partial unique indexes below:
        # NULLs never collide in a unique index, so a NULL lot_id/ride_id would bypass them.
        sa.CheckConstraint(
            """
            CASE entry_type
                WHEN 'EARN'     THEN ride_id IS NOT NULL AND lot_id IS NOT NULL
                WHEN 'REDEEM'   THEN redemption_id IS NOT NULL
                WHEN 'EXPIRY'   THEN lot_id IS NOT NULL
                WHEN 'REVERSAL' THEN ride_id IS NOT NULL
            END
            """,
            name="ck_ledger_entries_required_refs",
        ),
    )
    # Idempotency guards: a lot expires at most once; a ride is reversed at most once.
    op.create_index(
        "uq_ledger_entries_expiry_lot_id",
        "ledger_entries",
        ["lot_id"],
        unique=True,
        postgresql_where=sa.text("entry_type = 'EXPIRY'"),
    )
    op.create_index(
        "uq_ledger_entries_reversal_ride_id",
        "ledger_entries",
        ["ride_id"],
        unique=True,
        postgresql_where=sa.text("entry_type = 'REVERSAL'"),
    )
    # GET /drivers/{id}/history: ORDER BY created_at, id.
    op.create_index(
        "ix_ledger_entries_driver_history", "ledger_entries", ["driver_id", "created_at", "id"]
    )

    # Tombstone for a cancellation that arrives before its ride completion.
    # No FK to rides: the ride does not exist yet when this row is written.
    op.create_table(
        "ride_cancellations",
        sa.Column("ride_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("received_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        sa.Column("applied_at", TIMESTAMPTZ, nullable=True),
        sa.PrimaryKeyConstraint("ride_id", name="pk_ride_cancellations"),
    )


def downgrade() -> None:
    op.drop_table("ride_cancellations")
    op.drop_table("ledger_entries")
    op.drop_table("redemption_allocations")
    op.drop_table("redemptions")
    op.drop_table("credit_lots")
    op.drop_table("rides")
    op.drop_table("drivers")
