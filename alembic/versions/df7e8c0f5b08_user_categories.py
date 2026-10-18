"""user_categories

Власні категорії витрат користувача, унікальні за (user_id, lower(name)).

Revision ID: df7e8c0f5b08
Revises: 7d7b4032cd00
Create Date: 2026-10-12 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "df7e8c0f5b08"
down_revision: Union[str, None] = "7d7b4032cd00"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_categories",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_categories_user_id", "user_categories", ["user_id"])
    op.create_index(
        "uq_user_categories_user_id_lower_name",
        "user_categories",
        ["user_id", sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_user_categories_user_id_lower_name", table_name="user_categories")
    op.drop_index("ix_user_categories_user_id", table_name="user_categories")
    op.drop_table("user_categories")
