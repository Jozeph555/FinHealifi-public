"""
Наскрізні HTTP-тести план-факту боргів проти застосунку app.main.

Тимчасовий користувач реєструється й входить через /auth/register і
/auth/login; усі запити йдуть справжніми маршрутами з JWT-заголовком і
справжньою залежністю get_db (дані комітяться в тестову БД і видаляються
після тесту). httpx.ASGITransport не надсилає lifespan-подій, тож
планувальник синхронізації Monobank не запускається.

«Сьогодні» задається підміною app.api.deps.today_kyiv, яку викликають
get_today і resolve_today. Збої розв'язувача імітуються підміною
debt_optimizer.optimize_milp (і _within_budget для відповіді без
запасного плану).

Сценарії: чернетка й прийняття першого плану, повторне прийняття, платіж до
дати прийняття, розбіжність поточного місяця, позначка «платежів не було» при
наявних платежах, PATCH залишку як звірка, другий план посеред місяця й
матеріалізація, 503 під час accept і materialize, запасний план avalanche у
знімку, переплата понад залишок.
"""
import time
import uuid
from datetime import date

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import app.api.deps as deps
import app.services.debt_optimizer as debt_optimizer
from app.main import app as fastapi_app
from app.models.database import AsyncSessionLocal
from app.models.debt import Debt
from app.models.debt_plan import (
    DebtBalanceCheck, DebtPayment, DebtPlanMonth, DebtPlanSchedule, DebtPlanVersion,
)
from app.models.milp_plan import MilpPlan
from app.models.user import User

INSTALLMENT = {"name": "Розстрочка", "balance": 10_000, "monthly_rate": 0.02, "min_payment": 1_000,
               "debt_type": "installment"}
CARD = {"name": "Картка", "balance": 7_345.67, "monthly_rate": 0.0347, "min_payment": None, "debt_type": "revolving"}
NOT_FINISHED_TEXT = "не встиг завершитися"


@pytest_asyncio.fixture
async def clock(monkeypatch):
    """Керований «сьогодні»: підміняє app.api.deps.today_kyiv."""
    state = {"today": date(2026, 11, 14)}
    monkeypatch.setattr(deps, "today_kyiv", lambda today=None: state["today"])
    return state


