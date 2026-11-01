"""store redemption balance_after

An idempotent replay must return the ORIGINAL response, including the balance right after the
redemption. That value can't be recomputed later (later earns/expiries change the balance), so it
is stored. NOT NULL without a default is safe: no redemptions exist before this revision (the
redeem endpoint ships with it).

Revision ID: 358f78646ca7
Revises: 177858290214
Create Date: 2026-10-30 11:24:13.307542

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '358f78646ca7'
down_revision: str | None = '177858290214'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("redemptions", sa.Column("balance_after_milli", sa.BigInteger(), nullable=False))
    op.create_check_constraint(
        "ck_redemptions_balance_after_milli_non_negative", "redemptions", "balance_after_milli >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_redemptions_balance_after_milli_non_negative", "redemptions", type_="check")
    op.drop_column("redemptions", "balance_after_milli")
