"""
Тести цілісності збереженого плану боргів.

- Відбиток складу активних боргів (app/services/debt_fingerprint.py)
  детермінований, не залежить від порядку і змінюється лише від полів, які
  використовує MILP.
- POST /debt/optimize зберігає extra_monthly_uah і debts_fingerprint.
- GET /debt/plan/latest повертає extra_monthly_uah (null для планів без нього)
  і is_stale — true/false, якщо відбиток збережено, null — якщо ні.
- 404 не згадує optimize-from-forecast.

HTTP — через httpx ASGITransport на мінімальному FastAPI з роутером боргів.
"""
import uuid
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

import app.api.debt as debt_module
from app.api.deps import get_current_user, get_db
from app.models.forecast import Forecast
from app.models.milp_plan import MilpPlan


@pytest_asyncio.fixture
async def client(db_session, real_user):
    app = FastAPI()
    app.include_router(debt_module.router)

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: real_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _debt(id, *, name="Борг", debt_type="installment", balance=10_000.0, monthly_rate=0.02, min_payment=1_000.0,
          **extra):
    return SimpleNamespace(id=id, name=name, debt_type=debt_type, balance=balance, monthly_rate=monthly_rate,
                           min_payment=min_payment, **extra)


# --- відбиток (чиста функція) -------------------------------------------------------

def test_fingerprint_is_deterministic_and_order_independent():
    from app.services.debt_fingerprint import debts_fingerprint

    a, b = _debt("a", name="Картка"), _debt("b", name="Розстрочка", balance=5_000.0)

    first = debts_fingerprint([a, b])
    assert first == debts_fingerprint([a, b]) == debts_fingerprint([b, a])
    assert len(first) == 64 and int(first, 16) >= 0  # sha256 hex
    # int і float з тим самим значенням дають той самий відбиток
    assert debts_fingerprint([_debt("a", balance=10_000)]) == debts_fingerprint([_debt("a", balance=10_000.0)])


@pytest.mark.parametrize("change", [
    {"balance": 9_999.0},
    {"monthly_rate": 0.021},
    {"min_payment": 1_100.0},
    {"min_payment": None},
    {"debt_type": "revolving"},
])
def test_fingerprint_changes_with_plan_fields(change):
    from app.services.debt_fingerprint import debts_fingerprint

    base = [_debt("a"), _debt("b", name="Друга")]
    changed = [SimpleNamespace(**{**vars(_debt("a")), **change}), _debt("b", name="Друга")]

    assert debts_fingerprint(base) != debts_fingerprint(changed)


def test_fingerprint_changes_when_debt_added_or_removed():
    from app.services.debt_fingerprint import debts_fingerprint

    one, two = [_debt("a")], [_debt("a"), _debt("b")]

    assert debts_fingerprint(one) != debts_fingerprint(two)
    assert debts_fingerprint([]) != debts_fingerprint(one)


def test_fingerprint_ignores_fields_mip_does_not_use():
    from app.services.debt_fingerprint import debts_fingerprint

    plain = [_debt("a")]
    noisy = [_debt("a", created_at="2026-01-01", user_id="u-1", is_active=True)]
    renamed = [_debt("a", name="Інша назва")]

    assert debts_fingerprint(plain) == debts_fingerprint(noisy)
    # назва не впливає на план (MILP рахує за сумами, ставкою й мінімумом)
    assert debts_fingerprint(plain) == debts_fingerprint(renamed)


# --- POST /debt/optimize зберігає extra і відбиток ----------------------------------------

