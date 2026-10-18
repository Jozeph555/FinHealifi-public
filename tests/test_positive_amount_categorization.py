"""
Тести того, що додатні суми не отримують категорію витрат від правил MCC і ключових слів.

Якщо сума > 0, а правило дало категорію витрат (не дохід і не переказ),
транзакція лишається pending. Від'ємні суми, дохід, перекази й рішення
користувача (manual, repay-loan, reimbursement) не змінюються.

categorize_all_transactions і recategorize_pending відкривають власну сесію;
у тесті її замінено на db_session, тож перевіряється повний шлях «рядок у БД
-> правило -> категорія».
"""
import uuid
from contextlib import asynccontextmanager

import pytest

import app.services.categorization as categorization_module
from app.models.account import Account
from app.models.transaction import Transaction
from app.services.categorization import categorize_all_transactions, recategorize_pending




@pytest.fixture
def categorize_in_test_session(db_session, monkeypatch):
    @asynccontextmanager
    async def _session():
        yield db_session  # без close(): сесію закриває фікстура db_session

    monkeypatch.setattr(categorization_module, "AsyncSessionLocal", _session)


async def _add_txn(db_session, user, *, amount, description, mcc=4829, category=None, category_method=None):
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=user.id, source_type="monobank_api")
    db_session.add(account)
    await db_session.flush()
    txn = Transaction(
        id=str(uuid.uuid4()), user_id=user.id, account_id=account.id,
        time=1_760_000_000, time_precision="second",
        description=description, mcc=mcc,
        amount=amount, amount_uah=amount // 100,
        category=category, category_method=category_method,
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


# а) keyword-правило дає категорію витрат -> для додатної суми pending
@pytest.mark.parametrize("description, expense_category", [
    ("Від: Іван Кредитенко, кредит", "Борги та кредити"),
    ("Погашення боргу", "Борги та кредити"),
    ("Оренда за жовтень", "Житло та комунальні"),
])
async def test_positive_amount_keyword_expense_rule_goes_to_pending(
    db_session, real_user, categorize_in_test_session, description, expense_category,
):
    txn = await _add_txn(db_session, real_user, amount=1_000_000, description=description)

    await categorize_all_transactions()

    assert (txn.category, txn.category_method) == ("pending", "pending"), (
        f"додатна сума отримала категорію витрат з keyword-правила (очікувалось pending, "
        f"без змін було б {expense_category!r})"
    )


# б) MCC-правило дає категорію витрат -> для додатної суми pending
@pytest.mark.parametrize("mcc, expense_category", [(5411, "Продукти"), (5812, "Кафе та ресторани")])
async def test_positive_amount_mcc_expense_rule_goes_to_pending(
    db_session, real_user, categorize_in_test_session, mcc, expense_category,
):
    txn = await _add_txn(db_session, real_user, amount=20_500, description="Повернення коштів", mcc=mcc)

    await categorize_all_transactions()

    assert (txn.category, txn.category_method) == ("pending", "pending"), (
        f"додатна сума отримала категорію витрат з MCC-правила (без змін було б {expense_category!r})"
    )


# б') те саме для recategorize_pending
async def test_positive_amount_expense_rule_goes_to_pending_on_recategorize(
    db_session, real_user, categorize_in_test_session,
):
    txn = await _add_txn(db_session, real_user, amount=1_000_000, description="кредит",
                         category="pending", category_method="pending")

    await recategorize_pending()

    assert (txn.category, txn.category_method) == ("pending", "pending")


# в) від'ємна сума з тим самим описом чи MCC лишається в категорії витрат
@pytest.mark.parametrize("description, mcc, expense_category", [
    ("Від: Іван Кредитенко, кредит", 4829, "Борги та кредити"),
    ("Погашення боргу", 4829, "Борги та кредити"),
    ("Оренда за жовтень", 4829, "Житло та комунальні"),
    ("Повернення коштів", 5411, "Продукти"),
    ("Повернення коштів", 5812, "Кафе та ресторани"),
])
async def test_negative_amount_keeps_expense_category(
    db_session, real_user, categorize_in_test_session, description, mcc, expense_category,
):
    txn = await _add_txn(db_session, real_user, amount=-1_000_000, description=description, mcc=mcc)

    await categorize_all_transactions()

    assert txn.category == expense_category
    assert txn.category_method in ("keyword", "mcc")


# г) "Погашення облігацій" — повернення тіла облігацій, переказ, а не платіж за боргом
async def test_bond_redemption_positive_is_investments(db_session, real_user, categorize_in_test_session):
    txn = await _add_txn(db_session, real_user, amount=4_099_500, description="Погашення облігацій")

    await categorize_all_transactions()

    assert (txn.category, txn.category_method) == ("Інвестиції", "keyword")


# г') дохід і перекази для додатних сум не змінюються
@pytest.mark.parametrize("description, category", [
    ("Зарплата за вересень", "Заробітна плата"),
    ("Часткове зняття банки «На відпустку»", "Заощадження"),
    ("Продаж облігацій", "Інвестиції"),
])
async def test_positive_amount_income_and_transfer_unchanged(
    db_session, real_user, categorize_in_test_session, description, category,
):
    txn = await _add_txn(db_session, real_user, amount=500_000, description=description)

    await categorize_all_transactions()

    assert (txn.category, txn.category_method) == (category, "keyword")


# д) рішення користувача не перекатегоризовуються, навіть якщо сума додатна
@pytest.mark.parametrize("category, category_method", [
    ("Повернення позики", "manual"),         # категорія POST /transactions/{id}/repay-loan
    ("Житло та комунальні", "manual"),       # ручна категорія витрат від користувача
    ("Кафе та ресторани", "reimbursement"),  # POST /transactions/{id}/mark-reimbursement
])
async def test_positive_user_decided_category_unchanged(
    db_session, real_user, categorize_in_test_session, category, category_method,
):
    txn = await _add_txn(db_session, real_user, amount=100_000, description="Оренда за жовтень, кредит",
                         category=category, category_method=category_method)

    await categorize_all_transactions()
    await recategorize_pending()

    assert (txn.category, txn.category_method) == (category, category_method)
