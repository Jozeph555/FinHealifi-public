"""
Тести ручної зміни категорій і списку транзакцій.

- PATCH /transactions/{id} і POST /transactions/manual перевіряють пару «знак
  суми + категорія» (validate_manual_category_sign): від'ємна сума не може
  бути доходом, додатна — витратою; перекази дозволені для обох знаків.
  Порушення — 422 з поясненням.
- PATCH рядка з loan_id чи category_method='reimbursement' — 409.
- Відхилений PATCH нічого не пише в category_training_examples.
- GET /transactions: offset, limit (<= 1000), category (зокрема "pending"),
  sign=in|out, from/to (дати за Києвом, межі включно), q (ILIKE по опису);
  X-Total-Count — кількість після фільтрів, до limit/offset.
- TransactionResponse містить loan_id, account_id, mcc, amount (копійки).
- GET /transactions/categories містить групи expense/income/transfer/service.

PATCH і POST викликаються напряму; GET — через httpx ASGITransport на
мінімальному FastAPI з роутером транзакцій і підміненими get_db та
get_current_user.
"""
import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select

import app.api.transactions as transactions_module
from app.api.deps import get_current_user, get_db
from app.api.loans import LoanCreateRequest, create_loan
from app.api.transactions import (
    MANUAL_ENTRY_CATEGORIES,
    ManualTransactionRequest,
    MarkReimbursementRequest,
    RepayLoanRequest,
    UpdateCategoryRequest,
    create_manual_transaction,
    list_manual_categories,
    mark_reimbursement,
    repay_loan,
    update_transaction_category,
)
from app.models.account import Account
from app.models.category_training_example import CategoryTrainingExample
from app.models.transaction import Transaction
from app.models.user import User
from app.services.mcc_mapping import INCOME_CATEGORIES, SERVICE_CATEGORIES, TRANSFER_CATEGORIES

KYIV = ZoneInfo("Europe/Kyiv")


# --- допоміжне ----------------------------------------------------------------

async def _account(db_session, user) -> Account:
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=user.id, source_type="monobank_api")
    db_session.add(account)
    await db_session.flush()
    return account


