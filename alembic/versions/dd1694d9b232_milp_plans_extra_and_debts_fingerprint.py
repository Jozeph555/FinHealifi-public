"""milp_plans: extra_monthly_uah and debts_fingerprint

Додатковий платіж і відбиток боргів збереженого плану (nullable).

Revision ID: dd1694d9b232
Revises: df7e8c0f5b08
Create Date: 2026-10-13 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "dd1694d9b232"
down_revision: Union[str, None] = "df7e8c0f5b08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("milp_plans", sa.Column("extra_monthly_uah", sa.Float(), nullable=True))
    op.add_column("milp_plans", sa.Column("debts_fingerprint", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("milp_plans", "debts_fingerprint")
    op.drop_column("milp_plans", "extra_monthly_uah")
