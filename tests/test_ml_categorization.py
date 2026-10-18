"""
Тести ML-категоризації: POST /mono/recategorize-pending-ml,
record_training_example() і запис навчальних прикладів з правилової й ручної
категоризації.

Класифікатор (get_classifier_for_user, predict_category в app/api/mono.py)
замінено детермінованою заглушкою: перевіряється логіка ендпоінта
(обмеження користувачем, збереження confidence, відсутність запису
ML-передбачень у пул), а не якість моделі.
"""
import uuid
from datetime import date

import pytest
from sqlalchemy import select

import app.api.mono as mono_module
from app.models.account import Account
from app.models.transaction import Transaction
from app.models.loan import Loan
from app.models.category_training_example import CategoryTrainingExample
from app.api.mono import recategorize_pending_ml
from app.api.transactions import (
    create_manual_transaction,
    ManualTransactionRequest,
    update_transaction_category,
    UpdateCategoryRequest,
    repay_loan,
    RepayLoanRequest,
)
from app.services.categorization import record_training_example


def _fake_predict_category(description, clf, threshold=0.6):
    if description == "Сільпо":
        return "Продукти", 0.95, "ml"
    if description == "Ашан невпевнено":
        return "Продукти", 0.65, "ml"
    return None, 0.3, "pending"


@pytest.fixture(autouse=True)
def _fake_ml_model(monkeypatch):
    # Тести не залежать від файлів моделей у ml_artifacts/ і від якості
    # навченої моделі.
    monkeypatch.setattr(mono_module, "get_classifier_for_user", lambda user_id: object())
    monkeypatch.setattr(mono_module, "predict_category", _fake_predict_category)


async def _make_account(db_session, user_id):
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=user_id, source_type="manual")
    db_session.add(account)
    await db_session.flush()
    return account


