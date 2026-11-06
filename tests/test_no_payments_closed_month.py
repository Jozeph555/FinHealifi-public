"""
Позначка «платежів не було» лише для закритих місяців і 409 фіксації з місяцем дії.

Закритий місяць — строго раніше за поточний за today_kyiv (з APP_TODAY_OVERRIDE).
Позначка на поточному чи майбутньому місяці — 422, на минулому — 200; зняти
наявну позначку можна і на незакритому місяці. Позначка на незакритому місяці,
що вже є в БД, не рахується заповненням місяця. 409 від POST
/debt/plan/materialize за незаповненого попереднього місяця містить
unresolved_month (YYYY-MM), а текст — назву місяця без ISO.
«Сьогодні» в HTTP-тестах задає змінна APP_TODAY_OVERRIDE.
"""
import re
import uuid
from datetime import date

import httpx
import pytest_asyncio
from sqlalchemy import delete, select

from app.main import app as fastapi_app
from app.models.database import AsyncSessionLocal
from app.models.debt import Debt
from app.models.debt_plan import (
    DebtBalanceCheck, DebtPayment, DebtPlanMonth, DebtPlanSchedule, DebtPlanVersion,
)
from app.models.milp_plan import MilpPlan
from app.models.user import User
from app.services import debt_ledger as ledger
from app.services import debt_plan_versions as versions_service

ENV = "APP_TODAY_OVERRIDE"
INSTALLMENT = {"name": "Розстрочка", "balance": 10_000, "monthly_rate": 0.02, "min_payment": 1_000,
               "debt_type": "installment"}
