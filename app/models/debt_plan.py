"""
Моделі «план-факт» для боргів: прийняті версії плану, знімок помісячного
розкладу, платежі за боргами, звірки залишку й позначки місяців.

Усі суми — цілі копійки (BigInteger). Місяць зберігається як Date першого
числа місяця; дати платежів і звірок — календарні дати без часу.
"""
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer, String, text,
)
from sqlalchemy.sql import func

from app.models.database import Base


class DebtPlanVersion(Base):
    """Прийнята версія плану погашення.

    Версія діє для місяців [effective_from, effective_to); effective_to
    ставиться, коли версію замінює інша, і дорівнює effective_from нової.
    Якщо effective_to <= effective_from, версія не діяла жодного місяця.
    materialized_at — коли збережено знімок розкладу (debt_plan_schedule);
    до цього версія не має планових чисел.
    """
    __tablename__ = "debt_plan_versions"
    __table_args__ = (
        CheckConstraint("status IN ('accepted', 'superseded')", name="ck_debt_plan_versions_status"),
        CheckConstraint("start_kind IN ('acceptance_balances', 'month_start_actual')",
                        name="ck_debt_plan_versions_start_kind"),
        CheckConstraint("extract(day from effective_from) = 1", name="ck_debt_plan_versions_effective_from_day1"),
        # Одна прийнята версія на користувача.
        Index("uq_debt_plan_versions_one_accepted", "user_id", unique=True,
              postgresql_where=text("status = 'accepted'")),
        Index("uq_debt_plan_versions_accepted_effective_from", "user_id", "effective_from", unique=True,
              postgresql_where=text("status = 'accepted'")),
        Index("uq_debt_plan_versions_user_version_no", "user_id", "version_no", unique=True),
    )

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    source_plan_id = Column(String, ForeignKey("milp_plans.id"), nullable=False)
    version_no = Column(Integer, nullable=False)
    status = Column(String, nullable=False)  # 'accepted' | 'superseded'
    accepted_at = Column(DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp())
    accepted_on = Column(Date, nullable=False)  # дата прийняття за Europe/Kyiv
    effective_from = Column(Date, nullable=False)  # перше число місяця
    effective_to = Column(Date, nullable=True)  # виключна межа; NULL — версія чинна
    start_kind = Column(String, nullable=False)  # 'acceptance_balances' | 'month_start_actual'
    extra_monthly_kop = Column(BigInteger, nullable=False)
    strategy = Column(String, nullable=True)  # 'milp' | 'avalanche_fallback'; NULL до матеріалізації
    solver_status = Column(String, nullable=True)
    months_to_payoff = Column(Integer, nullable=True)  # NULL — не погашено за горизонт або не матеріалізовано
    total_interest_kop = Column(BigInteger, nullable=True)
    structure_fingerprint = Column(String(64), nullable=True)  # склад боргів знімка, без залишків
    materialized_at = Column(DateTime(timezone=True), nullable=True)
    superseded_at = Column(DateTime(timezone=True), nullable=True)


class DebtPlanSchedule(Base):
    """Плановий рядок знімка: борг у календарному місяці версії."""
    __tablename__ = "debt_plan_schedule"

    version_id = Column(String, ForeignKey("debt_plan_versions.id", ondelete="CASCADE"), primary_key=True)
    month = Column(Date, primary_key=True)  # перше число місяця
    debt_id = Column(String, ForeignKey("debts.id"), primary_key=True)
    opening_kop = Column(BigInteger, nullable=False)
    interest_kop = Column(BigInteger, nullable=False)
    planned_payment_kop = Column(BigInteger, nullable=False)
    closing_kop = Column(BigInteger, nullable=False)


class DebtPayment(Base):
    """Платіж за боргом, внесений користувачем вручну."""
    __tablename__ = "debt_payments"
    __table_args__ = (
        CheckConstraint("amount_kop > 0", name="ck_debt_payments_amount_positive"),
        Index("ix_debt_payments_debt_paid_on", "debt_id", "paid_on"),
        Index("ix_debt_payments_user_paid_on", "user_id", "paid_on"),
    )

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    debt_id = Column(String, ForeignKey("debts.id"), nullable=False)
    paid_on = Column(Date, nullable=False)
    amount_kop = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.clock_timestamp())
    updated_at = Column(DateTime(timezone=True), server_default=func.clock_timestamp(),
                        onupdate=func.clock_timestamp())


class DebtBalanceCheck(Base):
    """Відомий залишок боргу на дату (звірка).

    source:
    - 'statement' — залишок за випискою, внесений користувачем;
    - 'manual_edit' — зміна залишку через PATCH /debt/{id};
    - 'debt_created' — залишок під час створення боргу;
    - 'migration' — залишок наявного боргу на момент появи журналу;
    - 'plan_accepted' — стартовий залишок першої прийнятої версії плану.

    'plan_accepted' діє з початку дня as_of (платежі цього дня віднімаються,
    відсотки місяця нараховуються); решта — на кінець дня as_of (віднімаються
    лише платежі після as_of, відсотки до кінця місяця не нараховуються).
    """
    __tablename__ = "debt_balance_checks"
    __table_args__ = (
        CheckConstraint("balance_kop >= 0", name="ck_debt_balance_checks_balance_nonneg"),
        CheckConstraint(
            "source IN ('statement', 'manual_edit', 'debt_created', 'migration', 'plan_accepted')",
            name="ck_debt_balance_checks_source",
        ),
        # Одна звірка на дату; стартовий залишок плану — окремо від неї.
        Index("uq_debt_balance_checks_debt_as_of", "debt_id", "as_of", unique=True,
              postgresql_where=text("source <> 'plan_accepted'")),
        Index("ix_debt_balance_checks_debt_as_of", "debt_id", "as_of"),
    )

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    debt_id = Column(String, ForeignKey("debts.id"), nullable=False)
    as_of = Column(Date, nullable=False)
    balance_kop = Column(BigInteger, nullable=False)
    source = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.clock_timestamp())


class DebtPlanMonth(Base):
    """Позначка користувача для місяця: «платежів не було»."""
    __tablename__ = "debt_plan_months"

    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    month = Column(Date, primary_key=True)  # перше число місяця
    no_payments_confirmed = Column(Boolean, nullable=False, default=False)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
