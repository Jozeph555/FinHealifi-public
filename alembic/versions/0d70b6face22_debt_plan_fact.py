"""debt plan fact: versions, schedule, payments, balance checks, month marks

Таблиці план-факту боргів; milp_plans стають чернетками; звірка 'migration' для кожного наявного боргу.

Revision ID: 0d70b6face22
Revises: dd1694d9b232
Create Date: 2026-11-04 18:06:47.585929

"""
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0d70b6face22"
down_revision: Union[str, Sequence[str], None] = "dd1694d9b232"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uah_to_kop(value) -> int:
    """Гривні з Float у копійки: Decimal(str(x)), half-up."""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def upgrade() -> None:
    """Створює таблиці план-факту, переводить плани в чернетки, додає звірки наявних боргів."""
    op.create_table(
        "debt_plan_months",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("no_payments_confirmed", sa.Boolean(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "month"),
    )
    op.create_table(
        "debt_balance_checks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("debt_id", sa.String(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("balance_kop", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=True),
        sa.CheckConstraint("source IN ('statement', 'manual_edit', 'debt_created', 'migration', 'plan_accepted')",
                           name="ck_debt_balance_checks_source"),
        sa.CheckConstraint("balance_kop >= 0", name="ck_debt_balance_checks_balance_nonneg"),
        sa.ForeignKeyConstraint(["debt_id"], ["debts.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debt_balance_checks_debt_as_of", "debt_balance_checks", ["debt_id", "as_of"])
    op.create_index("ix_debt_balance_checks_user_id", "debt_balance_checks", ["user_id"])
    op.create_index("uq_debt_balance_checks_debt_as_of", "debt_balance_checks", ["debt_id", "as_of"], unique=True,
                    postgresql_where=sa.text("source <> 'plan_accepted'"))
    op.create_table(
        "debt_payments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("debt_id", sa.String(), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("amount_kop", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=True),
        sa.CheckConstraint("amount_kop > 0", name="ck_debt_payments_amount_positive"),
        sa.ForeignKeyConstraint(["debt_id"], ["debts.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debt_payments_debt_paid_on", "debt_payments", ["debt_id", "paid_on"])
    op.create_index("ix_debt_payments_user_paid_on", "debt_payments", ["user_id", "paid_on"])
    op.create_table(
        "debt_plan_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("source_plan_id", sa.String(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"),
                  nullable=False),
        sa.Column("accepted_on", sa.Date(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("start_kind", sa.String(), nullable=False),
        sa.Column("extra_monthly_kop", sa.BigInteger(), nullable=False),
        sa.Column("strategy", sa.String(), nullable=True),
        sa.Column("solver_status", sa.String(), nullable=True),
        sa.Column("months_to_payoff", sa.Integer(), nullable=True),
        sa.Column("total_interest_kop", sa.BigInteger(), nullable=True),
        sa.Column("structure_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("start_kind IN ('acceptance_balances', 'month_start_actual')",
                           name="ck_debt_plan_versions_start_kind"),
        sa.CheckConstraint("status IN ('accepted', 'superseded')", name="ck_debt_plan_versions_status"),
        sa.CheckConstraint("extract(day from effective_from) = 1", name="ck_debt_plan_versions_effective_from_day1"),
        sa.ForeignKeyConstraint(["source_plan_id"], ["milp_plans.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debt_plan_versions_user_id", "debt_plan_versions", ["user_id"])
    op.create_index("uq_debt_plan_versions_accepted_effective_from", "debt_plan_versions",
                    ["user_id", "effective_from"], unique=True, postgresql_where=sa.text("status = 'accepted'"))
    op.create_index("uq_debt_plan_versions_one_accepted", "debt_plan_versions", ["user_id"], unique=True,
                    postgresql_where=sa.text("status = 'accepted'"))
    op.create_index("uq_debt_plan_versions_user_version_no", "debt_plan_versions", ["user_id", "version_no"],
                    unique=True)
    op.create_table(
        "debt_plan_schedule",
        sa.Column("version_id", sa.String(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("debt_id", sa.String(), nullable=False),
        sa.Column("opening_kop", sa.BigInteger(), nullable=False),
        sa.Column("interest_kop", sa.BigInteger(), nullable=False),
        sa.Column("planned_payment_kop", sa.BigInteger(), nullable=False),
        sa.Column("closing_kop", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["debt_id"], ["debts.id"]),
        sa.ForeignKeyConstraint(["version_id"], ["debt_plan_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("version_id", "month", "debt_id"),
    )

    op.add_column("milp_plans", sa.Column("debts_structure_fingerprint", sa.String(length=64), nullable=True))
    op.execute("UPDATE milp_plans SET status = 'draft'")
    op.alter_column("milp_plans", "status", server_default="draft")
    op.create_check_constraint("ck_milp_plans_status", "milp_plans", "status IN ('draft')")

    bind = op.get_bind()
    today = bind.execute(sa.text("SELECT (now() AT TIME ZONE 'Europe/Kyiv')::date")).scalar_one()
    debts = bind.execute(sa.text("SELECT id, user_id, balance FROM debts")).all()
    checks = sa.table(
        "debt_balance_checks",
        sa.column("id", sa.String), sa.column("user_id", sa.String), sa.column("debt_id", sa.String),
        sa.column("as_of", sa.Date), sa.column("balance_kop", sa.BigInteger), sa.column("source", sa.String),
    )
    rows = [
        {"id": str(uuid.uuid4()), "user_id": user_id, "debt_id": debt_id, "as_of": today,
         "balance_kop": max(_uah_to_kop(balance), 0), "source": "migration"}
        for debt_id, user_id, balance in debts
    ]
    if rows:
        op.bulk_insert(checks, rows)


def downgrade() -> None:
    """Видаляє таблиці план-факту; у debts.balance переносить останню звірку боргу.

    Платежі після останньої звірки й відсотки в debts.balance не переносяться.
    """
    op.execute("""
        UPDATE debts d SET balance = c.balance_kop / 100.0
        FROM (
            SELECT DISTINCT ON (debt_id) debt_id, balance_kop
            FROM debt_balance_checks
            ORDER BY debt_id, as_of DESC, created_at DESC
        ) c
        WHERE c.debt_id = d.id AND c.balance_kop > 0
    """)
    op.drop_table("debt_plan_schedule")
    op.drop_index("uq_debt_plan_versions_user_version_no", table_name="debt_plan_versions")
    op.drop_index("uq_debt_plan_versions_one_accepted", table_name="debt_plan_versions")
    op.drop_index("uq_debt_plan_versions_accepted_effective_from", table_name="debt_plan_versions")
    op.drop_index("ix_debt_plan_versions_user_id", table_name="debt_plan_versions")
    op.drop_table("debt_plan_versions")
    op.drop_index("ix_debt_payments_user_paid_on", table_name="debt_payments")
    op.drop_index("ix_debt_payments_debt_paid_on", table_name="debt_payments")
    op.drop_table("debt_payments")
    op.drop_index("uq_debt_balance_checks_debt_as_of", table_name="debt_balance_checks")
    op.drop_index("ix_debt_balance_checks_user_id", table_name="debt_balance_checks")
    op.drop_index("ix_debt_balance_checks_debt_as_of", table_name="debt_balance_checks")
    op.drop_table("debt_balance_checks")
    op.drop_table("debt_plan_months")

    op.drop_constraint("ck_milp_plans_status", "milp_plans", type_="check")
    op.alter_column("milp_plans", "status", server_default=None)
    op.execute("UPDATE milp_plans SET status = 'active'")
    op.drop_column("milp_plans", "debts_structure_fingerprint")
