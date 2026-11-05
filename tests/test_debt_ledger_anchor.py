"""
Залишок боргу з якорів журналу: відсотки місяця нараховуються на кінець місяця.

Правило: залишок на дату D = залишок останнього якоря <= D мінус платежі після
якоря; відсотки місяця нараховуються на кінець місяця і до залишку всередині
місяця не входять.

- борг 35 000 грн під 5 %/міс.: якір 'migration' на дату D, потім прийняття
  плану додає якір 'plan_accepted' на ту саму дату; залишок на D — 35 000 і
  в balance_as_of, і в GET /debt;
- платіж зменшує залишок, видалення платежу його повертає;
- два якорі на одну дату з різним часом створення: усередині місяця залишок
  однаковий, на кінець місяця діє пізніший;
- якір у попередньому місяці: відсотки нараховано за закриті місяці, за
  поточний — ні.
"""
from datetime import date

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

import app.api.debt as debt_module
import app.api.debt_plan_fact as plan_fact_module
from app.api.deps import get_current_user, get_db, get_today
from app.models.debt import Debt
from app.models.debt_plan import DebtBalanceCheck
from app.services import debt_ledger as ledger

LOAN = {"name": "Позика", "balance": 35_000, "monthly_rate": 0.05, "min_payment": 2_000,
        "debt_type": "installment"}
D = date(2026, 11, 4)
NOV = date(2026, 11, 1)


@pytest_asyncio.fixture
async def clock():
    return {"today": D}


@pytest_asyncio.fixture
async def client(db_session, real_user, clock):
    app = FastAPI()
    app.include_router(debt_module.router)
    app.include_router(plan_fact_module.router)

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: real_user
    app.dependency_overrides[get_today] = lambda: clock["today"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _loan_with_migration_anchor(db_session, real_user) -> Debt:
    """Борг як після міграції: рядок debts і якір 'migration' на дату D."""
    debt = Debt(id=f"loan-{real_user.id}", user_id=real_user.id, **LOAN, is_active=True)
    db_session.add(debt)
    await db_session.flush()
    await ledger.upsert_check(db_session, real_user.id, debt.id, D, 3_500_000, "migration")
    await db_session.commit()
    return debt


async def _accept(client):
    draft = await client.post("/debt/optimize", json={"extra_monthly_uah": 1_000})
    assert draft.status_code == 201, draft.text
    resp = await client.post(f"/debt/plans/{draft.json()['plan_id']}/accept")
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _listed_balance(client, debt_id):
    return {d["id"]: d for d in (await client.get("/debt")).json()}[debt_id]["balance"]


# --- дефект: другий якір на ту саму дату після прийняття --------------------------------------

async def test_balance_unchanged_after_accept_adds_same_day_anchor(client, db_session, real_user):
    debt = await _loan_with_migration_anchor(db_session, real_user)
    assert await _listed_balance(client, debt.id) == 35_000

    await _accept(client)

    sources = (await db_session.execute(
        select(DebtBalanceCheck.source).where(DebtBalanceCheck.debt_id == debt.id, DebtBalanceCheck.as_of == D)
    )).scalars().all()
    assert sorted(sources) == ["migration", "plan_accepted"]
    assert await ledger.balance_as_of(db_session, debt, D) == 3_500_000
    assert await _listed_balance(client, debt.id) == 35_000


async def test_payment_then_delete_restores_balance(client, db_session, real_user):
    debt = await _loan_with_migration_anchor(db_session, real_user)
    await _accept(client)

    resp = await client.post("/debt/payments", json={"debt_id": debt.id, "paid_on": D.isoformat(), "amount": 5_000})
    assert resp.status_code == 201, resp.text
    assert await _listed_balance(client, debt.id) == 30_000

    assert (await client.delete(f"/debt/payments/{resp.json()['id']}")).status_code == 204
    assert await _listed_balance(client, debt.id) == 35_000


# --- два якорі на одну дату ---------------------------------------------------------------------

def _balance(anchors, payments, through):
    return ledger.debt_series(anchors, payments, 0.05, NOV, through)[-1].closing


def test_same_day_anchors_mid_month_balance_is_anchor_minus_payments():
    migration = ledger.Anchor(D, 3_500_000, "migration")
    plan = ledger.Anchor(D, 3_500_000, ledger.PLAN_ANCHOR_SOURCE)
    payment = [(D, 100_000), (date(2026, 11, 10), 50_000)]

    # Порядок у списку = порядок створення: пізніший якір діє.
    assert _balance([migration, plan], [], D) == 3_500_000
    assert _balance([plan, migration], [], D) == 3_500_000
    # Стартовий залишок плану діє з початку дня: платіж того ж дня віднімається.
    assert _balance([migration, plan], payment, date(2026, 11, 10)) == 3_500_000 - 150_000
    # Звірка — на кінець дня: віднімаються лише платежі після неї.
    assert _balance([plan, migration], payment, date(2026, 11, 10)) == 3_500_000 - 50_000


def test_same_day_anchors_month_end_follows_later_anchor():
    migration = ledger.Anchor(D, 3_500_000, "migration")
    plan = ledger.Anchor(D, 3_500_000, ledger.PLAN_ANCHOR_SOURCE)
    nov_end = date(2026, 11, 30)

    # Пізніший — стартовий залишок плану: відсотки листопада на кінець місяця.
    assert _balance([migration, plan], [], nov_end) == 3_675_000
    # Пізніша — звірка: до кінця її місяця відсотків немає.
    assert _balance([plan, migration], [], nov_end) == 3_500_000


# --- якір у попередньому місяці ------------------------------------------------------------------

def test_anchor_in_previous_month_accrues_closed_months_only():
    plan = ledger.Anchor(D, 3_500_000, ledger.PLAN_ANCHOR_SOURCE)

    assert _balance([plan], [], date(2026, 11, 29)) == 3_500_000
    assert _balance([plan], [], date(2026, 11, 30)) == 3_675_000  # кінець листопада
    assert _balance([plan], [], date(2026, 12, 10)) == 3_675_000  # грудень ще не закрито
    assert _balance([plan], [(date(2026, 12, 5), 200_000)], date(2026, 12, 10)) == 3_475_000
    assert _balance([plan], [], date(2026, 12, 31)) == 3_675_000 + 183_750


async def test_get_debt_in_next_month_includes_closed_month_interest(client, db_session, real_user, clock):
    debt = await _loan_with_migration_anchor(db_session, real_user)
    await _accept(client)

    clock["today"] = date(2026, 12, 10)
    assert await _listed_balance(client, debt.id) == 36_750


async def test_balance_edit_on_acceptance_day_overrides_plan_anchor(client, db_session, real_user):
    """Звірка, замінена в день прийняття плану, діє після стартового залишку плану."""
    debt = await _loan_with_migration_anchor(db_session, real_user)
    await _accept(client)

    assert (await client.patch(f"/debt/{debt.id}", json={"balance": 30_000})).status_code == 200
    assert await _listed_balance(client, debt.id) == 30_000
    assert await ledger.balance_as_of(db_session, debt, date(2026, 11, 30)) == 3_000_000
