"""
Тести позик і POST /transactions/{id}/repay-loan.

Позика створюється з remaining_amount == principal_amount; repay-loan зменшує
залишок і змінює статус not_repaid -> partially_repaid -> repaid; переплата
дає 422 без змін; чужа позика — 404; від'ємна сума — 422. Функції ендпоінтів
викликаються напряму, без HTTP-шару.
"""
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.transaction import Transaction
from app.api.transactions import (
    create_manual_transaction,
    ManualTransactionRequest,
    repay_loan,
    RepayLoanRequest,
)
from app.api.loans import create_loan, LoanCreateRequest


async def _make_pending_income(db_session, real_user, amount_uah: float):
    """Створює додатну pending-транзакцію — стан, у якому вона чекає ручного рішення в income-review."""
    txn = await create_manual_transaction(
        ManualTransactionRequest(
            date=datetime(2026, 8, 1, tzinfo=timezone.utc),
            description="Від: Іван П.",
            amount=amount_uah,
            category=None,
        ),
        current_user=real_user,
        db=db_session,
    )
    txn.category = "pending"
    txn.category_method = "pending"
    await db_session.commit()
    await db_session.refresh(txn)
    return txn


async def _make_loan(db_session, real_user, principal_amount: int):
    return await create_loan(
        LoanCreateRequest(
            borrower_name="Іван П.",
            principal_amount=principal_amount,
            issued_at=date(2026, 7, 1),
        ),
        current_user=real_user,
        db=db_session,
    )


# --- Loan CRUD ---------------------------------------------------------

async def test_create_loan_initializes_remaining_amount(db_session, real_user):
    loan = await _make_loan(db_session, real_user, 5000)
    assert loan.remaining_amount == 5000
    assert loan.status == "not_repaid"


# --- repay-loan: повне погашення ---------------------------------------

async def test_repay_loan_full_repayment(db_session, real_user):
    loan = await _make_loan(db_session, real_user, 1000)
    txn = await _make_pending_income(db_session, real_user, 1000)

    result = await repay_loan(
        txn.id, RepayLoanRequest(loan_id=loan.id),
        current_user=real_user, db=db_session,
    )

    assert result.category == "Повернення позики"
    assert result.category_method == "manual"

    await db_session.refresh(loan)
    assert loan.remaining_amount == 0
    assert loan.status == "repaid"

    reloaded_txn = (
        await db_session.execute(select(Transaction).where(Transaction.id == txn.id))
    ).scalar_one()
    assert reloaded_txn.loan_id == loan.id


# --- repay-loan: часткове погашення -------------------------------------

async def test_repay_loan_partial_repayment(db_session, real_user):
    loan = await _make_loan(db_session, real_user, 1000)
    txn = await _make_pending_income(db_session, real_user, 400)

    await repay_loan(
        txn.id, RepayLoanRequest(loan_id=loan.id),
        current_user=real_user, db=db_session,
    )

    await db_session.refresh(loan)
    assert loan.remaining_amount == 600
    assert loan.status == "partially_repaid"


# --- repay-loan: overpay -> 422, нічого не змінилось --------------------

async def test_repay_loan_overpay_rejected(db_session, real_user):
    loan = await _make_loan(db_session, real_user, 1000)
    txn = await _make_pending_income(db_session, real_user, 1500)

    with pytest.raises(HTTPException) as exc_info:
        await repay_loan(
            txn.id, RepayLoanRequest(loan_id=loan.id),
            current_user=real_user, db=db_session,
        )
    assert exc_info.value.status_code == 422

    await db_session.refresh(loan)
    await db_session.refresh(txn)
    assert loan.remaining_amount == 1000
    assert loan.status == "not_repaid"
    assert txn.loan_id is None
    assert txn.category == "pending"


# --- repay-loan: чужа позика -> 404, не 422/500 --------------------------

async def test_repay_loan_foreign_loan_returns_404(db_session, real_user):
    from app.models.user import User

    other_user = User(
        id=f"test-{uuid.uuid4()}",
        email=f"other-{uuid.uuid4()}@example.com",
        password_hash="not-a-real-hash",
    )
    db_session.add(other_user)
    await db_session.flush()

    other_loan = await _make_loan(db_session, other_user, 1000)
    txn = await _make_pending_income(db_session, real_user, 500)

    with pytest.raises(HTTPException) as exc_info:
        await repay_loan(
            txn.id, RepayLoanRequest(loan_id=other_loan.id),
            current_user=real_user, db=db_session,
        )
    assert exc_info.value.status_code == 404


# --- repay-loan: від'ємна сума -> 422 ------------------------------------

async def test_repay_loan_negative_amount_rejected(db_session, real_user):
    loan = await _make_loan(db_session, real_user, 1000)
    txn = await _make_pending_income(db_session, real_user, -200)

    with pytest.raises(HTTPException) as exc_info:
        await repay_loan(
            txn.id, RepayLoanRequest(loan_id=loan.id),
            current_user=real_user, db=db_session,
        )
    assert exc_info.value.status_code == 422


# --- 'Повернення позики' не рахується доходом і не є відтоком -------------
# Перевіряються властивості рядка: категорія не «Дохід», сума додатна.

async def test_loan_repayment_category_is_not_income_and_amount_is_positive(
    db_session, real_user,
):
    loan = await _make_loan(db_session, real_user, 1000)
    txn = await _make_pending_income(db_session, real_user, 1000)

    result = await repay_loan(
        txn.id, RepayLoanRequest(loan_id=loan.id),
        current_user=real_user, db=db_session,
    )

    # Категорія не «Дохід»
    assert result.category != "Дохід"
    # Сума додатна — не відтік
    assert result.amount_uah > 0
