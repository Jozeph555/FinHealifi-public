"""
Тести власних категорій витрат користувача.

- POST /transactions/categories/custom: прибирання пробілів, довжина 2..40,
  без керуючих символів (422); збіг без урахування регістру з вбудованою
  (витрати, дохід, перекази, службові, «Інше») чи власною категорією того ж
  користувача -> 409; не більше 50 на користувача (422).
- Власна категорія — звичайна витрата: PATCH і POST manual з від'ємною сумою
  проходять, з додатною — 422; категорія іншого користувача чи неіснуюча — 422.
- GET /transactions/categories: власні категорії в categories, групі expense і
  custom_categories; базові ключі на місці.
- Прогноз рахує її витратою; ML вчить як звичайний клас (з порогом прикладів).

HTTP — через httpx ASGITransport на мінімальному FastAPI з роутером транзакцій.
"""
import uuid
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

import app.api.transactions as transactions_module
from app.api.deps import get_current_user, get_db
from app.models.account import Account
from app.models.category_training_example import CategoryTrainingExample
from app.models.transaction import Transaction
from app.models.user import User
from app.services.forecasting import _load_expense_breakdown, _load_monthly_series
from app.services.ml_categorizer import MIN_EXAMPLES_PER_CLASS, _drop_rare_classes, is_ml_assignable

CUSTOM = "Для дому"
MONTH_TS = int(datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc).timestamp())


@pytest_asyncio.fixture
async def client(db_session, real_user):
    app = FastAPI()
    app.include_router(transactions_module.router)

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: real_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _create(client, name):
    return await client.post("/transactions/categories/custom", json={"name": name})


async def _other_user(db_session):
    user = User(id=f"test-{uuid.uuid4()}", email=f"other-{uuid.uuid4()}@example.com", password_hash="x")
    db_session.add(user)
    await db_session.flush()
    return user


async def _txn(db_session, user, *, amount, category="pending", method="pending"):
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=user.id, source_type="manual")
    db_session.add(account)
    await db_session.flush()
    txn = Transaction(
        id=str(uuid.uuid4()), user_id=user.id, account_id=account.id,
        time=MONTH_TS, time_precision="date", description="Опис для пулу",
        amount=amount, amount_uah=amount // 100, category=category, category_method=method,
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


# --- (а) створення і валідація назви ----------------------------------------------

async def test_create_trims_and_returns_name(client):
    resp = await _create(client, f"   {CUSTOM}  ")

    assert resp.status_code == 201
    assert resp.json()["name"] == CUSTOM


@pytest.mark.parametrize("name, status", [("Я", 422), ("Ок", 201), ("x" * 40, 201), ("x" * 41, 422),
                                          ("   ", 422)])
async def test_create_length_limits(client, name, status):
    assert (await _create(client, name)).status_code == status


@pytest.mark.parametrize("name", ["Для\tдому", "Для\nдому", "Для\x00дому", "Ремонт\x7fавто"])
async def test_create_control_characters_rejected(client, name):
    assert (await _create(client, name)).status_code == 422


# --- (б) конфлікти -------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "продукти",            # витрати
    "ЗАРОБІТНА ПЛАТА",     # дохід
    "заощадження",         # переказ
    "Отримана Позика",     # переказ (системний)
    "PENDING",             # службова
    "повернення позики",   # службова (repay-loan)
    "інше",                # «Інше» (ціль mark-reimbursement)
])
async def test_create_conflicts_with_builtin_case_insensitive(client, name):
    assert (await _create(client, name)).status_code == 409


async def test_create_conflicts_with_own_custom_case_insensitive(client):
    assert (await _create(client, CUSTOM)).status_code == 201
    assert (await _create(client, "  дЛЯ ДОМУ ")).status_code == 409


# --- (в) інші користувачі --------------------------------------------------------------

async def test_other_user_same_name_allowed_and_isolated(db_session, real_user, client):
    other = await _other_user(db_session)
    db_session.add(transactions_module.UserCategory(id=str(uuid.uuid4()), user_id=other.id, name=CUSTOM))
    await db_session.flush()

    # та сама назва — дозволена мені
    assert (await _create(client, CUSTOM)).status_code == 201
    # чужа (інша назва) — невидима й недоступна
    db_session.add(transactions_module.UserCategory(id=str(uuid.uuid4()), user_id=other.id, name="Ремонт авто"))
    await db_session.flush()
    body = (await client.get("/transactions/categories")).json()
    assert "Ремонт авто" not in body["custom_categories"] and "Ремонт авто" not in body["categories"]

    txn = await _txn(db_session, real_user, amount=-50_000)
    resp = await client.patch(f"/transactions/{txn.id}", json={"category": "Ремонт авто"})
    assert resp.status_code == 422
    assert (txn.category, txn.category_method) == ("pending", "pending")


