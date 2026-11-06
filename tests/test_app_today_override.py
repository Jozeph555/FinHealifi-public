"""
Керована дата APP_TODAY_OVERRIDE.

Змінну не задано — «сьогодні» реальне; задано — today_kyiv, get_today,
resolve_today, журнал боргу, межі місяців план-факту, перевірка «дата в
майбутньому» і GET /debt/today беруть її; неправильний формат — помилка;
на старті застосунку — WARNING у журнал; через API дату не змінити.
Наскрізний HTTP-сценарій грудня: другу версію прийнято 20.11; 1–5 грудня
materialize без force — 409, з force — 409, доки листопад не заповнено чи не
позначено, і 201 після цього; з 6 грудня — без force; після фіксації
preview зникає. «Сьогодні» в HTTP-тестах задає лише змінна середовища.
"""
import logging
import uuid
from datetime import date, datetime

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import app.main as main
from app.api.deps import get_today, resolve_today
from app.core.config import today_override
from app.main import app as fastapi_app
from app.models.database import AsyncSessionLocal
from app.models.debt import Debt
from app.models.debt_plan import (
    DebtBalanceCheck, DebtPayment, DebtPlanMonth, DebtPlanSchedule, DebtPlanVersion,
)
from app.models.milp_plan import MilpPlan
from app.models.user import User
from app.services import debt_ledger as ledger
from app.services.affordability import historical_free_cash_flow

ENV = "APP_TODAY_OVERRIDE"
INSTALLMENT = {"name": "Розстрочка", "balance": 10_000, "monthly_rate": 0.02, "min_payment": 1_000,
               "debt_type": "installment"}


def _set_today(monkeypatch, value: str) -> None:
    monkeypatch.setenv(ENV, value)


# --- модуль ------------------------------------------------------------------------------------

def test_without_variable_today_is_real():
    assert today_override() is None
    real = datetime.now(ledger.KYIV_TZ).date()
    assert ledger.today_kyiv() == real
    assert get_today() == real
    assert resolve_today(object()) == real


def test_variable_sets_today_everywhere(monkeypatch):
    _set_today(monkeypatch, "2026-12-02")
    assert today_override() == date(2026, 12, 2)
    assert ledger.today_kyiv() == date(2026, 12, 2)
    assert get_today() == date(2026, 12, 2)
    assert resolve_today(object()) == date(2026, 12, 2)
    # Явна дата має пріоритет над змінною.
    assert ledger.today_kyiv(date(2026, 1, 5)) == date(2026, 1, 5)
    assert resolve_today(date(2026, 1, 5)) == date(2026, 1, 5)


@pytest.mark.parametrize("value", ["2026-12-2", "02.12.2026", "20261202", "2026-13-01", "завтра"])
def test_invalid_value_is_an_error(monkeypatch, value):
    _set_today(monkeypatch, value)
    with pytest.raises(RuntimeError, match=ENV):
        ledger.today_kyiv()


def test_empty_value_means_not_set(monkeypatch):
    _set_today(monkeypatch, "  ")
    assert today_override() is None


async def test_startup_warns_when_variable_is_set(monkeypatch, caplog):
    monkeypatch.setattr(main, "start_scheduler", lambda: None)
    monkeypatch.setattr(main, "stop_scheduler", lambda: None)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        async with main.lifespan(fastapi_app):
            pass
    assert ENV not in caplog.text

    _set_today(monkeypatch, "2026-12-02")
    with caplog.at_level(logging.WARNING, logger="app.main"):
        async with main.lifespan(fastapi_app):
            pass
    records = [r for r in caplog.records if r.name == "app.main" and r.levelno == logging.WARNING]
    assert len(records) == 1 and "APP_TODAY_OVERRIDE=2026-12-02" in records[0].getMessage()


async def test_ledger_and_affordability_use_variable(monkeypatch, db_session, real_user):
    debt = Debt(id=str(uuid.uuid4()), user_id=real_user.id, name="Картка", balance=1000.0, monthly_rate=0.03,
                debt_type="revolving", is_active=True)
    db_session.add(debt)
    await db_session.flush()
    db_session.add(DebtBalanceCheck(id=str(uuid.uuid4()), user_id=real_user.id, debt_id=debt.id,
                                    as_of=date(2026, 12, 1), balance_kop=100_000, source="statement"))
    db_session.add(DebtBalanceCheck(id=str(uuid.uuid4()), user_id=real_user.id, debt_id=debt.id,
                                    as_of=date(2026, 12, 5), balance_kop=80_000, source="statement"))
    await db_session.flush()

    _set_today(monkeypatch, "2026-12-02")
    states = await ledger.active_debt_states(db_session, real_user.id)
    assert states[0].balance_kop == 100_000, "звірка 05.12 ще в майбутньому"
    _set_today(monkeypatch, "2026-12-08")
    states = await ledger.active_debt_states(db_session, real_user.id)
    assert states[0].balance_kop == 80_000

    calls = []
    import app.services.affordability as affordability

    real_window = affordability._select_window

    def _window(coverage, today):
        calls.append(today)
        return real_window(coverage, today)
    monkeypatch.setattr(affordability, "_select_window", _window)
    await historical_free_cash_flow(db_session, real_user.id, 100.0)
    assert calls == [date(2026, 12, 8)]


# --- HTTP --------------------------------------------------------------------------------------

