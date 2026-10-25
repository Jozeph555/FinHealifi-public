"""
Тести POST /transactions/{id}/mark-reimbursement і врахування повернень
спільних витрат у прогнозі.

Ендпоінт позначає додатну pending-транзакцію category_method='reimbursement',
не змінюючи початкової витрати; forecasting._load_monthly_series віднімає такі
суми від витрат тієї ж категорії за той самий місяць. Контекст радника
(advisor_context.build_advisor_context) категорій витрат не містить. Функції
викликаються з db_session, без HTTP-шару.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.transactions import (
    create_manual_transaction,
    ManualTransactionRequest,
    mark_reimbursement,
    MarkReimbursementRequest,
    REIMBURSEMENT_TARGET_CATEGORIES,
)
from app.services.forecasting import _load_monthly_series
from app.services.advisor_context import build_advisor_context
from app.services.llm_advisor import _format_context


async def _make_expense(db_session, real_user, date: datetime, amount_uah: float, category: str):
    return await create_manual_transaction(
        ManualTransactionRequest(date=date, description="Спільна витрата", amount=amount_uah, category=category),
        current_user=real_user,
        db=db_session,
    )


async def _make_pending_income(db_session, real_user, date: datetime, amount_uah: float):
    # force=True: тести створюють кілька різних транзакцій з однаковими часом і
    # сумою (повернення часток спільного рахунку).
    txn = await create_manual_transaction(
        ManualTransactionRequest(
            date=date, description="Від друга", amount=amount_uah, category=None, force=True,
        ),
        current_user=real_user,
        db=db_session,
    )
    txn.category = "pending"
    txn.category_method = "pending"
    await db_session.commit()
    await db_session.refresh(txn)
    return txn


# --- mark-reimbursement: happy path --------------------------------------

async def test_mark_reimbursement_happy_path(db_session, real_user):
    txn = await _make_pending_income(db_session, real_user, datetime(2026, 8, 10, tzinfo=timezone.utc), 250)

    result = await mark_reimbursement(
        txn.id, MarkReimbursementRequest(category="Кафе та ресторани"),
        current_user=real_user, db=db_session,
    )

    assert result.category == "Кафе та ресторани"
    assert result.category_method == "reimbursement"


# --- mark-reimbursement: rejects non-positive amount ----------------------

async def test_mark_reimbursement_rejects_negative_amount(db_session, real_user):
    txn = await _make_pending_income(db_session, real_user, datetime(2026, 8, 10, tzinfo=timezone.utc), -100)

    with pytest.raises(HTTPException) as exc_info:
        await mark_reimbursement(
            txn.id, MarkReimbursementRequest(category="Кафе та ресторани"),
            current_user=real_user, db=db_session,
        )
    assert exc_info.value.status_code == 422


# --- mark-reimbursement: rejects excluded/unknown categories --------------

@pytest.mark.parametrize("category", ["Дохід", "pending", "Заощадження", "Готівка", "Інвестиції", "Не існує"])
async def test_mark_reimbursement_rejects_excluded_category(db_session, real_user, category):
    txn = await _make_pending_income(db_session, real_user, datetime(2026, 8, 10, tzinfo=timezone.utc), 250)

    with pytest.raises(HTTPException) as exc_info:
        await mark_reimbursement(
            txn.id, MarkReimbursementRequest(category=category),
            current_user=real_user, db=db_session,
        )
    assert exc_info.value.status_code == 422


# --- mark-reimbursement: приймає категорію «Інше» ----------------------------

async def test_mark_reimbursement_accepts_inshe(db_session, real_user):
    assert "Інше" in REIMBURSEMENT_TARGET_CATEGORIES
    txn = await _make_pending_income(db_session, real_user, datetime(2026, 8, 10, tzinfo=timezone.utc), 250)

    result = await mark_reimbursement(
        txn.id, MarkReimbursementRequest(category="Інше"),
        current_user=real_user, db=db_session,
    )
    assert result.category == "Інше"
    assert result.category_method == "reimbursement"


# --- віднімання в forecasting._load_monthly_series ------------------------

async def test_forecasting_nets_reimbursement_against_same_month_expense(db_session, real_user):
    await _make_expense(db_session, real_user, datetime(2026, 8, 5, tzinfo=timezone.utc), -1000, "Кафе та ресторани")

    for _ in range(3):
        txn = await _make_pending_income(db_session, real_user, datetime(2026, 8, 15, tzinfo=timezone.utc), 250)
        await mark_reimbursement(
            txn.id, MarkReimbursementRequest(category="Кафе та ресторани"),
            current_user=real_user, db=db_session,
        )

    df = await _load_monthly_series(real_user.id, income=False, session=db_session)

    assert len(df) == 1
    assert df.iloc[0]["y"] == 250.0


# --- повернення без витрати в тому ж місяці дає від'ємний підсумок місяця, а
# не NULL, і не створює рядків для непов'язаних додатних транзакцій ----------

async def test_forecasting_coalesce_handles_reimbursement_only_month(db_session, real_user):
    # Витрата в липні, повернення (без парної витрати того ж місяця) в серпні.
    await _make_expense(db_session, real_user, datetime(2026, 7, 5, tzinfo=timezone.utc), -1000, "Кафе та ресторани")

    txn = await _make_pending_income(db_session, real_user, datetime(2026, 8, 15, tzinfo=timezone.utc), 500)
    await mark_reimbursement(
        txn.id, MarkReimbursementRequest(category="Кафе та ресторани"),
        current_user=real_user, db=db_session,
    )

    df = await _load_monthly_series(real_user.id, income=False, session=db_session)

    assert len(df) == 2
    df = df.set_index(df["ds"].dt.strftime("%Y-%m"))
    assert df.loc["2026-07", "y"] == 1000.0
    assert df.loc["2026-08", "y"] == -500.0


# --- непов'язана додатна транзакція (не reimbursement) не створює місяця з
# total = 0 -----------------------------------------------------------------

async def test_forecasting_ignores_unrelated_positive_expense_category_rows(db_session, real_user):
    txn = await _make_pending_income(db_session, real_user, datetime(2026, 9, 1, tzinfo=timezone.utc), 300)
    # Додатна сума, категорія витрат, category_method='manual' — перевіряється,
    # що фільтр враховує лише reimbursement.
    txn.category = "Транспорт"
    txn.category_method = "manual"
    await db_session.commit()

    df = await _load_monthly_series(real_user.id, income=False, session=db_session)

    assert len(df) == 0


# --- віднімання в advisor_context.build_advisor_context ------------------

async def test_advisor_context_has_no_spending_categories(db_session, real_user):
    recent = datetime.now(timezone.utc) - timedelta(days=3)
    await _make_expense(db_session, real_user, recent, -1000, "Кафе та ресторани")

    for _ in range(3):
        txn = await _make_pending_income(db_session, real_user, recent + timedelta(hours=1), 250)
        await mark_reimbursement(
            txn.id, MarkReimbursementRequest(category="Кафе та ресторани"),
            current_user=real_user, db=db_session,
        )

    text = _format_context(await build_advisor_context(real_user.id, session=db_session))

    assert "Кафе" not in text
    assert "250" not in text and "1 000" not in text