async def _txn(db_session, user, account, *, amount, description="Опис", category="pending",
               category_method="pending", time=None, mcc=None) -> Transaction:
    txn = Transaction(
        id=str(uuid.uuid4()), user_id=user.id, account_id=account.id,
        time=time if time is not None else 1_772_000_000, time_precision="second",
        description=description, mcc=mcc,
        amount=amount, amount_uah=amount // 100,
        category=category, category_method=category_method,
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


async def _pool_rows(db_session, txn_id) -> list[CategoryTrainingExample]:
    return (await db_session.execute(
        select(CategoryTrainingExample).where(CategoryTrainingExample.source_transaction_id == txn_id)
    )).scalars().all()


async def _patch(db_session, user, txn_id, category):
    return await update_transaction_category(
        txn_id, UpdateCategoryRequest(category=category), current_user=user, db=db_session,
    )


def _kyiv_ts(*args) -> int:
    return int(datetime(*args, tzinfo=KYIV).timestamp())


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


# --- (а) від'ємна сума -> дохід: 422 ------------------------------------------

@pytest.mark.parametrize("category", ["Заробітна плата", "Інший дохід", "Подарунок"])
async def test_patch_negative_amount_to_income_is_422(db_session, real_user, category):
    account = await _account(db_session, real_user)
    txn = await _txn(db_session, real_user, account, amount=-150_000)

    with pytest.raises(HTTPException) as exc:
        await _patch(db_session, real_user, txn.id, category)

    assert exc.value.status_code == 422
    assert (txn.category, txn.category_method) == ("pending", "pending")


# --- (б) додатна сума -> витрата: 422 зі згадкою reimbursement -----------------

@pytest.mark.parametrize("category", ["Продукти", "Житло та комунальні", "Борги та кредити", "Готівка"])
async def test_patch_positive_amount_to_expense_is_422_with_reimbursement_hint(db_session, real_user, category):
    account = await _account(db_session, real_user)
    txn = await _txn(db_session, real_user, account, amount=1_000_000)

    with pytest.raises(HTTPException) as exc:
        await _patch(db_session, real_user, txn.id, category)

    assert exc.value.status_code == 422
    detail = str(exc.value.detail)
    assert "повернення витрати" in detail.lower()
    assert "mark-reimbursement" in detail
    assert (txn.category, txn.category_method) == ("pending", "pending")


# --- (в) дозволені пари проходять --------------------------------------------

@pytest.mark.parametrize("amount, category", [
    (1_000_000, "Заощадження"),
    (-1_000_000, "Заощадження"),
    (1_000_000, "Інвестиції"),
    (-1_000_000, "Інвестиції"),
    (1_000_000, "Заробітна плата"),
    (-150_000, "Продукти"),
    (-150_000, "Борги та кредити"),
])
async def test_patch_allowed_sign_category_pairs(db_session, real_user, amount, category):
    account = await _account(db_session, real_user)
    txn = await _txn(db_session, real_user, account, amount=amount)

    saved = await _patch(db_session, real_user, txn.id, category)

    assert (saved.category, saved.category_method) == (category, "manual")
    [row] = await _pool_rows(db_session, txn.id)
    assert (row.category, row.source) == (category, "manual_confirmed")


# --- (г) службові рядки: 409, нічого не змінено --------------------------------

async def _loan_row(db_session, real_user):
    loan = await create_loan(
        LoanCreateRequest(borrower_name="Іван П.", principal_amount=5000, issued_at=date(2026, 7, 1)),
        current_user=real_user, db=db_session,
    )
    account = await _account(db_session, real_user)
    txn = await _txn(db_session, real_user, account, amount=100_000)
    return await repay_loan(txn.id, RepayLoanRequest(loan_id=loan.id), current_user=real_user, db=db_session)


async def _reimbursement_row(db_session, real_user):
    account = await _account(db_session, real_user)
    txn = await _txn(db_session, real_user, account, amount=25_000)
    return await mark_reimbursement(
        txn.id, MarkReimbursementRequest(category="Кафе та ресторани"), current_user=real_user, db=db_session,
    )


@pytest.mark.parametrize("make_row", [_loan_row, _reimbursement_row], ids=["loan_id", "reimbursement"])
async def test_patch_service_row_is_409_and_unchanged(db_session, real_user, make_row):
    txn = await make_row(db_session, real_user)
    before = (txn.category, txn.category_method, txn.loan_id)

    with pytest.raises(HTTPException) as exc:
        await _patch(db_session, real_user, txn.id, "Інший дохід")  # знак коректний — блокує саме службовий стан

    assert exc.value.status_code == 409
    await db_session.refresh(txn)
    assert (txn.category, txn.category_method, txn.loan_id) == before
    assert await _pool_rows(db_session, txn.id) == []


# --- (д) відхилення -> record_training_example не викликається ------------------

@pytest.fixture
def training_spy(monkeypatch):
    calls = []
    real = transactions_module.record_training_example

    async def _spy(session, transaction, source):
        calls.append((transaction.id, transaction.category, source))
        await real(session, transaction, source)

    monkeypatch.setattr(transactions_module, "record_training_example", _spy)
    return calls


@pytest.mark.parametrize("case", ["negative_income", "positive_expense", "loan_row", "reimbursement_row"])
async def test_rejected_patch_does_not_record_training_example(db_session, real_user, training_spy, case):
    if case == "loan_row":
        txn, category = await _loan_row(db_session, real_user), "Інший дохід"
    elif case == "reimbursement_row":
        txn, category = await _reimbursement_row(db_session, real_user), "Інший дохід"
    else:
        account = await _account(db_session, real_user)
        amount, category = (-150_000, "Заробітна плата") if case == "negative_income" else (150_000, "Продукти")
        txn = await _txn(db_session, real_user, account, amount=amount)

    with pytest.raises(HTTPException) as exc:
        await _patch(db_session, real_user, txn.id, category)

    assert exc.value.status_code in (409, 422)
    assert training_spy == []
    assert await _pool_rows(db_session, txn.id) == []


# --- (з) чужий рядок -> 404 ----------------------------------------------------

async def test_patch_foreign_row_is_404(db_session, real_user):
    other = User(id=f"test-{uuid.uuid4()}", email=f"other-{uuid.uuid4()}@example.com", password_hash="x")
    db_session.add(other)
    await db_session.flush()
    account = await _account(db_session, other)
    txn = await _txn(db_session, other, account, amount=-150_000)

    with pytest.raises(HTTPException) as exc:
        await _patch(db_session, real_user, txn.id, "Продукти")

    assert exc.value.status_code == 404
    assert (txn.category, txn.category_method) == ("pending", "pending")


# --- POST /transactions/manual: та сама перевірка знака -------------------------

@pytest.mark.parametrize("amount, category", [(-100.0, "Заробітна плата"), (250.0, "Продукти")])
async def test_manual_create_rejects_wrong_sign_category(db_session, real_user, training_spy, amount, category):
    with pytest.raises(HTTPException) as exc:
        await create_manual_transaction(
            ManualTransactionRequest(date="2026-03-01T12:00:00", description="Тест знака",
                                     amount=amount, category=category),
            current_user=real_user, db=db_session,
        )

    assert exc.value.status_code == 422
    created = (await db_session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.user_id == real_user.id)
    )).scalar_one()
    assert created == 0
    assert training_spy == []


