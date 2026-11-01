"""store ride cancellation outcome

A repeated cancellation must replay the ORIGINAL outcome (how much was reversed, and the
unrecoverable shortfall). Stored on the ride, alongside the existing cancelled_at; all three are
set together, in the transaction that applies the cancellation.

Revision ID: bcd7842698b5
Revises: 358f78646ca7
Create Date: 2026-10-30 15:43:31.847101

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'bcd7842698b5'
down_revision: str | None = '358f78646ca7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rides", sa.Column("reversed_milli", sa.BigInteger(), nullable=True))
    op.add_column("rides", sa.Column("shortfall_milli", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_rides_cancellation_outcome",
        "rides",
        """
        (cancelled_at IS NULL AND reversed_milli IS NULL AND shortfall_milli IS NULL)
        OR (cancelled_at IS NOT NULL AND reversed_milli >= 0 AND shortfall_milli >= 0)
        """,
    )


def downgrade() -> None:
    op.drop_constraint("ck_rides_cancellation_outcome", "rides", type_="check")
    op.drop_column("rides", "shortfall_milli")
    op.drop_column("rides", "reversed_milli")
