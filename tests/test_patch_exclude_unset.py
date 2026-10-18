"""
Тести PATCH активів, боргів і позик: явно передане null очищує опційне
поле (model_dump(exclude_unset=True)), а не передані поля не змінюються.
"""
from datetime import date

from app.api.assets import create_asset, AssetCreateRequest, update_asset, AssetUpdateRequest
from app.api.debt import create_debt, DebtCreateRequest, update_debt, DebtUpdateRequest
from app.api.loans import create_loan, LoanCreateRequest, update_loan, LoanUpdateRequest


async def test_patch_can_clear_optional_asset_fields(db_session, real_user):
    asset = await create_asset(
        AssetCreateRequest(
            name="Депозит ПриватБанк", asset_type="Депозит", current_amount=10000,
            liquidity_level="medium", liquidity_days=30, annual_rate=2.0,
            rate_note="плаваюча ставка", opened_at=date(2026, 1, 1),
        ),
        current_user=real_user, db=db_session,
    )
    assert asset.annual_rate == 2.0  # перевірка перед очищенням

    updated = await update_asset(
        asset.id,
        AssetUpdateRequest(
            liquidity_days=None, annual_rate=None, rate_note=None, opened_at=None,
        ),
        current_user=real_user, db=db_session,
    )

    assert updated.liquidity_days is None
    assert updated.annual_rate is None
    assert updated.rate_note is None
    assert updated.opened_at is None
    # поля, які не передавались у PATCH, лишаються незмінними
    assert updated.name == "Депозит ПриватБанк"
    assert updated.current_amount == 10000


async def test_patch_asset_partial_update_still_ignores_omitted_fields(db_session, real_user):
    """PATCH лише current_amount не змінює інших полів активу."""
    asset = await create_asset(
        AssetCreateRequest(
            name="Депозит ПриватБанк", asset_type="Депозит", current_amount=10000,
            liquidity_level="medium", annual_rate=2.0,
        ),
        current_user=real_user, db=db_session,
    )

    updated = await update_asset(
        asset.id, AssetUpdateRequest(current_amount=15000),
        current_user=real_user, db=db_session,
    )

    assert updated.current_amount == 15000
    assert updated.annual_rate == 2.0  # не передавалось -> незмінне


async def test_patch_can_clear_debt_min_payment(db_session, real_user):
    debt = await create_debt(
        DebtCreateRequest(
            name="Кредитна картка", balance=50000.0, monthly_rate=0.02,
            min_payment=1500.0, debt_type="revolving",
        ),
        current_user=real_user, db=db_session,
    )
    assert debt.min_payment == 1500.0

    updated = await update_debt(
        debt.id, DebtUpdateRequest(min_payment=None),
        current_user=real_user, db=db_session,
    )

    assert updated.min_payment is None
    assert updated.balance == 50000.0  # не передавалось -> незмінне


async def test_patch_loan_partial_update_unaffected(db_session, real_user):
    """PATCH одного поля позики не змінює решти."""
    loan = await create_loan(
        LoanCreateRequest(
            borrower_name="Іван П.", principal_amount=5000,
            issued_at=date(2026, 7, 1),
        ),
        current_user=real_user, db=db_session,
    )

    updated = await update_loan(
        loan.id, LoanUpdateRequest(borrower_name="Петро І."),
        current_user=real_user, db=db_session,
    )

    assert updated.borrower_name == "Петро І."
    assert updated.principal_amount == 5000
    assert updated.remaining_amount == 5000
    assert updated.status == "not_repaid"