# --- (е) GET /transactions --------------------------------------------------------

async def test_list_defaults_unchanged(db_session, real_user, client):
    # Без параметрів — список (не обгортка), limit 50, сортування time desc.
    account = await _account(db_session, real_user)
    for i in range(55):
        await _txn(db_session, real_user, account, amount=-100 * (i + 1), time=1_772_000_000 + i)

    resp = await client.get("/transactions")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and len(body) == 50
    assert [t["time"] for t in body] == sorted((t["time"] for t in body), reverse=True)
    assert body[0]["time"] == 1_772_000_054


async def test_list_total_count_header_ignores_limit_offset(db_session, real_user, client):
    account = await _account(db_session, real_user)
    for i in range(55):
        await _txn(db_session, real_user, account, amount=-100, time=1_772_000_000 + i)

    resp = await client.get("/transactions", params={"limit": 5, "offset": 10})

    assert resp.status_code == 200
    assert len(resp.json()) == 5
    assert resp.headers.get("X-Total-Count") == "55"


async def test_list_offset(db_session, real_user, client):
    account = await _account(db_session, real_user)
    for i in range(6):
        await _txn(db_session, real_user, account, amount=-100, time=1_772_000_000 + i)

    resp = await client.get("/transactions", params={"limit": 2, "offset": 2})

    assert [t["time"] for t in resp.json()] == [1_772_000_003, 1_772_000_002]


async def test_list_limit_max_1000(client):
    assert (await client.get("/transactions", params={"limit": 1000})).status_code == 200
    assert (await client.get("/transactions", params={"limit": 1001})).status_code == 422


@pytest.mark.parametrize("category, expected", [("pending", {"p1", "p2"}), ("Продукти", {"g1"})])
async def test_list_filter_category(db_session, real_user, client, category, expected):
    account = await _account(db_session, real_user)
    await _txn(db_session, real_user, account, amount=-100, description="p1")
    await _txn(db_session, real_user, account, amount=500, description="p2")
    await _txn(db_session, real_user, account, amount=-100, description="g1", category="Продукти", category_method="mcc")

    resp = await client.get("/transactions", params={"category": category})

    assert {t["description"] for t in resp.json()} == expected
    assert resp.headers.get("X-Total-Count") == str(len(expected))


@pytest.mark.parametrize("sign, expected", [("in", {"inflow"}), ("out", {"outflow"})])
async def test_list_filter_sign(db_session, real_user, client, sign, expected):
    account = await _account(db_session, real_user)
    await _txn(db_session, real_user, account, amount=500, description="inflow")
    await _txn(db_session, real_user, account, amount=-500, description="outflow")

    resp = await client.get("/transactions", params={"sign": sign})

    assert {t["description"] for t in resp.json()} == expected


async def test_list_filter_sign_invalid_is_422(client):
    assert (await client.get("/transactions", params={"sign": "both"})).status_code == 422


