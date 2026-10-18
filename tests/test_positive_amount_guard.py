"""
Тести guard_positive_amount_category в автоматичних шляхах категоризації:
ML (POST /mono/recategorize-pending-ml) і категорія банку під час імпорту
виписки (statement_importer.BANK_CATEGORY_MAP). Додатна сума не отримує
категорію витрат; ручні шляхи охоронець не змінює.

ML-класифікатор замінено детермінованою заглушкою; categorize_all_transactions,
яку викликає підтвердження імпорту, працює з db_session.
"""
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

import app.api.imports as imports_module
import app.api.mono as mono_module
import app.services.categorization as categorization_module
from app.api.imports import ConfirmImportRequest, confirm_statement_import
from app.api.mono import recategorize_pending_ml
from app.models.account import Account
from app.models.transaction import Transaction


# --- (д) юніт-тест охоронця -------------------------------------------------

@pytest.mark.parametrize("amount, category", [
    (100_000, "Продукти"),
    (100_000, "Борги та кредити"),
    (100_000, "Житло та комунальні"),
    (100_000, "Готівка"),
    (1, "Кафе та ресторани"),
])
def test_guard_positive_expense_goes_to_pending(amount, category):
    from app.services.mcc_mapping import guard_positive_amount_category

    assert guard_positive_amount_category(amount, category) == "pending"


@pytest.mark.parametrize("amount, category", [
    (100_000, "Заробітна плата"),     # дохід
    (100_000, "Інший дохід"),         # дохід
    (100_000, "Заощадження"),         # переказ
    (100_000, "Інвестиції"),          # переказ
    (100_000, "pending"),             # службова
    (100_000, "Повернення позики"),   # службова (repay-loan)
    (100_000, None),                  # категорії немає — нічого охороняти
    (-100_000, "Продукти"),           # від'ємна сума — витрата
    (-100_000, "Заробітна плата"),    # від'ємна сума: охоронець знак доходу не перевіряє
    (0, "Продукти"),                  # нуль не додатний
])
def test_guard_leaves_other_cases_unchanged(amount, category):
    from app.services.mcc_mapping import guard_positive_amount_category

    assert guard_positive_amount_category(amount, category) == category


# --- (а, б) ML-ендпоінт --------------------------------------------------------

async def _pending_txn(db_session, user, amount):
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=user.id, source_type="manual")
    db_session.add(account)
    await db_session.flush()
    txn = Transaction(
        id=str(uuid.uuid4()), user_id=user.id, account_id=account.id,
        time=1_780_000_000, time_precision="date",  # акаунт тесту — manual
        amount=amount, amount_uah=amount // 100,
        description="Переказ від Олени",
        category="pending", category_method="pending",
    )
    db_session.add(txn)
    await db_session.commit()
    return txn


@pytest.fixture
def ml_predicts_groceries(monkeypatch):
    monkeypatch.setattr(mono_module, "get_classifier_for_user", lambda user_id: object())
    monkeypatch.setattr(
        mono_module, "predict_category",
        lambda description, clf, threshold=0.6: ("Продукти", 0.95, "ml"),
    )


async def test_ml_does_not_assign_expense_category_to_positive_amount(db_session, real_user, ml_predicts_groceries):
    txn = await _pending_txn(db_session, real_user, amount=150_000)

    result = await recategorize_pending_ml(current_user=real_user, db=db_session)

    await db_session.refresh(txn)
    assert (txn.category, txn.category_method, txn.category_confidence) == ("pending", "pending", None), (
        "ML поставив категорію витрат додатній сумі"
    )
    assert result["stats"]["ml"] == 0
    # Заблоковане охоронцем передбачення — частина still_pending, як і ML_FORBIDDEN_CATEGORIES.
    assert result["stats"]["blocked_by_policy"] == 1
    assert result["stats"]["still_pending"] == 1


async def test_ml_assigns_expense_category_to_negative_amount(db_session, real_user, ml_predicts_groceries):
    txn = await _pending_txn(db_session, real_user, amount=-150_000)

    result = await recategorize_pending_ml(current_user=real_user, db=db_session)

    await db_session.refresh(txn)
    assert (txn.category, txn.category_method, txn.category_confidence) == ("Продукти", "ml", 0.95)
    assert result["stats"]["ml"] == 1
    assert result["stats"]["blocked_by_policy"] == 0


# --- (в, г) bank_provided при імпорті --------------------------------------------

@pytest.fixture
def categorize_in_test_session(db_session, monkeypatch):
    @asynccontextmanager
    async def _session():
        yield db_session  # без close(): сесію закриває фікстура db_session

    monkeypatch.setattr(categorization_module, "AsyncSessionLocal", _session)


async def _import_one_row(db_session, user, amount: str):
    # "Комуналка та інтернет" -> BANK_CATEGORY_MAP -> "Житло та комунальні".
    # Опис без жодного ключового слова: після охоронця рядок проходить звичайну
    # категоризацію й лишається pending. Додаткові рядки з немапленою категорією
    # потрібні, щоб колонку суми можна було визначити за вмістом.
    csv = "\n".join([
        "дата,опис,категорія,сума,баланс",
        "28.02.2026,Кав'ярня Аромакава,Інше,-75.50,12000.00",
        f"01.03.2026,Платіж 7731,Комуналка та інтернет,{amount},13500.00",
        "02.03.2026,Парковка,Інше,-8.33,13491.67",
        "03.03.2026,АТБ Маркет,Інше,-1243.99,12247.68",
    ]).encode("utf-8")
    import_id = str(uuid.uuid4())
    imports_module._pending_imports[import_id] = {
        "user_id": user.id, "file_bytes": csv, "filename": "statement.csv",
    }
    result = await confirm_statement_import(
        ConfirmImportRequest(import_id=import_id, bank_id="privatbank"), current_user=user, db=db_session,
    )
    assert result["created"] == 4
    return (await db_session.execute(
        select(Transaction).where(
            Transaction.account_id == result["account_id"], Transaction.description == "Платіж 7731",
        )
    )).scalar_one()


async def test_bank_provided_expense_label_on_positive_amount_goes_to_pending(
    db_session, real_user, categorize_in_test_session,
):
    txn = await _import_one_row(db_session, real_user, "1500.00")

    assert (txn.category, txn.category_method) == ("pending", "pending"), (
        "bank_provided поставив категорію витрат додатній сумі"
    )


async def test_bank_provided_expense_label_on_negative_amount_keeps_mapping(
    db_session, real_user, categorize_in_test_session,
):
    txn = await _import_one_row(db_session, real_user, "-1500.00")

    assert (txn.category, txn.category_method) == ("Житло та комунальні", "bank_provided")
