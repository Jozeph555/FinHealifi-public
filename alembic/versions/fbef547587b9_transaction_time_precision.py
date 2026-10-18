"""transaction time_precision

Точність transactions.time на рівні рядка ('date' | 'minute' | 'second') з заповненням наявних рядків.

Revision ID: fbef547587b9
Revises: fe0284f22cb8
Create Date: 2026-10-09 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fbef547587b9'
down_revision: Union[str, Sequence[str], None] = 'fe0284f22cb8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT_NAME = "ck_transactions_time_precision"


def upgrade() -> None:
    """Upgrade schema.

    Заповнення за правилом dedup.infer_account_precision у SQL: monobank_api ->
    'second', manual -> 'date', csv_import — за всіма рядками рахунку (час за
    Києвом). Рядок без значення зупиняє міграцію (RAISE EXCEPTION).
    """
    # 1. Колонка спершу nullable — щоб заповнити наявні рядки.
    op.add_column("transactions", sa.Column("time_precision", sa.String(), nullable=True))

    # 2. Backfill.
    op.execute("""
        UPDATE transactions t SET time_precision = 'second'
        FROM accounts a
        WHERE a.id = t.account_id AND a.source_type = 'monobank_api'
    """)
    op.execute("""
        UPDATE transactions t SET time_precision = 'date'
        FROM accounts a
        WHERE a.id = t.account_id AND a.source_type = 'manual'
    """)
    op.execute("""
        WITH acc AS (
            SELECT t.account_id,
                   bool_and(
                       (to_timestamp(t.time) AT TIME ZONE 'Europe/Kyiv')::time = time '00:00:00'
                   ) AS all_midnight,
                   bool_and(
                       extract(second FROM (to_timestamp(t.time) AT TIME ZONE 'Europe/Kyiv')) = 0
                   ) AS all_zero_seconds
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            WHERE a.source_type = 'csv_import'
            GROUP BY t.account_id
        )
        UPDATE transactions t
        SET time_precision = CASE
            WHEN acc.all_midnight THEN 'date'
            WHEN acc.all_zero_seconds THEN 'minute'
            ELSE 'second'
        END
        FROM acc
        WHERE acc.account_id = t.account_id
    """)

    # 3. Гучна перевірка: жодного рядка без точності.
    op.execute("""
        DO $$
        DECLARE missing integer;
        BEGIN
            SELECT count(*) INTO missing FROM transactions WHERE time_precision IS NULL;
            IF missing > 0 THEN
                RAISE EXCEPTION
                    'time_precision backfill: % рядків без значення (невідомий source_type?)',
                    missing;
            END IF;
        END $$;
    """)

    # 4. NOT NULL + CHECK.
    op.alter_column("transactions", "time_precision", nullable=False)
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "transactions",
        "time_precision IN ('date', 'minute', 'second')",
    )


def downgrade() -> None:
    """Downgrade schema.

    Повторний upgrade відновлює ті самі значення лише для рахунків з однією
    точністю на всі рядки.
    """
    op.drop_constraint(CONSTRAINT_NAME, "transactions", type_="check")
    op.drop_column("transactions", "time_precision")
