"""
Тести оцінки «що якщо погасити борги ліквідними активами»
(advisor_context._estimate_debt_reduction_from_assets) і її подання в
контексті радника (llm_advisor._format_context).
"""
from datetime import date
import uuid

from app.api.debt import create_debt, DebtCreateRequest
from app.api.assets import create_asset, AssetCreateRequest
from app.models.forecast import Forecast
from app.services.advisor_context import _estimate_debt_reduction_from_assets
from app.services.llm_advisor import _format_context


async def _make_debt(db_session, real_user, **overrides):
    defaults = dict(
        name="МФО швидкредит", balance=10000.0, monthly_rate=0.04,
        debt_type="revolving",
    )
    defaults.update(overrides)
    return await create_debt(
        DebtCreateRequest(**defaults), current_user=real_user, db=db_session,
    )


async def _make_asset(db_session, real_user, **overrides):
    defaults = dict(
        name="Депозит ПриватБанк", asset_type="Депозит", current_amount=5000,
        liquidity_level="medium",
    )
    defaults.update(overrides)
    return await create_asset(
        AssetCreateRequest(**defaults), current_user=real_user, db=db_session,
    )


async def _make_forecast(db_session, real_user, free_cash_flow=3000.0):
    forecast = Forecast(
        id=str(uuid.uuid4()),
        user_id=real_user.id,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        predicted_income=10000.0,
        predicted_expenses=10000.0 - free_cash_flow,
        free_cash_flow=free_cash_flow,
        method="average_fallback",
        low_confidence=False,
    )
    db_session.add(forecast)
    await db_session.commit()
    return forecast


# --- _estimate_debt_reduction_from_assets --------------------------------

async def test_none_without_forecast(db_session, real_user):
    await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user)

    result = await _estimate_debt_reduction_from_assets(db_session, real_user.id, None)
    assert result is None


async def test_none_when_free_cash_flow_not_positive(db_session, real_user):
    await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user)
    forecast = await _make_forecast(db_session, real_user, free_cash_flow=0.0)

    result = await _estimate_debt_reduction_from_assets(db_session, real_user.id, forecast)
    assert result is None


async def test_none_when_no_debts(db_session, real_user):
    await _make_asset(db_session, real_user)
    forecast = await _make_forecast(db_session, real_user)

    result = await _estimate_debt_reduction_from_assets(db_session, real_user.id, forecast)
    assert result is None


async def test_none_when_no_liquid_assets(db_session, real_user):
    await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user, liquidity_level="low")
    forecast = await _make_forecast(db_session, real_user)

    result = await _estimate_debt_reduction_from_assets(db_session, real_user.id, forecast)
    assert result is None


async def test_none_when_no_active_assets(db_session, real_user):
    await _make_debt(db_session, real_user)
    forecast = await _make_forecast(db_session, real_user)

    result = await _estimate_debt_reduction_from_assets(db_session, real_user.id, forecast)
    assert result is None


async def test_applying_assets_never_increases_payoff_time(db_session, real_user):
    """Застосування активу не збільшує ні строк погашення, ні переплату."""
    await _make_debt(db_session, real_user, balance=10000.0, monthly_rate=0.04)
    await _make_asset(db_session, real_user, current_amount=5000, liquidity_level="high")
    forecast = await _make_forecast(db_session, real_user, free_cash_flow=1000.0)

    result = await _estimate_debt_reduction_from_assets(db_session, real_user.id, forecast)

    assert result is not None
    assert result["assets_applied_amount"] == 5000
    assert result["interest_saved"] >= 0
    if result["debt_payoff_months_saved"] is not None:
        assert result["debt_payoff_months_saved"] >= 0


async def test_asset_fully_covering_debt_pays_off_immediately(db_session, real_user):
    await _make_debt(db_session, real_user, balance=3000.0, monthly_rate=0.03)
    await _make_asset(db_session, real_user, current_amount=5000, liquidity_level="high")
    forecast = await _make_forecast(db_session, real_user, free_cash_flow=1000.0)

    result = await _estimate_debt_reduction_from_assets(db_session, real_user.id, forecast)

    assert result is not None
    assert result["adjusted_months_to_payoff"] == 1  # борг закрито одноразовим платежем


async def test_only_medium_and_high_liquidity_counted(db_session, real_user):
    await _make_debt(db_session, real_user, balance=10000.0, monthly_rate=0.04)
    await _make_asset(db_session, real_user, name="Готівка", current_amount=2000, liquidity_level="high")
    await _make_asset(db_session, real_user, name="Нерухомість", current_amount=500000, liquidity_level="low")
    forecast = await _make_forecast(db_session, real_user, free_cash_flow=1000.0)

    result = await _estimate_debt_reduction_from_assets(db_session, real_user.id, forecast)

    assert result is not None
    assert result["assets_applied_amount"] == 2000  # нерухомість (low) не враховується


# --- _format_context (pure, без Anthropic API) ---------------------------

def test_format_context_includes_months_saved_when_known():
    context = {"assets_applied_amount": 5000, "debt_payoff_months_saved": 3,
               "adjusted_months_to_payoff": 9, "interest_saved_by_assets": 450.0}
    text = _format_context(context)
    assert "5000 грн" in text
    assert "скоротиться на 3 міс" in text
    assert "450.0 грн" in text


def test_format_context_falls_back_to_adjusted_months_when_baseline_never_finishes():
    context = {"assets_applied_amount": 5000, "debt_payoff_months_saved": None,
               "adjusted_months_to_payoff": 20, "interest_saved_by_assets": 100.0}
    text = _format_context(context)
    assert "за 20 міс" in text


def test_format_context_omits_what_if_when_not_computed():
    text = _format_context({"assets_applied_amount": None})
    assert "активи" not in text.lower()