async def _create_debt(client, **body):
    payload = {"name": "Розстрочка", "balance": 10_000, "monthly_rate": 0.02, "min_payment": 1_000,
               "debt_type": "installment", **body}
    resp = await client.post("/debt", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_optimize_saves_extra_and_fingerprint(db_session, client):
    from app.services.debt_fingerprint import debts_fingerprint

    debt = await _create_debt(client)
    body = (await client.post("/debt/optimize", json={"extra_monthly_uah": 2_500.5})).json()

    plan = (await db_session.execute(select(MilpPlan).where(MilpPlan.id == body["plan_id"]))).scalar_one()
    assert plan.extra_monthly_uah == 2_500.5
    assert plan.debts_fingerprint == debts_fingerprint([SimpleNamespace(**{
        "id": debt["id"], "name": "Розстрочка", "debt_type": "installment", "balance": 10_000.0,
        "monthly_rate": 0.02, "min_payment": 1_000.0,
    })])


# --- GET /debt/plan/latest ----------------------------------------------------------------

async def test_latest_fresh_plan_is_not_stale_and_has_extra(client):
    await _create_debt(client)
    await client.post("/debt/optimize", json={"extra_monthly_uah": 3_000})

    body = (await client.get("/debt/plan/latest")).json()

    assert body["extra_monthly_uah"] == 3_000
    assert body["is_stale"] is False


@pytest.mark.parametrize("change", ["patch_balance", "add_debt", "deactivate"])
async def test_latest_becomes_stale_after_debts_change(client, change):
    debt = await _create_debt(client)
    await client.post("/debt/optimize", json={"extra_monthly_uah": 1_000})

    if change == "patch_balance":
        await client.patch(f"/debt/{debt['id']}", json={"balance": 8_000})
    elif change == "add_debt":
        await _create_debt(client, name="Новий борг")
    else:
        await client.patch(f"/debt/{debt['id']}", json={"is_active": False})

    body = (await client.get("/debt/plan/latest")).json()
    assert body["is_stale"] is True


async def test_latest_not_stale_after_inactive_debt_edit(client):
    await _create_debt(client)
    old = await _create_debt(client, name="Погашений")
    await client.patch(f"/debt/{old['id']}", json={"is_active": False})
    await client.post("/debt/optimize", json={"extra_monthly_uah": 1_000})

    await client.patch(f"/debt/{old['id']}", json={"balance": 1, "name": "Перейменований погашений"})

    assert (await client.get("/debt/plan/latest")).json()["is_stale"] is False


async def test_latest_not_stale_after_rename(client):
    debt = await _create_debt(client)
    await client.post("/debt/optimize", json={"extra_monthly_uah": 1_000})

    assert (await client.patch(f"/debt/{debt['id']}", json={"name": "Нова назва"})).status_code == 200

    assert (await client.get("/debt/plan/latest")).json()["is_stale"] is False


async def test_latest_old_plan_without_fingerprint_is_unknown(db_session, real_user, client):
    await _create_debt(client)
    db_session.add(MilpPlan(id=str(uuid.uuid4()), user_id=real_user.id, months_to_payoff=10,
                            total_interest_paid=100.0, plan_data={}, status="active"))
    await db_session.flush()

    body = (await client.get("/debt/plan/latest")).json()

    assert body["extra_monthly_uah"] is None
    assert body["is_stale"] is None
    for key in ("plan_id", "forecast_id", "generated_at", "months_to_payoff", "total_interest_paid", "status",
                "comparison", "monthly_schedule"):
        assert key in body


async def test_latest_after_optimize_from_forecast_is_unknown_and_does_not_crash(db_session, real_user, client):
    await _create_debt(client)
    db_session.add(Forecast(id=str(uuid.uuid4()), user_id=real_user.id, period_start=date(2026, 11, 1),
                            period_end=date(2026, 11, 30), predicted_income=60_000, predicted_expenses=55_000,
                            free_cash_flow=5_000, method="average_fallback", low_confidence=True))
    await db_session.flush()

    assert (await client.post("/debt/optimize-from-forecast")).status_code == 200
    body = (await client.get("/debt/plan/latest")).json()

    assert body["is_stale"] is None and body["extra_monthly_uah"] is None


async def test_latest_404_does_not_mention_deprecated_endpoint(client):
    resp = await client.get("/debt/plan/latest")

    assert resp.status_code == 404
    assert "optimize-from-forecast" not in resp.json()["detail"]
    assert "Борги" in resp.json()["detail"]
