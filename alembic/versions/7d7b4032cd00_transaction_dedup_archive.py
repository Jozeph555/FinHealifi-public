"""transaction_dedup_archive

Архів транзакцій, видалених як дублікати між джерелами.

Revision ID: 7d7b4032cd00
Revises: fbef547587b9
Create Date: 2026-10-09 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7d7b4032cd00'
down_revision: Union[str, Sequence[str], None] = 'fbef547587b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "transaction_dedup_archive"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        TABLE,
        # копія рядка transactions
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("time", sa.BigInteger(), nullable=False),
        sa.Column("time_precision", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("mcc", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=True),
        sa.Column("amount_uah", sa.Integer(), nullable=True),
        sa.Column("currency_code", sa.Integer(), nullable=True),
        sa.Column("cashback_amount", sa.Integer(), nullable=True),
        sa.Column("balance", sa.BigInteger(), nullable=True),
        sa.Column("hold", sa.Boolean(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("category_method", sa.String(), nullable=True),
        sa.Column("category_confidence", sa.Float(), nullable=True),
        sa.Column("loan_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        # метадані запуску
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("match_kind", sa.String(), nullable=False),
        sa.Column("kept_id", sa.String(), nullable=False),
        sa.Column("transfers", sa.JSON(), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("run_id", "id", name="pk_transaction_dedup_archive"),
        sa.CheckConstraint("time_precision IN ('date', 'minute', 'second')",
                           name="ck_transaction_dedup_archive_time_precision"),
        sa.CheckConstraint("match_kind IN ('date', 'minute', 'second')",
                           name="ck_transaction_dedup_archive_match_kind"),
    )
    op.create_index("ix_transaction_dedup_archive_run_id", TABLE, ["run_id"])
    op.create_index("ix_transaction_dedup_archive_user_id", TABLE, ["user_id"])
    op.create_index("ix_transaction_dedup_archive_kept_id", TABLE, ["kept_id"])


def downgrade() -> None:
    """Downgrade schema. Падає (RAISE EXCEPTION), якщо в архіві є хоч один рядок."""
    op.execute(f"""
        DO $$
        DECLARE archived integer;
        BEGIN
            SELECT count(*) INTO archived FROM {TABLE};
            IF archived > 0 THEN
                RAISE EXCEPTION
                    '{TABLE}: % рядків в архіві — downgrade знищив би їх. Спершу відновіть запуски або свідомо очистіть архів.',
                    archived;
            END IF;
        END $$;
    """)
    op.drop_index("ix_transaction_dedup_archive_kept_id", table_name=TABLE)
    op.drop_index("ix_transaction_dedup_archive_user_id", table_name=TABLE)
    op.drop_index("ix_transaction_dedup_archive_run_id", table_name=TABLE)
    op.drop_table(TABLE)
