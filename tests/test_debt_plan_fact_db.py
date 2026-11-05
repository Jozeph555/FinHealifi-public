"""
Тести план-факту, яким потрібні справжні транзакції БД.

- міграція план-факту на БД з наявними боргами й планами: звірка 'migration'
  для кожного боргу (1234.56 грн -> 123456 коп.), плани стають чернетками.
  Міграція виконується вниз і вгору всередині транзакції, яка потім
  відкочується (DDL у Postgres транзакційний);
- конкурентне прийняття тієї самої чернетки двома сесіями дає одну версію;
- конкурентна матеріалізація дає один знімок.

Конкурентні тести комітять дані в тестову БД і видаляють їх у finally.
"""
import asyncio
import os
import uuid
from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import make_url

from app.api.debt import ExtraPaymentRequest, optimize_with_extra
from app.models.database import AsyncSessionLocal
from app.models.debt import Debt
from app.models.debt_plan import (
    DebtBalanceCheck, DebtPayment, DebtPlanMonth, DebtPlanSchedule, DebtPlanVersion,
)
from app.models.milp_plan import MilpPlan
from app.models.user import User
from app.services import debt_plan_versions as versions
from app.services.debt_ledger import upsert_check

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "dd1694d9b232"


def test_migration_backfills_checks_for_existing_debts():
    url = make_url(os.environ["DATABASE_URL"]).set(drivername="postgresql+psycopg2")
    assert (url.database or "").endswith("_test")
    engine = create_engine(url)
    user_id = f"test-mig-{uuid.uuid4()}"
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                cfg = Config(str(ROOT / "alembic.ini"))
                cfg.set_main_option("script_location", str(ROOT / "alembic"))
                cfg.attributes["connection"] = conn
                command.downgrade(cfg, PREVIOUS_REVISION)

                conn.execute(text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, 'x')"),
                             {"id": user_id, "email": f"{user_id}@example.com"})
                for debt_id, balance in (("d-mig-1", 1234.56), ("d-mig-2", 10_000.0)):
                    conn.execute(text(
                        "INSERT INTO debts (id, user_id, name, balance, monthly_rate, min_payment, debt_type, is_active)"
                        " VALUES (:id, :u, 'Борг', :b, 0.02, 100, 'installment', true)"
                    ), {"id": f"{debt_id}-{user_id}", "u": user_id, "b": balance})
                conn.execute(text(
                    "INSERT INTO milp_plans (id, user_id, months_to_payoff, total_interest_paid, plan_data, status)"
                    " VALUES (:id, :u, 10, 1.0, '{}', 'active')"
                ), {"id": f"plan-{user_id}", "u": user_id})

                command.upgrade(cfg, "head")

                today = conn.execute(text("SELECT (now() AT TIME ZONE 'Europe/Kyiv')::date")).scalar_one()
                rows = conn.execute(text(
                    "SELECT c.debt_id, c.balance_kop, c.source, c.as_of FROM debt_balance_checks c"
                    " WHERE c.user_id = :u ORDER BY c.debt_id"
                ), {"u": user_id}).all()
                assert [(r.balance_kop, r.source, r.as_of) for r in rows] == [
                    (123456, "migration", today), (1_000_000, "migration", today),
                ]
                status = conn.execute(text("SELECT status FROM milp_plans WHERE user_id = :u"),
                                      {"u": user_id}).scalar_one()
                assert status == "draft"
            finally:
                trans.rollback()
    finally:
        engine.dispose()


async def _cleanup(user_id: str) -> None:
    async with AsyncSessionLocal() as s:
        version_ids = select(DebtPlanVersion.id).where(DebtPlanVersion.user_id == user_id)
        await s.execute(delete(DebtPlanSchedule).where(DebtPlanSchedule.version_id.in_(version_ids)))
        for model in (DebtPlanVersion, DebtPayment, DebtBalanceCheck, DebtPlanMonth, MilpPlan, Debt):
            await s.execute(delete(model).where(model.user_id == user_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def _setup_user_with_draft(today: date) -> tuple[User, str]:
    user = User(id=f"test-conc-{uuid.uuid4()}", email=f"conc-{uuid.uuid4()}@example.com", password_hash="x")
    async with AsyncSessionLocal() as s:
        s.add(user)
        await s.flush()
        debt = Debt(id=str(uuid.uuid4()), user_id=user.id, name="Кредит", balance=10_000.0, monthly_rate=0.02,
                    min_payment=1_000.0, debt_type="installment", is_active=True)
        s.add(debt)
        await s.flush()
        await upsert_check(s, user.id, debt.id, today, 1_000_000, "debt_created")
        await s.commit()
        draft = await optimize_with_extra(ExtraPaymentRequest(extra_monthly_uah=1_000), current_user=user, db=s,
                                          months=36, today=today)
    return user, draft["plan_id"]


async def _accept_in_own_session(user_id, plan_id, today):
    async with AsyncSessionLocal() as s:
        version, created = await versions.accept_plan(s, user_id, plan_id, today)
        return version.id, created


async def _materialize_in_own_session(user_id, today):
    async with AsyncSessionLocal() as s:
        version, created = await versions.materialize(s, user_id, today=today)
        return version.id, created


async def test_concurrent_accept_of_same_draft_creates_one_version():
    user, plan_id = await _setup_user_with_draft(date(2026, 11, 14))
    try:
        results = await asyncio.gather(
            _accept_in_own_session(user.id, plan_id, date(2026, 11, 14)),
            _accept_in_own_session(user.id, plan_id, date(2026, 11, 14)),
        )
        assert results[0][0] == results[1][0]
        assert sorted(created for _, created in results) == [False, True]
        async with AsyncSessionLocal() as s:
            count = (await s.execute(
                select(func.count()).select_from(DebtPlanVersion).where(DebtPlanVersion.user_id == user.id)
            )).scalar_one()
            anchors = (await s.execute(
                select(func.count()).select_from(DebtBalanceCheck)
                .where(DebtBalanceCheck.user_id == user.id, DebtBalanceCheck.source == "plan_accepted")
            )).scalar_one()
        assert count == 1 and anchors == 1
    finally:
        await _cleanup(user.id)


async def test_concurrent_materialize_builds_one_snapshot():
    user, plan_id = await _setup_user_with_draft(date(2026, 11, 14))
    try:
        await _accept_in_own_session(user.id, plan_id, date(2026, 11, 14))
        async with AsyncSessionLocal() as s:
            debt_id = (await s.execute(select(Debt.id).where(Debt.user_id == user.id))).scalar_one()
            s.add(DebtPayment(id=str(uuid.uuid4()), user_id=user.id, debt_id=debt_id, paid_on=date(2026, 11, 15),
                              amount_kop=200_000))
            await s.commit()
            second = await optimize_with_extra(ExtraPaymentRequest(extra_monthly_uah=2_000), current_user=user,
                                               db=s, months=36, today=date(2026, 11, 20))
        v2_id, _ = await _accept_in_own_session(user.id, second["plan_id"], date(2026, 11, 20))

        results = await asyncio.gather(
            _materialize_in_own_session(user.id, date(2026, 12, 7)),
            _materialize_in_own_session(user.id, date(2026, 12, 7)),
        )
        assert {vid for vid, _ in results} == {v2_id}
        assert sorted(created for _, created in results) == [False, True]
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(
                select(DebtPlanSchedule.month).where(DebtPlanSchedule.version_id == v2_id)
            )).scalars().all()
        assert rows and len(rows) == len(set(rows))
        assert min(rows) == date(2026, 12, 1)
    finally:
        await _cleanup(user.id)
