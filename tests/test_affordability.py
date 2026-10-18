"""
Тести плану з додатковим платежем і калькулятора спроможності.

A) POST /debt/optimize {extra_monthly_uah}: бюджет = сума мінімальних платежів
   + extra; план зберігається з forecast_id = NULL.
B) GET /debt/affordability?extra=&months=: лише читання; історичний місячний
   FCF за останні N повних місяців, частка місяців з FCF >= extra,
   min/max/медіана/P25/P75.
C) POST /debt/optimize-from-forecast працює як раніше.
D) GET /debt/plan/latest повертає план без прогнозу.

HTTP — через httpx ASGITransport на мінімальному FastAPI з роутером боргів.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI
from sqlalchemy import func, select

import app.api.debt as debt_module
from app.api.deps import get_current_user, get_db
from app.models.account import Account
from app.models.debt import Debt
from app.models.forecast import Forecast
from app.models.milp_plan import MilpPlan
from app.models.transaction import Transaction
from app.models.user import User

DISCLAIMER = "Це історичні дані, а не прогноз і не гарантія"


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


async def _other_user(db_session):
    user = User(id=f"test-{uuid.uuid4()}", email=f"other-{uuid.uuid4()}@example.com", password_hash="x")
    db_session.add(user)
    await db_session.flush()
    return user


async def _debt(db_session, user, *, name, balance, rate, min_payment=None, debt_type="installment"):
    debt = Debt(id=str(uuid.uuid4()), user_id=user.id, name=name, balance=balance, monthly_rate=rate,
                min_payment=min_payment, debt_type=debt_type, is_active=True)
    db_session.add(debt)
    await db_session.flush()
    return debt


async def _counts(db_session):
    return tuple([
        (await db_session.execute(select(func.count()).select_from(model))).scalar_one()
        for model in (Forecast, MilpPlan)
    ])


# --- A) POST /debt/optimize -----------------------------------------------------------

async def test_optimize_extra_zero_is_minimum_only_plan(db_session, real_user, client):
    await _debt(db_session, real_user, name="Розстрочка", balance=10_000, rate=0.02, min_payment=1_000)

    resp = await client.post("/debt/optimize", json={"extra_monthly_uah": 0})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["forecast_id"] is None
    assert (body["extra_monthly_uah"], body["min_payments_total_uah"], body["total_monthly_payment_uah"]) == (
        0, 1_000, 1_000)
    assert body["status"] == "Optimal"
    plan = (await db_session.execute(select(MilpPlan).where(MilpPlan.id == body["plan_id"]))).scalar_one()
    assert plan.forecast_id is None and plan.user_id == real_user.id


async def test_optimize_budget_is_minimums_plus_extra(db_session, real_user, client):
    await _debt(db_session, real_user, name="Розстрочка", balance=20_000, rate=0.02, min_payment=1_000)
    await _debt(db_session, real_user, name="Картка", balance=50_000, rate=0.03, debt_type="revolving")

    resp = await client.post("/debt/optimize", json={"extra_monthly_uah": 3_000})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    # revolving: max(4 % балансу, 200) = 2 000
    assert body["min_payments_total_uah"] == 3_000
    assert body["total_monthly_payment_uah"] == 6_000
    first = body["monthly_schedule"][0]["payments"]
    assert sum(first.values()) <= 6_000 + 0.01


@pytest.mark.parametrize("payload", [
    {"extra_monthly_uah": -1},
    {"extra_monthly_uah": "багато"},
    {"extra_monthly_uah": 10_000_001},
    {},
])
async def test_optimize_invalid_extra_is_422(db_session, real_user, client, payload):
    await _debt(db_session, real_user, name="Розстрочка", balance=10_000, rate=0.02, min_payment=1_000)

    resp = await client.post("/debt/optimize", json=payload)

    assert resp.status_code == 422
    # Помилка валідації поля, а не 422 від солвера (бюджет, менший за мінімальні
    # платежі, теж дає 422).
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and any("extra_monthly_uah" in e["loc"] for e in detail)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
async def test_optimize_non_finite_extra_is_422(db_session, real_user, client, raw):
    await _debt(db_session, real_user, name="Розстрочка", balance=10_000, rate=0.02, min_payment=1_000)

    resp = await client.post("/debt/optimize", content='{"extra_monthly_uah": %s}' % raw,
                             headers={"content-type": "application/json"})

    assert resp.status_code == 422
    assert any("extra_monthly_uah" in e["loc"] for e in resp.json()["detail"])


async def test_optimize_without_active_debts_is_clear_409(db_session, real_user, client):
    resp = await client.post("/debt/optimize", json={"extra_monthly_uah": 1_000})

    assert resp.status_code == 409
    assert "борг" in resp.json()["detail"].lower()


async def test_optimize_infeasible_is_422_with_hint_and_no_plan(db_session, real_user, client):
    # Ставка 10 %/міс > мінімуму 4 %: баланс росте, мінімальний платіж теж,
    # фіксованого бюджету (Σ мінімумів + 0) не вистачає вже в 2-му місяці.
    await _debt(db_session, real_user, name="Дорога картка", balance=50_000, rate=0.10, debt_type="revolving")
    before = await _counts(db_session)

    resp = await client.post("/debt/optimize", json={"extra_monthly_uah": 0})

    assert resp.status_code == 422
    assert "додатков" in resp.json()["detail"].lower()
    assert await _counts(db_session) == before


async def test_optimize_solver_error_is_422_not_500(db_session, real_user, client, monkeypatch):
    await _debt(db_session, real_user, name="Розстрочка", balance=10_000, rate=0.02, min_payment=1_000)

    def _boom(*args, **kwargs):
        raise RuntimeError("CBC exploded")

    monkeypatch.setattr(debt_module, "optimize_debt_repayment", _boom)

    resp = await client.post("/debt/optimize", json={"extra_monthly_uah": 500})

    assert resp.status_code == 422
    assert "CBC exploded" in resp.json()["detail"]


async def test_optimize_ignores_other_users_debts(db_session, real_user, client):
    other = await _other_user(db_session)
    await _debt(db_session, other, name="Чужий борг", balance=999_999, rate=0.05, min_payment=50_000)
    await _debt(db_session, real_user, name="Мій борг", balance=10_000, rate=0.02, min_payment=1_000)

    body = (await client.post("/debt/optimize", json={"extra_monthly_uah": 0})).json()

    assert body["min_payments_total_uah"] == 1_000
    assert set(body["monthly_schedule"][0]["payments"]) == {"Мій борг"}


# --- D) GET /debt/plan/latest із планом без прогнозу ------------------------------------

async def test_plan_latest_works_with_null_forecast(db_session, real_user, client):
    await _debt(db_session, real_user, name="Розстрочка", balance=10_000, rate=0.02, min_payment=1_000)
    created = (await client.post("/debt/optimize", json={"extra_monthly_uah": 2_000})).json()

    resp = await client.get("/debt/plan/latest")

    assert resp.status_code == 200
    assert resp.json()["plan_id"] == created["plan_id"]
    assert resp.json()["forecast_id"] is None


# --- C) optimize-from-forecast ---------------------------------------------------------

async def test_optimize_from_forecast_unchanged(db_session, real_user, client):
    await _debt(db_session, real_user, name="Розстрочка", balance=10_000, rate=0.02, min_payment=1_000)
    forecast = Forecast(id=str(uuid.uuid4()), user_id=real_user.id, period_start=date(2026, 11, 1),
                        period_end=date(2026, 11, 30), predicted_income=60_000, predicted_expenses=55_000,
                        free_cash_flow=5_000, method="average_fallback", low_confidence=True)
    db_session.add(forecast)
    await db_session.flush()

    resp = await client.post("/debt/optimize-from-forecast")

    assert resp.status_code == 200, resp.text
    assert resp.json()["forecast_id"] == forecast.id
    assert resp.json()["monthly_budget_used"] == 5_000


# --- B) GET /debt/affordability -----------------------------------------------------------------

def _month_start(k: int) -> date:
    """Повертає перше число місяця, що на k місяців раніше за поточний."""
    return date.today().replace(day=1) - relativedelta(months=k)


async def _seed_months(db_session, user, ks, *, income_of, expense_of):
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=user.id, source_type="manual")
    db_session.add(account)
    await db_session.flush()
    for k in ks:
        ts = int(datetime.combine(_month_start(k) + timedelta(days=9), datetime.min.time(),
                                  tzinfo=timezone.utc).timestamp()) + 12 * 3600
        for amount, category in ((income_of(k), "Заробітна плата"), (-expense_of(k), "Продукти")):
            db_session.add(Transaction(
                id=str(uuid.uuid4()), user_id=user.id, account_id=account.id, time=ts, time_precision="date",
                description="Опис", amount=amount * 100, amount_uah=amount, category=category,
                category_method="manual",
            ))
    await db_session.flush()


async def test_affordability_summary_over_last_full_months(db_session, real_user, client):
    # k = 1..14 повних місяців тому; перший місяць блоку (k=14) вікно відкидає як неповний.
    # FCF(k) = (50 000 + 1 000 * (13 - k)) - 45 000 -> останні 12 (k = 1..12): 6 000 .. 17 000.
    await _seed_months(db_session, real_user, range(1, 15),
                       income_of=lambda k: 50_000 + 1_000 * (13 - k), expense_of=lambda k: 45_000)
    before = await _counts(db_session)

    resp = await client.get("/debt/affordability", params={"extra": 10_000})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["n_months_requested"] == 12 and body["n_months_used"] == 12
    assert [m["month"] for m in body["months"]] == [f"{_month_start(k):%Y-%m}" for k in range(12, 0, -1)]
    assert sorted(m["fcf_uah"] for m in body["months"]) == [float(v) for v in range(6_000, 17_001, 1_000)]
    assert body["count_ge_extra"] == 8 and body["share"] == pytest.approx(8 / 12)
    assert (body["min"], body["max"], body["median"]) == (6_000, 17_000, 11_500)
    assert body["p25"] == pytest.approx(8_750) and body["p75"] == pytest.approx(14_250)
    assert body["historical_not_forecast"] is True
    assert body["disclaimer"] == DISCLAIMER
    assert "recommended" not in str(sorted(body)) and "forecast" not in body
    assert await _counts(db_session) == before  # лише читання


async def test_affordability_drops_current_partial_month(db_session, real_user, client):
    await _seed_months(db_session, real_user, range(0, 6),
                       income_of=lambda k: 900_000 if k == 0 else 60_000, expense_of=lambda k: 50_000)

    body = (await client.get("/debt/affordability", params={"extra": 0, "months": 3})).json()

    current = f"{date.today():%Y-%m}"
    assert current not in [m["month"] for m in body["months"]]
    assert [m["month"] for m in body["months"]] == [f"{_month_start(k):%Y-%m}" for k in (3, 2, 1)]
    assert body["max"] == 10_000


async def test_affordability_short_history_is_200_with_explanation(db_session, real_user, client):
    # k = 1..3 -> після відкидання першого місяця блоку лишається 2 повних.
    await _seed_months(db_session, real_user, range(1, 4), income_of=lambda k: 60_000, expense_of=lambda k: 50_000)

    resp = await client.get("/debt/affordability", params={"extra": 5_000})

    assert resp.status_code == 200
    body = resp.json()
    assert body["n_months_used"] == 2
    assert body["count_ge_extra"] is None and body["share"] is None and body["median"] is None
    assert body["explanation"]


async def test_affordability_ignores_other_users(db_session, real_user, client):
    other = await _other_user(db_session)
    await _seed_months(db_session, other, range(1, 8), income_of=lambda k: 999_000, expense_of=lambda k: 1)
    await _seed_months(db_session, real_user, range(1, 8), income_of=lambda k: 60_000, expense_of=lambda k: 50_000)

    body = (await client.get("/debt/affordability", params={"extra": 0, "months": 3})).json()

    assert {m["fcf_uah"] for m in body["months"]} == {10_000.0}


@pytest.mark.parametrize("params", [{"extra": -1}, {"extra": "abc"}, {}, {"extra": 0, "months": 2},
                                    {"extra": 0, "months": 25}, {"extra": "nan"}])
async def test_affordability_invalid_params_is_422(client, params):
    assert (await client.get("/debt/affordability", params=params)).status_code == 422
