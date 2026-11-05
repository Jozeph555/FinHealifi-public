"""
Стани прийнятого плану в GET /debt/plan/active: чинна й очікувана версії.

Сценарій: перша версія діє з 1 листопада; після неї додано борг, побудовано
нову чернетку й прийнято її як очікувану версію з 1 грудня; далі додано ще
один борг. Перевіряється structure_changed окремо для effective і pending,
preview очікуваної версії з її чернетки та comparison чинної версії, прийнятої
від залишків на дату прийняття.

«Сьогодні» задається підміною залежності get_today; HTTP — через httpx
ASGITransport на мінімальному FastAPI з роутерами боргів.
"""
from datetime import date

import httpx
import pytest_asyncio
from fastapi import FastAPI

import app.api.debt as debt_module
import app.api.debt_plan_fact as plan_fact_module
from app.api.deps import get_current_user, get_db, get_today

INSTALLMENT = {"name": "Розстрочка", "balance": 10_000, "monthly_rate": 0.02, "min_payment": 1_000,
               "debt_type": "installment"}
CARD = {"name": "Картка", "balance": 7_345.67, "monthly_rate": 0.0347, "min_payment": None, "debt_type": "revolving"}


@pytest_asyncio.fixture
async def clock():
    return {"today": date(2026, 11, 14)}


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


async def _debt(client, **body):
    resp = await client.post("/debt", json={**INSTALLMENT, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _draft(client, extra):
    resp = await client.post("/debt/optimize", json={"extra_monthly_uah": extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _accept(client, plan_id):
    resp = await client.post(f"/debt/plans/{plan_id}/accept")
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _active(client):
    resp = await client.get("/debt/plan/active")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_effective_and_pending_versions_report_structure_preview_and_comparison(client, clock):
    await _debt(client)
    draft1 = await _draft(client, 1_000)
    await _accept(client, draft1["plan_id"])

    # 1. Нічого не змінилося: чинна версія збігається зі складом, очікуваної немає.
    body = await _active(client)
    assert body["pending"] is None
    assert body["effective"]["structure_changed"] is False
    assert body["effective"]["preview"] is None
    assert body["effective"]["comparison"] == draft1["comparison"]
    assert body["effective"]["comparison_based_on"] == "balances_at_acceptance"

    # 2. Борг додано після першої версії; нову чернетку прийнято як очікувану з 1 грудня.
    clock["today"] = date(2026, 11, 20)
    await _debt(client, **CARD)
    draft2 = await _draft(client, 1_500)
    await _accept(client, draft2["plan_id"])
    body = await _active(client)
    effective, pending = body["effective"], body["pending"]
    assert effective["start_kind"] == "acceptance_balances" and pending["start_kind"] == "month_start_actual"
    assert effective["structure_changed"] is True
    assert pending["structure_changed"] is False
    assert pending["materialized"] is False
    assert pending["preview"] == {
        "months_to_payoff": draft2["months_to_payoff"],
        "total_interest": draft2["total_interest_paid"],
        "horizon_months": None,
        "based_on": "balances_at_build",
    }
    assert pending["comparison"] is None
    assert effective["preview"] is None
    assert effective["comparison"] == draft1["comparison"]

    # 3. Ще один борг після прийняття очікуваної версії: розходяться обидві.
    clock["today"] = date(2026, 11, 25)
    await _debt(client, name="Ще один борг", balance=2_000, min_payment=200)
    body = await _active(client)
    assert body["effective"]["structure_changed"] is True
    assert body["pending"]["structure_changed"] is True


async def test_month_start_version_has_no_comparison_after_materialize(client, clock):
    debt = await _debt(client)
    await _accept(client, (await _draft(client, 1_000))["plan_id"])
    resp = await client.post("/debt/payments", json={"debt_id": debt["id"], "paid_on": "2026-11-14", "amount": 1_500})
    assert resp.status_code == 201, resp.text
    clock["today"] = date(2026, 11, 20)
    await _accept(client, (await _draft(client, 2_000))["plan_id"])

    clock["today"] = date(2026, 12, 7)
    assert (await client.post("/debt/plan/materialize")).status_code == 201
    effective = (await _active(client))["effective"]
    assert effective["start_kind"] == "month_start_actual" and effective["materialized"] is True
    assert effective["comparison"] is None and effective["comparison_based_on"] is None
    assert effective["preview"] is None
