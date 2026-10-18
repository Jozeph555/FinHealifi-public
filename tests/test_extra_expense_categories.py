"""
Тести категорій витрат «Навчання та саморозвиток» і «Інші витрати» в ручному виборі.

Обидві — звичайні витрати, лише для від'ємних сум. «Інші витрати» не
збігається з цільовою категорією «Інше» для mark-reimbursement. Прогноз, радник
і ML обробляють їх як будь-яку витрату (усе, що не входить у
forecasting.EXPENSE_EXCLUDED_CATEGORIES).
"""
import uuid
from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi import HTTPException

from app.api.transactions import UpdateCategoryRequest, list_manual_categories, update_transaction_category
from app.models.account import Account
from app.models.category_training_example import CategoryTrainingExample
from app.models.transaction import Transaction
from app.services.forecasting import EXPENSE_EXCLUDED_CATEGORIES, _load_expense_breakdown, _load_monthly_series
from app.services.ml_categorizer import (
    EXCLUDED_CATEGORIES,
    ML_FORBIDDEN_CATEGORIES,
    _fit_default_classifier,
    _load_pool,
    is_ml_assignable,
)

EDUCATION = "Навчання та саморозвиток"
OTHER_EXPENSES = "Інші витрати"
NEW = [EDUCATION, OTHER_EXPENSES]

MONTH_TS = int(datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc).timestamp())


async def _txn(db_session, user, *, amount, category="pending", category_method="pending"):
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=user.id, source_type="manual")
    db_session.add(account)
    await db_session.flush()
    txn = Transaction(
        id=str(uuid.uuid4()), user_id=user.id, account_id=account.id,
        time=MONTH_TS, time_precision="date", description="Опис",
        amount=amount, amount_uah=amount // 100,
        category=category, category_method=category_method,
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


async def _patch(db_session, user, txn_id, category):
    return await update_transaction_category(
        txn_id, UpdateCategoryRequest(category=category), current_user=user, db=db_session,
    )


# --- (а) PATCH: від'ємна 200, додатна 422 через знак ------------------------------

@pytest.mark.parametrize("category", NEW)
async def test_patch_negative_ok(db_session, real_user, category):
    txn = await _txn(db_session, real_user, amount=-75_000)

    saved = await _patch(db_session, real_user, txn.id, category)

    assert (saved.category, saved.category_method) == (category, "manual")


@pytest.mark.parametrize("category", NEW)
async def test_patch_positive_is_sign_422(db_session, real_user, category):
    txn = await _txn(db_session, real_user, amount=75_000)

    with pytest.raises(HTTPException) as exc:
        await _patch(db_session, real_user, txn.id, category)

    assert exc.value.status_code == 422
    detail = str(exc.value.detail)
    assert "Невідома категорія" not in detail
    assert "Додатна сума" in detail and "mark-reimbursement" in detail


# --- (б) прогноз: нові категорії — витрати місяця ---------------------------------

async def test_forecast_counts_new_categories_as_expenses(db_session, real_user):
    await _txn(db_session, real_user, amount=-10_000, category="Продукти", category_method="manual")
    await _txn(db_session, real_user, amount=-40_000, category=EDUCATION, category_method="manual")
    await _txn(db_session, real_user, amount=-25_000, category=OTHER_EXPENSES, category_method="manual")

    breakdown = await _load_expense_breakdown(real_user.id, session=db_session)
    [month] = breakdown["months"]
    income = await _load_monthly_series(real_user.id, income=True, session=db_session)

    assert set(NEW) & set(EXPENSE_EXCLUDED_CATEGORIES) == set()
    assert month["categorized"] == 750.0 and month["total"] == 750.0  # 100 + 400 + 250
    assert (month["pending_merchant"], month["pending_p2p_out"], month["pending_inflow"]) == (0.0, 0.0, 0.0)
    assert breakdown["by_category"] == {EDUCATION: 400.0, OTHER_EXPENSES: 250.0, "Продукти": 100.0}
    assert income.empty


# --- (в) групи категорій ------------------------------------------------------------

async def test_categories_groups_have_new_expenses():
    body = await list_manual_categories()

    for category in NEW:
        assert category in body["categories"] and category in body["expense"]
        assert category not in body["income"] + body["transfer"] + body["service"]
        assert category not in body["non_spending_categories"]
    assert {"categories", "income_categories", "non_spending_categories"} <= set(body)
    # «Інше» лишається поза списком ручного вибору
    assert "Інше" not in body["categories"]


# --- (г) ML: звичайні класи; відсутність прикладів не ламає навчання ----------------

@pytest.mark.parametrize("category", NEW)
def test_ml_new_categories_are_ordinary_classes(category):
    assert category not in EXCLUDED_CATEGORIES
    assert category not in ML_FORBIDDEN_CATEGORIES
    assert is_ml_assignable(category) is True


async def test_ml_pool_keeps_new_category_examples(db_session, real_user, recwarn):
    db_session.add(CategoryTrainingExample(
        id=str(uuid.uuid4()), user_id=real_user.id, source_transaction_id=str(uuid.uuid4()),
        description="Курс англійської", category=EDUCATION, source="manual_confirmed",
    ))
    await db_session.flush()

    texts, labels, owners = await _load_pool(session=db_session)

    assert "Курс англійської" in texts and EDUCATION in labels
    assert not [w for w in recwarn if EDUCATION in str(w.message)]


def test_training_without_examples_of_new_categories():
    # Класи моделі — лише мітки з пулу; категорія без прикладів класом не стає
    # і навчання не ламає. Синтетичні embeddings — без завантаження моделі.
    rng = np.random.default_rng(0)
    labels = ["Продукти"] * 20 + ["Розваги"] * 20
    clf, _ = _fit_default_classifier(rng.normal(size=(len(labels), 8)), labels)

    assert set(clf.classes_) == {"Продукти", "Розваги"}


# --- (д) наявні категорії не змінилися -------------------------------------------

OLD_MANUAL_ENTRY_EXPENSES = [
    "Продукти", "Кафе та ресторани", "Транспорт", "АЗС", "Здоров'я",
    "Краса та догляд", "Спорт та фітнес", "Розваги", "Цифровий контент",
    "Одяг та взуття", "Техніка та ПЗ", "Житло та комунальні",
    "Зв'язок та інтернет", "Доставка", "Готівка", "Подарунки та благодійність",
    "Борги та кредити",
]


async def test_old_categories_unchanged():
    body = await list_manual_categories()

    assert set(OLD_MANUAL_ENTRY_EXPENSES) <= set(body["expense"])
    assert {"Заощадження", "Інвестиції", "Отримана позика", "Переказ між власними рахунками"} <= set(body["transfer"])
    assert "Подарунок" in body["income"]
    # нові — лише додані, старий порядок не зсунуто
    assert [c for c in body["expense"] if c not in NEW] == OLD_MANUAL_ENTRY_EXPENSES