ISO_MONTH_RE = re.compile(r"\d{4}-\d{2}")


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
    """Клієнт із JWT тимчасового користувача; атрибут user_id — його id."""
    email = f"nopay-{uuid.uuid4().hex[:12]}@example.com"
    password = "test-password-123"
    user_id = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as c:
        try:
            assert (await c.post("/auth/register", json={"email": email, "password": password})).status_code == 201
            resp = await c.post("/auth/login", json={"email": email, "password": password})
            c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
            user_id = (await c.get("/auth/me")).json()["id"]
            c.user_id = user_id
            yield c
        finally:
            if user_id is None:
                async with AsyncSessionLocal() as s:
                    user_id = (await s.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
            if user_id is not None:
                await _cleanup(user_id)


async def _accept_two_versions(api, monkeypatch):
    """Перша версія 14.11, друга прийнята 20.11 (діє з 01.12)."""
    monkeypatch.setenv(ENV, "2026-11-14")
    (await api.post("/debt", json=INSTALLMENT)).json()
    plan_id = (await api.post("/debt/optimize", json={"extra_monthly_uah": 1_000})).json()["plan_id"]
    assert (await api.post(f"/debt/plans/{plan_id}/accept")).status_code == 201
    monkeypatch.setenv(ENV, "2026-11-20")
    plan_id = (await api.post("/debt/optimize", json={"extra_monthly_uah": 2_000})).json()["plan_id"]
    assert (await api.post(f"/debt/plans/{plan_id}/accept")).status_code == 201


def _mark_url(month: str) -> str:
    return f"/debt/plan-fact/months/{month}/no-payments"


async def _insert_mark(user_id: str, month: date) -> None:
    """Позначка в обхід API — як уже наявна в БД з часу, коли її дозволяли ставити на поточний місяць."""
    async with AsyncSessionLocal() as s:
        s.add(DebtPlanMonth(user_id=user_id, month=month, no_payments_confirmed=True))
        await s.commit()


async def test_mark_allowed_only_for_closed_month(api, monkeypatch):
    await _accept_two_versions(api, monkeypatch)
    monkeypatch.setenv(ENV, "2026-12-02")

    current = await api.put(_mark_url("2026-12"), json={"confirmed": True})
    assert current.status_code == 422, current.text
    assert "триває" in current.json()["detail"]
    assert not ISO_MONTH_RE.search(current.json()["detail"])

    future = await api.put(_mark_url("2027-01"), json={"confirmed": True})
    assert future.status_code == 422, future.text

    past = await api.put(_mark_url("2026-11"), json={"confirmed": True})
    assert past.status_code == 200, past.text
    assert past.json() == {"month": "2026-11", "entry_state": "no_payments_confirmed"}


async def test_materialize_409_names_unresolved_month(api, monkeypatch):
    await _accept_two_versions(api, monkeypatch)
    monkeypatch.setenv(ENV, "2026-12-02")

    resp = await api.post("/debt/plan/materialize", params={"force": "true"})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["unresolved_month"] == "2026-11"
    assert "листопад 2026 р." in body["detail"]
    assert not ISO_MONTH_RE.search(body["detail"]), body["detail"]

    # Ранній виклик без force — інша причина, без місяця дії.
    early = await api.post("/debt/plan/materialize")
    assert early.status_code == 409 and "unresolved_month" not in early.json()


async def test_materialize_succeeds_after_marking_previous_month(api, monkeypatch):
    await _accept_two_versions(api, monkeypatch)
    monkeypatch.setenv(ENV, "2026-12-02")
    assert (await api.put(_mark_url("2026-11"), json={"confirmed": True})).status_code == 200
    resp = await api.post("/debt/plan/materialize", params={"force": "true"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["materialized"] is True


async def test_existing_mark_on_open_month_does_not_count(api, monkeypatch):
    """Позначку на грудні (як у демо-БД) не зараховано: ні грудень, ні листопад не стають заповненими."""
    await _accept_two_versions(api, monkeypatch)
    monkeypatch.setenv(ENV, "2026-12-02")
    await _insert_mark(api.user_id, date(2026, 12, 1))

    months = {m["month"]: m for m in (await api.get("/debt/plan-fact")).json()["months"]}
    assert months["2026-12"]["entry_state"] == "not_entered"
    assert months["2026-12"]["no_payments_mark"] is True
    assert months["2026-11"]["no_payments_mark"] is False

    async with AsyncSessionLocal() as s:
        state = await versions_service.month_entry_state(s, api.user_id, date(2026, 12, 1), date(2026, 12, 2))
    assert state == versions_service.NOT_ENTERED

    outcome = (await api.get("/debt/plan-fact/outcome")).json()
    assert outcome["status"] == "unresolved_months" and outcome["unresolved_months"] == ["2026-11"]

    resp = await api.post("/debt/plan/materialize", params={"force": "true"})
    assert resp.status_code == 409 and resp.json()["unresolved_month"] == "2026-11"

    # Скасувати таку позначку можна.
    undo = await api.put(_mark_url("2026-12"), json={"confirmed": False})
    assert undo.status_code == 200, undo.text
    assert undo.json()["entry_state"] == "not_entered"
    months = {m["month"]: m for m in (await api.get("/debt/plan-fact")).json()["months"]}
    assert months["2026-12"]["no_payments_mark"] is False


async def test_mark_on_current_month_ignored_by_entry_state_until_closed(api, monkeypatch):
    await _accept_two_versions(api, monkeypatch)
    await _insert_mark(api.user_id, date(2026, 11, 1))
    async with AsyncSessionLocal() as s:
        open_state = await versions_service.month_entry_state(s, api.user_id, date(2026, 11, 1), date(2026, 11, 20))
        closed_state = await versions_service.month_entry_state(s, api.user_id, date(2026, 11, 1), date(2026, 12, 2))
    assert open_state == versions_service.NOT_ENTERED
    assert closed_state == versions_service.NO_PAYMENTS_CONFIRMED


async def test_locked_month_message_uses_month_name(api, monkeypatch):
    await _accept_two_versions(api, monkeypatch)
    monkeypatch.setenv(ENV, "2026-12-08")
    assert (await api.put(_mark_url("2026-11"), json={"confirmed": True})).status_code == 200
    assert (await api.post("/debt/plan/materialize")).status_code == 201
    debt_id = (await api.get("/debt")).json()[0]["id"]
    resp = await api.post("/debt/payments", json={"debt_id": debt_id, "paid_on": "2026-11-28", "amount": 100})
    assert resp.status_code == 409, resp.text
    assert "листопад 2026 р." in resp.json()["detail"].lower()
    assert not ISO_MONTH_RE.search(resp.json()["detail"])


def test_month_name_helper():
    assert ledger.month_name(date(2026, 11, 1)) == "листопад 2026 р."