async def _cleanup(user_id: str) -> None:
    async with AsyncSessionLocal() as s:
        version_ids = select(DebtPlanVersion.id).where(DebtPlanVersion.user_id == user_id)
        await s.execute(delete(DebtPlanSchedule).where(DebtPlanSchedule.version_id.in_(version_ids)))
        for model in (DebtPlanVersion, DebtPayment, DebtBalanceCheck, DebtPlanMonth, MilpPlan, Debt):
            await s.execute(delete(model).where(model.user_id == user_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


@pytest_asyncio.fixture
async def api():
    """Клієнт із JWT тимчасового користувача; «сьогодні» не підміняється, лише змінна середовища."""
    email = f"today-{uuid.uuid4().hex[:12]}@example.com"
    password = "test-password-123"
    user_id = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as c:
        try:
            assert (await c.post("/auth/register", json={"email": email, "password": password})).status_code == 201
            resp = await c.post("/auth/login", json={"email": email, "password": password})
            c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
            user_id = (await c.get("/auth/me")).json()["id"]
            yield c
        finally:
            if user_id is None:
                async with AsyncSessionLocal() as s:
                    user_id = (await s.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
            if user_id is not None:
                await _cleanup(user_id)


async def test_today_endpoint(api, monkeypatch):
    resp = await api.get("/debt/today")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"today": datetime.now(ledger.KYIV_TZ).date().isoformat(), "demo": False}

    _set_today(monkeypatch, "2026-12-02")
    assert (await api.get("/debt/today")).json() == {"today": "2026-12-02", "demo": True}
    # Через API дату не змінити: параметри ігноруються, інших методів немає.
    assert (await api.get("/debt/today", params={"today": "2030-01-01"})).json()["today"] == "2026-12-02"
    for method in ("post", "put"):
        assert (await getattr(api, method)("/debt/today", json={"today": "2030-01-01"})).status_code == 405
    # PATCH іде в PATCH /debt/{debt_id} і не змінює дату.
    assert (await api.patch("/debt/today", json={"today": "2030-01-01"})).status_code >= 400
    assert (await api.get("/debt/today")).json()["today"] == "2026-12-02"


async def _december_setup(api, monkeypatch):
    """Перша версія 14.11, друга прийнята 20.11 (діє з 01.12); повертає (борг, друга версія)."""
    _set_today(monkeypatch, "2026-11-14")
    debt = (await api.post("/debt", json=INSTALLMENT)).json()
    plan_id = (await api.post("/debt/optimize", json={"extra_monthly_uah": 1_000})).json()["plan_id"]
    assert (await api.post(f"/debt/plans/{plan_id}/accept")).status_code == 201

    _set_today(monkeypatch, "2026-11-20")
    plan_id = (await api.post("/debt/optimize", json={"extra_monthly_uah": 2_000})).json()["plan_id"]
    resp = await api.post(f"/debt/plans/{plan_id}/accept")
    assert resp.status_code == 201, resp.text
    v2 = resp.json()
    assert v2["effective_from"] == "2026-12-01" and v2["materialized"] is False
    return debt, v2


@pytest.mark.parametrize("fill", ["payment", "mark"])
async def test_december_scenario_force_before_sixth(api, monkeypatch, fill):
    debt, v2 = await _december_setup(api, monkeypatch)

    _set_today(monkeypatch, "2026-12-02")
    assert (await api.get("/debt/today")).json() == {"today": "2026-12-02", "demo": True}
    active = (await api.get("/debt/plan/active")).json()
    assert active["in_force_version_id"] == v2["version_id"]
    assert active["preview"] is not None

    early = await api.post("/debt/plan/materialize")
    assert early.status_code == 409 and "force=true" in early.json()["detail"]
    unfilled = await api.post("/debt/plan/materialize", params={"force": "true"})
    assert unfilled.status_code == 409 and unfilled.json()["unresolved_month"] == "2026-11"
    assert "листопад 2026 р." in unfilled.json()["detail"]

    # Майбутня дата відносно демонстраційної — відмова.
    future = await api.post("/debt/payments", json={"debt_id": debt["id"], "paid_on": "2026-12-03", "amount": 100})
    assert future.status_code == 422

    if fill == "payment":
        resp = await api.post("/debt/payments", json={"debt_id": debt["id"], "paid_on": "2026-11-25", "amount": 1_500})
        assert resp.status_code == 201, resp.text
    else:
        resp = await api.put("/debt/plan-fact/months/2026-11/no-payments", json={"confirmed": True})
        assert resp.status_code == 200, resp.text

    forced = await api.post("/debt/plan/materialize", params={"force": "true"})
    assert forced.status_code == 201, forced.text
    assert forced.json()["materialized"] is True and forced.json()["preview"] is None
    active = (await api.get("/debt/plan/active")).json()
    assert active["materialized"] is True and active["preview"] is None and active["pending"] is None
    assert active["schedule"][0]["month"] == "2026-12"

    months = {m["month"]: m for m in (await api.get("/debt/plan-fact")).json()["months"]}
    assert list(months)[-1] == "2026-12"
    assert months["2026-11"]["closed"] is True and months["2026-11"]["editable"] is False
    assert months["2026-12"]["closed"] is False


async def test_december_scenario_from_sixth_without_force(api, monkeypatch):
    debt, v2 = await _december_setup(api, monkeypatch)
    resp = await api.post("/debt/payments", json={"debt_id": debt["id"], "paid_on": "2026-11-20", "amount": 1_500})
    assert resp.status_code == 201, resp.text

    _set_today(monkeypatch, "2026-12-05")
    assert (await api.post("/debt/plan/materialize")).status_code == 409

    _set_today(monkeypatch, "2026-12-06")
    assert (await api.get("/debt/today")).json()["today"] == "2026-12-06"
    fixed = await api.post("/debt/plan/materialize")
    assert fixed.status_code == 201, fixed.text
    assert fixed.json()["preview"] is None
    assert (await api.post("/debt/plan/materialize")).status_code == 200
    active = (await api.get("/debt/plan/active")).json()
    assert active["version_id"] == v2["version_id"] and active["preview"] is None and active["pending"] is None
