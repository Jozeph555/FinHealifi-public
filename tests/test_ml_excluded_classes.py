"""
Тести того, що ML не призначає перекази (TRANSFER_CATEGORIES) і дохід
(INCOME_CATEGORIES): ці класи відкидаються з пулу навчання, включно із
синтетичними прикладами, і не застосовуються під час передбачення, навіть
якщо модель їх містить.

Класифікатор у тесті ендпоінта замінено детермінованою заглушкою.
"""
import uuid

import pytest

import app.api.mono as mono_module
from app.api.mono import recategorize_pending_ml
from app.models.account import Account
from app.models.category_training_example import CategoryTrainingExample
from app.models.transaction import Transaction
from app.services.mcc_mapping import INCOME_CATEGORIES, TRANSFER_CATEGORIES
from app.services.ml_categorizer import _load_pool

ML_FORBIDDEN = TRANSFER_CATEGORIES | INCOME_CATEGORIES


async def test_training_pool_has_no_transfer_or_income_labels(db_session):
    texts, labels, owners = await _load_pool(session=db_session)

    assert set(labels) & ML_FORBIDDEN == set()


@pytest.mark.parametrize("source", ["manual_confirmed", "keyword"])
async def test_forbidden_class_from_pool_rows_is_excluded_with_warning(db_session, real_user, source):
    # Фільтр _load_pool охоплює всі джерела: рядки правил, ручні підтвердження
    # й синтетичні приклади.
    db_session.add(CategoryTrainingExample(
        id=str(uuid.uuid4()), user_id=real_user.id, source_transaction_id=str(uuid.uuid4()),
        description="Поповнення брокерського рахунку", category="Інвестиції", source=source,
    ))
    db_session.add(CategoryTrainingExample(
        id=str(uuid.uuid4()), user_id=real_user.id, source_transaction_id=str(uuid.uuid4()),
        description="Сільпо", category="Продукти", source=source,
    ))
    await db_session.flush()

    with pytest.warns(UserWarning, match="Інвестиції"):
        texts, labels, owners = await _load_pool(session=db_session)

    assert "Сільпо" in texts
    assert "Поповнення брокерського рахунку" not in texts
    assert set(labels) & ML_FORBIDDEN == set()


@pytest.mark.parametrize("forbidden_class", ["Інвестиції", "Заощадження", "Заробітна плата"])
async def test_ml_endpoint_does_not_assign_transfer_or_income_class(
    db_session, real_user, monkeypatch, forbidden_class,
):
    monkeypatch.setattr(mono_module, "get_classifier_for_user", lambda user_id: object())
    monkeypatch.setattr(
        mono_module, "predict_category",
        lambda description, clf, threshold=0.6: (forbidden_class, 0.95, "ml"),
    )
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=real_user.id, source_type="manual")
    db_session.add(account)
    await db_session.flush()
    txn = Transaction(
        id=str(uuid.uuid4()), user_id=real_user.id, account_id=account.id,
        time=1_780_000_000, time_precision="date",  # акаунт тесту — manual
        amount=-500000, amount_uah=-5000,
        description="Поповнення брокерського рахунку",
        category="pending", category_method="pending",
    )
    db_session.add(txn)
    await db_session.commit()

    result = await recategorize_pending_ml(current_user=real_user, db=db_session)

    await db_session.refresh(txn)
    assert txn.category == "pending"
    assert txn.category_method == "pending"
    assert txn.category_confidence is None
    assert result["stats"]["ml"] == 0
    assert result["stats"]["blocked_by_policy"] == 1
    # blocked_by_policy — частина still_pending (транзакція лишилася pending):
    # ml + still_pending == total.
    assert result["stats"]["still_pending"] == 1
    assert result["total"] == 1