# --- (г) PATCH ------------------------------------------------------------------------------

async def test_patch_negative_to_custom_ok_and_feeds_training_pool(db_session, real_user, client):
    await _create(client, CUSTOM)
    txn = await _txn(db_session, real_user, amount=-50_000)

    resp = await client.patch(f"/transactions/{txn.id}", json={"category": CUSTOM})

    assert resp.status_code == 200
    assert (resp.json()["category"], resp.json()["category_method"]) == (CUSTOM, "manual")
    [example] = (await db_session.execute(
        select(CategoryTrainingExample).where(CategoryTrainingExample.source_transaction_id == txn.id)
    )).scalars().all()
    assert (example.category, example.source) == (CUSTOM, "manual_confirmed")


async def test_patch_positive_to_custom_is_sign_422(db_session, real_user, client):
    await _create(client, CUSTOM)
    txn = await _txn(db_session, real_user, amount=50_000)

    resp = await client.patch(f"/transactions/{txn.id}", json={"category": CUSTOM})

    assert resp.status_code == 422
    assert "Додатна сума" in resp.json()["detail"]


async def test_patch_unknown_category_is_422(db_session, real_user, client):
    txn = await _txn(db_session, real_user, amount=-50_000)

    assert (await client.patch(f"/transactions/{txn.id}", json={"category": CUSTOM})).status_code == 422


# --- (д) POST /transactions/manual ------------------------------------------------------------

@pytest.mark.parametrize("amount, status", [(-120.5, 201), (120.5, 422)])
async def test_manual_create_with_custom(client, amount, status):
    await _create(client, CUSTOM)

    resp = await client.post("/transactions/manual", json={
        "date": "2026-03-01T12:00:00", "description": "Гачки й полиці", "amount": amount, "category": CUSTOM,
    })

    assert resp.status_code == status
    if status == 201:
        assert (resp.json()["category"], resp.json()["category_method"]) == (CUSTOM, "manual")


# --- (е) GET /transactions/categories ---------------------------------------------------------

async def test_categories_include_custom(client):
    before = (await client.get("/transactions/categories")).json()
    await _create(client, CUSTOM)

    body = (await client.get("/transactions/categories")).json()

    assert body["custom_categories"] == [CUSTOM]
    assert CUSTOM in body["expense"] and CUSTOM in body["categories"]
    assert CUSTOM not in body["income"] + body["transfer"] + body["service"] + body["non_spending_categories"]
    for key in ("categories", "income_categories", "non_spending_categories", "expense", "income", "transfer",
                "service"):
        assert key in body
    assert [c for c in body["categories"] if c != CUSTOM] == before["categories"]
    assert sorted(body["expense"] + body["income"] + body["transfer"]) == sorted(body["categories"])


# --- (є) прогноз ----------------------------------------------------------------------------------

async def test_forecast_counts_custom_as_expense(db_session, real_user, client):
    await _create(client, CUSTOM)
    await _txn(db_session, real_user, amount=-10_000, category="Продукти", method="manual")
    await _txn(db_session, real_user, amount=-35_000, category=CUSTOM, method="manual")

    breakdown = await _load_expense_breakdown(real_user.id, session=db_session)
    [month] = breakdown["months"]
    income = await _load_monthly_series(real_user.id, income=True, session=db_session)

    assert month["categorized"] == 450.0 and month["total"] == 450.0
    assert (month["pending_merchant"], month["pending_inflow"]) == (0.0, 0.0)
    assert breakdown["by_category"] == {CUSTOM: 350.0, "Продукти": 100.0}
    assert income.empty


# --- (ж) ліміт 50 ---------------------------------------------------------------------------------

async def test_limit_50_per_user(db_session, real_user, client):
    for i in range(50):
        db_session.add(transactions_module.UserCategory(id=str(uuid.uuid4()), user_id=real_user.id,
                                                        name=f"Моя категорія {i}"))
    await db_session.flush()

    resp = await _create(client, "П'ятдесят перша")

    assert resp.status_code == 422
    assert "50" in resp.json()["detail"]


# --- (з) ML -----------------------------------------------------------------------------------------

def test_ml_custom_category_is_ordinary_class_with_threshold():
    assert is_ml_assignable(CUSTOM) is True
    texts = [f"t{i}" for i in range(10)]
    labels = ["Продукти"] * 5 + [CUSTOM] * MIN_EXAMPLES_PER_CLASS + ["Ремонт авто"] * 2
    owners = [None] * 10

    with pytest.warns(UserWarning, match="Ремонт авто"):
        _, kept, _ = _drop_rare_classes(texts, labels, owners)

    assert kept.count(CUSTOM) == MIN_EXAMPLES_PER_CLASS
    assert "Ремонт авто" not in kept
