"""
Тести GET /transactions: сортування, мінімальна сума і сума знайденого.

- sort = time_desc (за замовчуванням) | amount_desc | amount_asc (за модулем
  суми); невідоме значення -> 422; за рівності — за id, сторінки стабільні;
- min_amount (цілі гривні, >= 0): |amount| >= min_amount * 100 копійок;
- X-Total-Amount: сума |amount| у копійках після фільтрів, до limit/offset;
- без цих параметрів відповідь і порядок не змінюються.

HTTP — через httpx ASGITransport на мінімальному FastAPI з роутером транзакцій.
"""
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.api.transactions as transactions_module
from app.api.deps import get_current_user, get_db
from app.models.account import Account
from app.models.transaction import Transaction


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


@pytest_asyncio.fixture
async def account(db_session, real_user):
    acc = Account(id=f"test-acc-{uuid.uuid4()}", user_id=real_user.id, source_type="monobank_api")
    db_session.add(acc)
    await db_session.flush()
    return acc


async def _txn(db_session, user, account, *, amount, time=1_772_000_000, id=None, category="pending",
               description="Опис"):
    txn = Transaction(
        id=id or str(uuid.uuid4()), user_id=user.id, account_id=account.id,
        time=time, time_precision="second", description=description,
        amount=amount, amount_uah=amount // 100,
        category=category, category_method="pending" if category == "pending" else "mcc",
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


def _ids(resp):
    return [t["id"] for t in resp.json()]


# --- 1. sort ----------------------------------------------------------------------

async def test_sort_amount_desc_by_absolute_value(db_session, real_user, account, client):
    for i, amount in enumerate([-500, 120_000, -90_000, 3_000, -1_000_000]):
        await _txn(db_session, real_user, account, amount=amount, id=f"s{i}", time=1_772_000_000 + i)

    resp = await client.get("/transactions", params={"sort": "amount_desc"})

    assert [t["amount"] for t in resp.json()] == [-1_000_000, 120_000, -90_000, 3_000, -500]


async def test_sort_amount_asc_by_absolute_value(db_session, real_user, account, client):
    for i, amount in enumerate([-500, 120_000, -90_000, 3_000, -1_000_000]):
        await _txn(db_session, real_user, account, amount=amount, id=f"s{i}", time=1_772_000_000 + i)

    resp = await client.get("/transactions", params={"sort": "amount_asc"})

    assert [t["amount"] for t in resp.json()] == [-500, 3_000, -90_000, 120_000, -1_000_000]


async def test_sort_unknown_is_422(client):
    assert (await client.get("/transactions", params={"sort": "amount"})).status_code == 422


@pytest.mark.parametrize("sort, params", [
    ("amount_desc", {"amount": -50_000, "time": 1_772_000_000}),  # однаковий |amount|
    ("time_desc", {"amount": -50_000, "time": 1_772_000_000}),    # однаковий time (імпорт з точністю до дня)
])
async def test_tie_break_by_id_gives_stable_pages(db_session, real_user, account, client, sort, params):
    # Вставка у зворотному порядку id — щоб фізичний порядок рядків не збігався з id.
    ids = [f"t{i:02d}" for i in range(7)]
    for txn_id in reversed(ids):
        await _txn(db_session, real_user, account, id=txn_id, **params)

    pages = []
    for offset in range(0, 7, 2):
        pages += _ids(await client.get("/transactions", params={"sort": sort, "limit": 2, "offset": offset}))

    assert pages == ids


# --- 2. min_amount ----------------------------------------------------------------

async def test_min_amount_filters_by_absolute_value_inclusive(db_session, real_user, account, client):
    for txn_id, amount in [("below", -9_999), ("edge_out", -10_000), ("edge_in", 10_000), ("big", 250_000),
                           ("small_in", 500)]:
        await _txn(db_session, real_user, account, id=txn_id, amount=amount)

    resp = await client.get("/transactions", params={"min_amount": 100})

    assert set(_ids(resp)) == {"edge_out", "edge_in", "big"}
    assert resp.headers.get("X-Total-Count") == "3"


async def test_min_amount_combines_with_sign_and_category(db_session, real_user, account, client):
    await _txn(db_session, real_user, account, id="pend_out_big", amount=-500_000)
    await _txn(db_session, real_user, account, id="pend_in_big", amount=500_000)
    await _txn(db_session, real_user, account, id="pend_out_small", amount=-5_000)
    await _txn(db_session, real_user, account, id="cat_out_big", amount=-500_000, category="Продукти")

    resp = await client.get("/transactions", params={"category": "pending", "sign": "out", "min_amount": 1000})

    assert _ids(resp) == ["pend_out_big"]


@pytest.mark.parametrize("value", ["-1", "1.5", "abc"])
async def test_min_amount_invalid_is_422(client, value):
    assert (await client.get("/transactions", params={"min_amount": value})).status_code == 422


# --- 3. X-Total-Amount ------------------------------------------------------------

async def test_total_amount_header_sums_abs_after_filters_before_limit(db_session, real_user, account, client):
    for amount in [-100_057, 200_000, -3_000]:
        await _txn(db_session, real_user, account, amount=amount)
    await _txn(db_session, real_user, account, amount=-999_999, category="Продукти")  # відсічено фільтром

    resp = await client.get("/transactions", params={"category": "pending", "limit": 1, "offset": 1})

    assert len(resp.json()) == 1
    assert resp.headers.get("X-Total-Count") == "3"
    assert resp.headers.get("X-Total-Amount") == str(100_057 + 200_000 + 3_000)


async def test_total_amount_header_zero_when_nothing_found(client):
    resp = await client.get("/transactions", params={"category": "немає такої"})

    assert resp.json() == []
    assert resp.headers.get("X-Total-Amount") == "0"


# --- 4. без нових параметрів ---------------------------------------------------------

async def test_defaults_unchanged(db_session, real_user, account, client):
    times = [1_772_000_300, 1_772_000_100, 1_772_000_500, 1_772_000_200]
    for i, t in enumerate(times):
        await _txn(db_session, real_user, account, id=f"r{i}", time=t, amount=-(i + 1) * 1000)

    default = await client.get("/transactions")
    explicit = await client.get("/transactions", params={"sort": "time_desc"})

    assert [t["time"] for t in default.json()] == sorted(times, reverse=True)
    assert default.json() == explicit.json()
    assert default.headers.get("X-Total-Count") == "4"