async def test_list_filter_dates_are_kyiv_inclusive(db_session, real_user, client):
    # Межі — календарні дні за Києвом: 2026-03-01 00:00 за Києвом =
    # 2026-02-28 22:00 UTC.
    account = await _account(db_session, real_user)
    rows = {
        "before": _kyiv_ts(2026, 2, 28, 23, 59, 59),
        "first": _kyiv_ts(2026, 3, 1, 0, 0, 0),
        "last": _kyiv_ts(2026, 3, 31, 23, 59, 59),
        "after": _kyiv_ts(2026, 4, 1, 0, 0, 0),
    }
    for name, ts in rows.items():
        await _txn(db_session, real_user, account, amount=-100, description=name, time=ts)

    resp = await client.get("/transactions", params={"from": "2026-03-01", "to": "2026-03-31"})

    assert {t["description"] for t in resp.json()} == {"first", "last"}
    assert resp.headers.get("X-Total-Count") == "2"


async def test_list_search_q_case_insensitive_and_literal(db_session, real_user, client):
    account = await _account(db_session, real_user)
    await _txn(db_session, real_user, account, amount=-100, description="АТБ Маркет")
    await _txn(db_session, real_user, account, amount=-100, description="супермаркет атб")
    await _txn(db_session, real_user, account, amount=-100, description="Сільпо")
    await _txn(db_session, real_user, account, amount=-100, description="Знижка 50% на каву")
    await _txn(db_session, real_user, account, amount=-100, description="Знижка 500 грн")

    assert {t["description"] for t in (await client.get("/transactions", params={"q": "атб"})).json()} == {
        "АТБ Маркет", "супермаркет атб",
    }
    # % і _ — літерали, а не шаблони ILIKE
    assert {t["description"] for t in (await client.get("/transactions", params={"q": "50%"})).json()} == {
        "Знижка 50% на каву",
    }


async def test_list_filters_are_user_scoped(db_session, real_user, client):
    other = User(id=f"test-{uuid.uuid4()}", email=f"other-{uuid.uuid4()}@example.com", password_hash="x")
    db_session.add(other)
    await db_session.flush()
    await _txn(db_session, other, await _account(db_session, other), amount=-100, description="чужий")
    await _txn(db_session, real_user, await _account(db_session, real_user), amount=-100, description="свій")

    resp = await client.get("/transactions", params={"category": "pending"})

    assert {t["description"] for t in resp.json()} == {"свій"}
    assert resp.headers.get("X-Total-Count") == "1"


# --- (є) поля відповіді -----------------------------------------------------------

async def test_response_has_new_fields(db_session, real_user, client):
    account = await _account(db_session, real_user)
    txn = await _txn(db_session, real_user, account, amount=-12_345, mcc=5411,
                     category="Продукти", category_method="mcc")

    [item] = (await client.get("/transactions")).json()

    # базові поля
    assert {k: item[k] for k in ("id", "time", "description", "amount_uah", "category", "category_method",
                                 "possible_duplicate", "message")} == {
        "id": txn.id, "time": txn.time, "description": "Опис", "amount_uah": -124,
        "category": "Продукти", "category_method": "mcc", "possible_duplicate": False, "message": None,
    }
    # додаткові поля
    assert {k: item.get(k, "<немає>") for k in ("loan_id", "account_id", "mcc", "amount")} == {
        "loan_id": None, "account_id": account.id, "mcc": 5411, "amount": -12_345,
    }


# --- (ж) групи категорій -----------------------------------------------------------

async def test_categories_groups():
    body = await list_manual_categories()

    # базові ключі
    assert body["categories"] == MANUAL_ENTRY_CATEGORIES
    assert body["income_categories"] == sorted(INCOME_CATEGORIES)
    assert "non_spending_categories" in body

    assert set(body) >= {"expense", "income", "transfer", "service"}
    assert body["income"] == sorted(INCOME_CATEGORIES)
    assert body["transfer"] == sorted(TRANSFER_CATEGORIES)
    assert body["service"] == sorted(SERVICE_CATEGORIES)
    assert {"Продукти", "Борги та кредити", "Готівка"} <= set(body["expense"])
    assert set(body["expense"]) & (INCOME_CATEGORIES | TRANSFER_CATEGORIES | SERVICE_CATEGORIES) == set()
    # Кожна категорія dropdown — рівно в одній з груп expense/income/transfer
    assert sorted(body["expense"] + body["income"] + body["transfer"]) == sorted(MANUAL_ENTRY_CATEGORIES)
