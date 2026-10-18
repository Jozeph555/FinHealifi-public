"""
Тести системних категорій для ручного вибору.

- «Подарунки та благодійність» — витрата, лише від'ємні суми; не плутати з
  «Подарунок» (дохід).
- «Отримана позика» — поза прогнозом (не витрата й не дохід), лише додатні
  суми; записів Loan не змінює.
- «Переказ між власними рахунками» — поза прогнозом, обидва знаки.

Назви задаються буквально, а не через TRANSFER_CATEGORIES.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

import app.api.mono as mono_module
from app.api.mono import recategorize_pending_ml
from app.api.transactions import UpdateCategoryRequest, list_manual_categories, update_transaction_category
from app.models.account import Account
from app.models.category_training_example import CategoryTrainingExample
from app.models.transaction import Transaction
from app.services.forecasting import _load_expense_breakdown, _load_monthly_series
from app.services.ml_categorizer import _load_pool, is_ml_assignable

GIFTS = "Подарунки та благодійність"
LOAN_RECEIVED = "Отримана позика"
OWN_TRANSFER = "Переказ між власними рахунками"
NEUTRAL = [LOAN_RECEIVED, OWN_TRANSFER]

MONTH_TS = int(datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc).timestamp())


async def _account(db_session, user) -> Account:
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=user.id, source_type="manual")
    db_session.add(account)
    await db_session.flush()
    return account


async def _txn(db_session, user, account, *, amount, category="pending", category_method="pending",
               description="Опис") -> Transaction:
    txn = Transaction(
        id=str(uuid.uuid4()), user_id=user.id, account_id=account.id,
        time=MONTH_TS, time_precision="date",
        description=description, amount=amount, amount_uah=amount // 100,
        category=category, category_method=category_method,
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


async def _patch(db_session, user, txn_id, category):
    return await update_transaction_category(
        txn_id, UpdateCategoryRequest(category=category), current_user=user, db=db_session,
    )


# --- (а) Подарунки та благодійність: лише від'ємні ------------------------------

async def test_gifts_negative_patch_ok(db_session, real_user):
    txn = await _txn(db_session, real_user, await _account(db_session, real_user), amount=-50_000)

    saved = await _patch(db_session, real_user, txn.id, GIFTS)

    assert (saved.category, saved.category_method) == (GIFTS, "manual")


async def test_gifts_positive_patch_is_sign_422(db_session, real_user):
    txn = await _txn(db_session, real_user, await _account(db_session, real_user), amount=50_000)

    with pytest.raises(HTTPException) as exc:
        await _patch(db_session, real_user, txn.id, GIFTS)

    assert exc.value.status_code == 422
    # саме через знак, а не "Невідома категорія"
    assert "Додатна сума" in str(exc.value.detail)


# --- (б) Отримана позика: лише додатні --------------------------------------------

async def test_loan_received_positive_patch_ok(db_session, real_user):
    txn = await _txn(db_session, real_user, await _account(db_session, real_user), amount=1_000_000)

    saved = await _patch(db_session, real_user, txn.id, LOAN_RECEIVED)

    assert (saved.category, saved.category_method) == (LOAN_RECEIVED, "manual")


async def test_loan_received_negative_patch_is_sign_422(db_session, real_user):
    txn = await _txn(db_session, real_user, await _account(db_session, real_user), amount=-1_000_000)

    with pytest.raises(HTTPException) as exc:
        await _patch(db_session, real_user, txn.id, LOAN_RECEIVED)

    assert exc.value.status_code == 422
    detail = str(exc.value.detail)
    assert "Невідома категорія" not in detail
    assert LOAN_RECEIVED in detail and "Від'ємна сума" in detail


# --- (в) Переказ між власними рахунками: обидва знаки ------------------------------

@pytest.mark.parametrize("amount", [300_000, -300_000])
async def test_own_transfer_both_signs_ok(db_session, real_user, amount):
    txn = await _txn(db_session, real_user, await _account(db_session, real_user), amount=amount)

    saved = await _patch(db_session, real_user, txn.id, OWN_TRANSFER)

    assert (saved.category, saved.category_method) == (OWN_TRANSFER, "manual")


# --- (г) прогноз: нейтральні поза витратами, доходом і pending; подарунки — витрата --

async def test_forecast_ignores_neutral_and_counts_gifts(db_session, real_user):
    account = await _account(db_session, real_user)
    await _txn(db_session, real_user, account, amount=-10_000, category="Продукти", category_method="manual")
    await _txn(db_session, real_user, account, amount=-20_000, category=GIFTS, category_method="manual")
    await _txn(db_session, real_user, account, amount=500_000, category=LOAN_RECEIVED, category_method="manual")
    await _txn(db_session, real_user, account, amount=300_000, category=OWN_TRANSFER, category_method="manual")
    await _txn(db_session, real_user, account, amount=-300_000, category=OWN_TRANSFER, category_method="manual")

    breakdown = await _load_expense_breakdown(real_user.id, session=db_session)
    [month] = breakdown["months"]
    income = await _load_monthly_series(real_user.id, income=True, session=db_session)

    assert month["month"] == "2026-03"
    assert month["categorized"] == 300.0          # Продукти 100 + Подарунки 200
    assert month["total"] == 300.0
    assert (month["pending_merchant"], month["pending_p2p_out"], month["pending_p2p_in"],
            month["pending_inflow"]) == (0.0, 0.0, 0.0, 0.0)
    assert breakdown["by_category"] == {GIFTS: 200.0, "Продукти": 100.0}
    assert income.empty


# --- (д) ML: нейтральні не передбачаються і не вчаться ------------------------------

@pytest.mark.parametrize("category", NEUTRAL)
def test_ml_cannot_assign_neutral(category):
    assert is_ml_assignable(category) is False


def test_ml_can_assign_gifts_expense():
    assert is_ml_assignable(GIFTS) is True


async def test_ml_pool_excludes_neutral(db_session, real_user):
    for description, category in [("Позика від брата", LOAN_RECEIVED),
                                  ("З картки на картку", OWN_TRANSFER),
                                  ("Сільпо", "Продукти")]:
        db_session.add(CategoryTrainingExample(
            id=str(uuid.uuid4()), user_id=real_user.id, source_transaction_id=str(uuid.uuid4()),
            description=description, category=category, source="manual_confirmed",
        ))
    await db_session.flush()

    with pytest.warns(UserWarning):
        texts, labels, owners = await _load_pool(session=db_session)

    assert "Сільпо" in texts
    assert {"Позика від брата", "З картки на картку"} & set(texts) == set()
    assert set(labels) & set(NEUTRAL) == set()


@pytest.mark.parametrize("category", NEUTRAL)
async def test_ml_endpoint_blocks_neutral_prediction(db_session, real_user, monkeypatch, category):
    monkeypatch.setattr(mono_module, "get_classifier_for_user", lambda user_id: object())
    monkeypatch.setattr(mono_module, "predict_category",
                        lambda description, clf, threshold=0.6: (category, 0.95, "ml"))
    txn = await _txn(db_session, real_user, await _account(db_session, real_user), amount=-300_000)
    await db_session.commit()

    result = await recategorize_pending_ml(current_user=real_user, db=db_session)

    await db_session.refresh(txn)
    assert (txn.category, txn.category_method) == ("pending", "pending")
    assert result["stats"]["blocked_by_policy"] == 1


# --- (е) групи категорій ------------------------------------------------------------

async def test_categories_groups_include_new():
    body = await list_manual_categories()

    assert GIFTS in body["expense"] and GIFTS in body["categories"]
    assert set(NEUTRAL) <= set(body["transfer"]) and set(NEUTRAL) <= set(body["categories"])
    assert not set(NEUTRAL) & set(body["expense"] + body["income"])
    assert "Подарунок" in body["income"] and "Подарунок" not in body["expense"] + body["transfer"]
    assert GIFTS not in body["income"]
    # базові ключі на місці; нейтральні — «не витрата» для фільтрів фронтенду
    assert {"categories", "income_categories", "non_spending_categories"} <= set(body)
    assert set(NEUTRAL) <= set(body["non_spending_categories"])
    assert GIFTS not in body["non_spending_categories"]


# --- (є) наявні категорії на місці --------------------------------------------------

async def test_old_categories_unchanged():
    body = await list_manual_categories()

    assert {"Заощадження", "Інвестиції"} <= set(body["transfer"])
    assert {"Продукти", "Борги та кредити", "Готівка"} <= set(body["expense"])
    assert {"pending", "Заощадження", "Інвестиції", "Заробітна плата"} <= set(body["non_spending_categories"])