async def _make_pending_transaction(db_session, user_id, account_id, description):
    txn = Transaction(
        id=str(uuid.uuid4()),
        user_id=user_id,
        account_id=account_id,
        time=1772550000,
        time_precision="date",  # акаунт тесту — manual
        description=description,
        mcc=None,
        amount=-10000,
        amount_uah=-100,
        currency_code=980,
        cashback_amount=0,
        balance=None,
        hold=False,
        category="pending",
        category_method="pending",
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


async def _second_user(db_session):
    from app.models.user import User

    user = User(
        id=f"test-{uuid.uuid4()}",
        email=f"ml-cat-{uuid.uuid4()}@example.com",
        password_hash="not-a-real-hash",
    )
    db_session.add(user)
    await db_session.flush()
    return user


# --- POST /mono/recategorize-pending-ml -------------------------------------

async def test_ml_endpoint_categorizes_confident_pending_transaction(db_session, real_user):
    account = await _make_account(db_session, real_user.id)
    txn = await _make_pending_transaction(db_session, real_user.id, account.id, "Сільпо")

    result = await recategorize_pending_ml(current_user=real_user, db=db_session)

    assert result["stats"]["ml"] == 1
    assert result["stats"]["still_pending"] == 0

    await db_session.refresh(txn)
    assert txn.category == "Продукти"
    assert txn.category_method == "ml"
    assert txn.category_confidence == pytest.approx(0.95)


async def test_ml_endpoint_leaves_low_confidence_as_pending(db_session, real_user):
    account = await _make_account(db_session, real_user.id)
    txn = await _make_pending_transaction(db_session, real_user.id, account.id, "щось незрозуміле")

    result = await recategorize_pending_ml(current_user=real_user, db=db_session)

    assert result["stats"]["ml"] == 0
    assert result["stats"]["still_pending"] == 1

    await db_session.refresh(txn)
    assert txn.category == "pending"
    assert txn.category_method == "pending"
    assert txn.category_confidence is None


async def test_ml_endpoint_flags_low_confidence_ml_predictions(db_session, real_user):
    account = await _make_account(db_session, real_user.id)
    txn = await _make_pending_transaction(db_session, real_user.id, account.id, "Ашан невпевнено")

    result = await recategorize_pending_ml(current_user=real_user, db=db_session)

    assert result["stats"]["ml"] == 1
    assert result["stats"]["low_confidence"] == 1

    await db_session.refresh(txn)
    assert txn.category_method == "ml"
    assert txn.category_confidence == pytest.approx(0.65)


async def test_ml_endpoint_is_user_scoped(db_session, real_user):
    other_user = await _second_user(db_session)

    own_account = await _make_account(db_session, real_user.id)
    other_account = await _make_account(db_session, other_user.id)

    own_txn = await _make_pending_transaction(db_session, real_user.id, own_account.id, "Сільпо")
    other_txn = await _make_pending_transaction(db_session, other_user.id, other_account.id, "Сільпо")

    result = await recategorize_pending_ml(current_user=real_user, db=db_session)

    assert result["total"] == 1  # лише власна транзакція, не чужа

    await db_session.refresh(own_txn)
    await db_session.refresh(other_txn)
    assert own_txn.category_method == "ml"
    assert other_txn.category_method == "pending"  # чужа транзакція не зачеплена


async def test_ml_predictions_never_feed_training_pool(db_session, real_user):
    """ML-передбачення не записуються в category_training_examples."""
    account = await _make_account(db_session, real_user.id)
    txn = await _make_pending_transaction(db_session, real_user.id, account.id, "Сільпо")

    await recategorize_pending_ml(current_user=real_user, db=db_session)

    rows = (
        await db_session.execute(
            select(CategoryTrainingExample).where(
                CategoryTrainingExample.source_transaction_id == txn.id
            )
        )
    ).scalars().all()
    assert rows == []


# --- record_training_example: наповнення пулу навчальних прикладів ----------

async def test_manual_entry_with_category_feeds_training_pool(db_session, real_user):
    request = ManualTransactionRequest(
        date="2026-03-01T12:00:00",
        description="Аптека Подорожник",
        amount=-250.0,
        category="Здоров'я",
    )
    saved = await create_manual_transaction(request, current_user=real_user, db=db_session)

    row = (
        await db_session.execute(
            select(CategoryTrainingExample).where(
                CategoryTrainingExample.source_transaction_id == saved.id
            )
        )
    ).scalar_one()
    assert row.category == "Здоров'я"
    assert row.source == "manual_confirmed"
    assert row.user_id == real_user.id


async def test_manual_entry_without_category_does_not_feed_pool(db_session, real_user):
    request = ManualTransactionRequest(
        date="2026-03-01T12:00:00",
        description="Невідома транзакція",
        amount=-100.0,
        category=None,
    )
    saved = await create_manual_transaction(request, current_user=real_user, db=db_session)

    rows = (
        await db_session.execute(
            select(CategoryTrainingExample).where(
                CategoryTrainingExample.source_transaction_id == saved.id
            )
        )
    ).scalars().all()
    assert rows == []


async def test_patch_category_feeds_training_pool(db_session, real_user):
    account = await _make_account(db_session, real_user.id)
    txn = await _make_pending_transaction(db_session, real_user.id, account.id, "АТБ Маркет")

    await update_transaction_category(
        txn.id, UpdateCategoryRequest(category="Продукти"), current_user=real_user, db=db_session
    )

    row = (
        await db_session.execute(
            select(CategoryTrainingExample).where(
                CategoryTrainingExample.source_transaction_id == txn.id
            )
        )
    ).scalar_one()
    assert row.category == "Продукти"
    assert row.source == "manual_confirmed"


async def test_repay_loan_does_not_feed_training_pool(db_session, real_user):
    """repay_loan не записує службову категорію «Повернення позики» в пул навчальних прикладів."""
    account = await _make_account(db_session, real_user.id)
    loan = Loan(
        id=str(uuid.uuid4()),
        user_id=real_user.id,
        borrower_name="Тест Тестович",
        principal_amount=1000,
        remaining_amount=1000,
        issued_at=date(2026, 1, 1),
    )
    db_session.add(loan)
    await db_session.flush()

    txn = Transaction(
        id=str(uuid.uuid4()),
        user_id=real_user.id,
        account_id=account.id,
        time=1772550000,
        time_precision="date",  # акаунт тесту — manual
        description="Повернення від Тест Тестовича",
        mcc=None,
        amount=50000,
        amount_uah=500,
        currency_code=980,
        cashback_amount=0,
        balance=None,
        hold=False,
        category="pending",
        category_method="pending",
    )
    db_session.add(txn)
    await db_session.flush()

    await repay_loan(txn.id, RepayLoanRequest(loan_id=loan.id), current_user=real_user, db=db_session)

    rows = (
        await db_session.execute(
            select(CategoryTrainingExample).where(
                CategoryTrainingExample.source_transaction_id == txn.id
            )
        )
    ).scalars().all()
    assert rows == []


async def test_record_training_example_upserts_on_category_change(db_session, real_user):
    """Зміна категорії транзакції оновлює приклад у пулі, а не створює новий."""
    account = await _make_account(db_session, real_user.id)
    txn = await _make_pending_transaction(db_session, real_user.id, account.id, "Магазин")
    txn.category = "Продукти"

    await record_training_example(db_session, txn, "mcc")

    txn.category = "Кафе та ресторани"
    await record_training_example(db_session, txn, "manual_confirmed")

    rows = (
        await db_session.execute(
            select(CategoryTrainingExample).where(
                CategoryTrainingExample.source_transaction_id == txn.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].category == "Кафе та ресторани"
    assert rows[0].source == "manual_confirmed"