async def _cleanup(user_id: str) -> None:
    async with AsyncSessionLocal() as s:
        version_ids = select(DebtPlanVersion.id).where(DebtPlanVersion.user_id == user_id)
        await s.execute(delete(DebtPlanSchedule).where(DebtPlanSchedule.version_id.in_(version_ids)))
        for model in (DebtPlanVersion, DebtPayment, DebtBalanceCheck, DebtPlanMonth, MilpPlan, Debt):
            await s.execute(delete(model).where(model.user_id == user_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


@pytest_asyncio.fixture
async def api(clock):
    """Клієнт із JWT тимчасового користувача, зареєстрованого через /auth/register."""
    email = f"plan-fact-{uuid.uuid4().hex[:12]}@example.com"
    password = "test-password-123"
    user_id = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as c:
        try:
            resp = await c.post("/auth/register", json={"email": email, "password": password})
            assert resp.status_code == 201, resp.text
            resp = await c.post("/auth/login", json={"email": email, "password": password})
            assert resp.status_code == 200, resp.text
            c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
            me = await c.get("/auth/me")
            assert me.status_code == 200, me.text
            user_id = me.json()["id"]
            yield c
        finally:
            if user_id is None:
                async with AsyncSessionLocal() as s:
                    user_id = (await s.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
            if user_id is not None:
                await _cleanup(user_id)


# --- допоміжні запити ------------------------------------------------------------------------

async def _debt(api, **body):
    resp = await api.post("/debt", json={**INSTALLMENT, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _draft(api, extra=1_000):
    resp = await api.post("/debt/optimize", json={"extra_monthly_uah": extra})
    assert resp.status_code == 201, resp.text
    assert resp.json()["plan_state"] == "draft"
    return resp.json()["plan_id"]


async def _pay(api, debt_id, day, amount):
    return await api.post("/debt/payments", json={"debt_id": debt_id, "paid_on": day.isoformat(), "amount": amount})


async def _plan_fact(api, **params):
    resp = await api.get("/debt/plan-fact", params=params)
    assert resp.status_code == 200, resp.text
    return {m["month"]: m for m in resp.json()["months"]}


def _row(month, debt_id):
    return next(r for r in month["debts"] if r["debt_id"] == debt_id)


def _planned(version, month, debt_id):
    m = next(m for m in version["schedule"] if m["month"] == month)
    return next(d for d in m["debts"] if d["debt_id"] == debt_id)


def _numbers(value):
    """Усі числа з вкладеної JSON-структури."""
    if isinstance(value, dict):
        for v in value.values():
            yield from _numbers(v)
    elif isinstance(value, list):
        for v in value:
            yield from _numbers(v)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield value


async def _versions_in_db(api):
    user_id = (await api.get("/auth/me")).json()["id"]
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(DebtPlanVersion).where(DebtPlanVersion.user_id == user_id)
        )).scalars().all()
    return {v.id: v for v in rows}


# --- 1–5: перший план, повторне прийняття, платежі, розбіжність --------------------------------

async def test_first_plan_accept_payments_and_divergence(api, clock):
    a = await _debt(api)
    b = await _debt(api, **CARD)
    plan_id = await _draft(api)

    # 1. Прийняття першого плану.
    resp = await api.post(f"/debt/plans/{plan_id}/accept")
    assert resp.status_code == 201, resp.text
    v1 = resp.json()
    assert v1["version_no"] == 1
    assert v1["effective_from"] == "2026-11-01"
    assert v1["accepted_on"] == "2026-11-14"
    assert v1["start_kind"] == "acceptance_balances"
    assert v1["materialized"] is True
    assert v1["schedule"] is not None and len(v1["schedule"]) > 0
    assert v1["schedule"][0]["month"] == "2026-11"

    # 2. Повторне прийняття тієї самої чернетки.
    again = await api.post(f"/debt/plans/{plan_id}/accept")
    assert again.status_code == 200, again.text
    assert again.json()["version_id"] == v1["version_id"] and again.json()["version_no"] == 1

    # 3. Платіж раніше дати прийняття і в день прийняття.
    early = await _pay(api, a["id"], date(2026, 11, 13), 500)
    assert early.status_code == 409, early.text
    planned_a = _planned(v1, "2026-11", a["id"])["payment"]
    on_day = await _pay(api, a["id"], date(2026, 11, 14), planned_a - 100)
    assert on_day.status_code == 201, on_day.text

    # 4. План-факт поточного місяця: розбіжність = факт мінус план.
    nov = (await _plan_fact(api, **{"from": "2026-11", "to": "2026-11"}))["2026-11"]
    assert nov["entry_state"] == "entered"
    row_a, row_b = _row(nov, a["id"]), _row(nov, b["id"])
    assert row_a["actual_payment"] == planned_a - 100
    assert row_a["payment_diff"] == pytest.approx(row_a["actual_payment"] - row_a["planned_payment"])
    assert row_a["payment_diff"] == pytest.approx(-100)
    assert row_b["actual_payment"] == 0
    assert row_b["payment_diff"] == pytest.approx(-row_b["planned_payment"])
    assert nov["totals"]["payment_diff"] == pytest.approx(row_a["payment_diff"] + row_b["payment_diff"])

    # 5. Позначка «платежів не було» при наявних платежах.
    mark = await api.put("/debt/plan-fact/months/2026-11/no-payments", json={"confirmed": True})
    assert mark.status_code == 409, mark.text


# --- 6: PATCH залишку -------------------------------------------------------------------------

async def test_patch_balance_creates_manual_edit_check(api):
    debt = await _debt(api)
    resp = await api.patch(f"/debt/{debt['id']}", json={"balance": 8_765.43})
    assert resp.status_code == 200, resp.text
    assert resp.json()["balance"] == 8_765.43

    listed = {d["id"]: d for d in (await api.get("/debt")).json()}
    assert listed[debt["id"]]["balance"] == 8_765.43

    async with AsyncSessionLocal() as s:
        stored = await s.get(Debt, debt["id"])
        checks = (await s.execute(
            select(DebtBalanceCheck.source, DebtBalanceCheck.balance_kop, DebtBalanceCheck.as_of)
            .where(DebtBalanceCheck.debt_id == debt["id"])
        )).all()
    assert stored.balance == 10_000
    assert ("manual_edit", 876_543, date(2026, 11, 14)) in [tuple(c) for c in checks]


# --- 7: другий план посеред місяця ------------------------------------------------------------

async def test_second_plan_mid_month_and_materialize(api, clock):
    debt = await _debt(api)
    v1 = (await api.post(f"/debt/plans/{await _draft(api)}/accept")).json()
    assert (await _pay(api, debt["id"], date(2026, 11, 14), 1_500)).status_code == 201

    clock["today"] = date(2026, 11, 20)
    resp = await api.post(f"/debt/plans/{await _draft(api, extra=2_000)}/accept")
    assert resp.status_code == 201, resp.text
    v2 = resp.json()
    assert v2["version_no"] == 2 and v2["status"] == "accepted"
    assert v2["effective_from"] == "2026-12-01" and v2["start_kind"] == "month_start_actual"
    assert v2["materialized"] is False and v2["schedule"] == []
    assert v2["in_force_version_id"] == v1["version_id"]

    stored = await _versions_in_db(api)
    assert stored[v1["version_id"]].status == "superseded"
    assert stored[v1["version_id"]].effective_to == date(2026, 12, 1)
    nov = (await _plan_fact(api))["2026-11"]
    assert nov["version_id"] == v1["version_id"]

    clock["today"] = date(2026, 12, 3)
    early = await api.post("/debt/plan/materialize")
    assert early.status_code == 409, early.text
    assert "force=true" in early.json()["detail"]

    forced = await api.post("/debt/plan/materialize", params={"force": "true"})
    assert forced.status_code == 201, forced.text
    body = forced.json()
    assert body["version_id"] == v2["version_id"] and body["materialized"] is True
    assert body["schedule"][0]["month"] == "2026-12"
    nov_closing = _row((await _plan_fact(api))["2026-11"], debt["id"])["actual_closing"]
    assert _planned(body, "2026-12", debt["id"])["opening"] == nov_closing


async def test_forced_materialize_rejected_when_previous_month_empty(api, clock):
    await _debt(api)
    await api.post(f"/debt/plans/{await _draft(api)}/accept")
    clock["today"] = date(2026, 11, 20)
    assert (await api.post(f"/debt/plans/{await _draft(api, extra=2_000)}/accept")).status_code == 201

    clock["today"] = date(2026, 12, 3)
    resp = await api.post("/debt/plan/materialize", params={"force": "true"})
    assert resp.status_code == 409, resp.text
    assert "платежів не було" in resp.json()["detail"]


# --- 8а: розв'язувач не встиг ------------------------------------------------------------------

@pytest.fixture
def hanging_solver_without_fallback(monkeypatch):
    """Розв'язувач не завершується в межах ліміту, а avalanche вважається поза бюджетом."""
    import app.services.milp_optimizer as milp_optimizer

    def _hang(*args):
        time.sleep(1.0)
        raise AssertionError("розв'язувач мав бути перерваний лімітом")

    def _install():
        monkeypatch.setattr(milp_optimizer, "MILP_TIME_LIMIT_SEC", 0.05)
        monkeypatch.setattr(milp_optimizer, "MILP_TIMEOUT_GRACE_SEC", 0.1)
        monkeypatch.setattr(debt_optimizer, "optimize_milp", _hang)
        monkeypatch.setattr(debt_optimizer, "_within_budget", lambda *a: False)

    return _install


async def test_accept_returns_503_when_solver_does_not_finish(api, hanging_solver_without_fallback):
    await _debt(api)
    plan_id = await _draft(api)
    hanging_solver_without_fallback()

    resp = await api.post(f"/debt/plans/{plan_id}/accept")
    assert resp.status_code == 503, resp.text
    assert NOT_FINISHED_TEXT in resp.json()["detail"]
    assert (await api.get("/debt/plan/active")).status_code == 404


async def test_materialize_returns_503_when_solver_does_not_finish(api, clock, hanging_solver_without_fallback):
    debt = await _debt(api)
    await api.post(f"/debt/plans/{await _draft(api)}/accept")
    assert (await _pay(api, debt["id"], date(2026, 11, 14), 1_000)).status_code == 201
    clock["today"] = date(2026, 11, 20)
    assert (await api.post(f"/debt/plans/{await _draft(api, extra=2_000)}/accept")).status_code == 201

    clock["today"] = date(2026, 12, 6)
    hanging_solver_without_fallback()
    resp = await api.post("/debt/plan/materialize")
    assert resp.status_code == 503, resp.text
    assert NOT_FINISHED_TEXT in resp.json()["detail"]
    assert (await api.get("/debt/plan/active")).json()["materialized"] is False


# --- 8б: запасний план avalanche у знімку -------------------------------------------------------

@pytest.mark.parametrize("solver_status", ["Not Solved", "Time Limit"])
async def test_snapshot_uses_avalanche_fallback_when_milp_fails(api, monkeypatch, solver_status):
    await _debt(api)
    await _debt(api, **CARD)
    plan_id = await _draft(api)
    real = debt_optimizer.optimize_milp
    monkeypatch.setattr(debt_optimizer, "optimize_milp", lambda *a: {**real(*a), "status": solver_status})

    resp = await api.post(f"/debt/plans/{plan_id}/accept")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["strategy"] == "avalanche_fallback"
    assert body["solver_status"] == solver_status
    assert body["materialized"] is True and body["schedule"]
    assert body["months_to_payoff"] is not None


async def test_snapshot_fallback_after_solver_timeout(api, monkeypatch):
    import app.services.milp_optimizer as milp_optimizer

    await _debt(api)
    plan_id = await _draft(api)

    def _hang(*args):
        time.sleep(1.0)
        raise AssertionError("розв'язувач мав бути перерваний лімітом")

    monkeypatch.setattr(milp_optimizer, "MILP_TIME_LIMIT_SEC", 0.05)
    monkeypatch.setattr(milp_optimizer, "MILP_TIMEOUT_GRACE_SEC", 0.1)
    monkeypatch.setattr(debt_optimizer, "optimize_milp", _hang)

    resp = await api.post(f"/debt/plans/{plan_id}/accept")
    assert resp.status_code == 201, resp.text
    assert resp.json()["strategy"] == "avalanche_fallback"
    assert resp.json()["solver_status"] == debt_optimizer.STATUS_TIMEOUT


# --- 8в: переплата понад залишок ------------------------------------------------------------------

async def test_overpayment_closes_debt_without_negative_values(api):
    debt = await _debt(api)
    await api.post(f"/debt/plans/{await _draft(api)}/accept")

    resp = await _pay(api, debt["id"], date(2026, 11, 14), 50_000)
    assert resp.status_code == 201, resp.text

    result = (await api.get("/debt/plan-fact")).json()
    nov = next(m for m in result["months"] if m["month"] == "2026-11")
    row = _row(nov, debt["id"])
    assert row["actual_closing"] == 0
    assert row["overpaid"] == pytest.approx(50_000 - 10_200)
    assert row["actual_payment"] == 50_000
    numbers_except_diffs = _numbers({k: v for k, v in row.items() if not k.endswith("_diff")})
    assert all(n >= 0 for n in numbers_except_diffs)
    listed = {d["id"]: d for d in (await api.get("/debt")).json()}
    assert listed[debt["id"]]["balance"] == 0
