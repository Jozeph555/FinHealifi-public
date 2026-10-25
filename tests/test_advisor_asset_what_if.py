"""
Тести сценарію «якби спрямувати заощадження на борги»
(advisor_context._estimate_debt_reduction_from_assets), його речень у
контексті радника і відсутності прогнозу (Forecast) у контексті.

Бюджет сценарію — бюджет збереженого плану: сума мінімальних платежів +
extra_monthly_uah. Без плану, із застарілим планом або з розбіжністю
базового прогону й плану сценарій не рахується.
Плани будуються справжнім POST /debt/optimize (PuLP + CBC).
"""
import logging
import uuid
from datetime import date

from sqlalchemy import select

from app.api.assets import AssetCreateRequest, create_asset
from app.api.debt import DebtCreateRequest, ExtraPaymentRequest, create_debt, optimize_with_extra
from app.models.forecast import Forecast
from app.models.milp_plan import MilpPlan
import app.services.advisor_context as advisor_context
from app.services.advisor_context import (
    INCONSISTENT_PLAN_REASON,
    NO_PLAN_REASON,
    PLAN_UNAVAILABLE_SENTENCE,
    STALE_PLAN_REASON,
    UNKNOWN_BUDGET_REASON,
    _estimate_debt_reduction_from_assets,
    _what_if_sentences,
    build_advisor_context,
)
from app.services.llm_advisor import _format_context
from app.services.milp_optimizer import optimize_milp


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


async def _make_plan(db_session, real_user, extra=1000.0):
    return await optimize_with_extra(
        ExtraPaymentRequest(extra_monthly_uah=extra), current_user=real_user, db=db_session, months=36,
    )


async def _latest_plan(db_session, real_user) -> MilpPlan:
    return (await db_session.execute(
        select(MilpPlan).where(MilpPlan.user_id == real_user.id).order_by(MilpPlan.generated_at.desc()).limit(1)
    )).scalar_one()


async def _make_forecast(db_session, real_user):
    forecast = Forecast(
        id=str(uuid.uuid4()),
        user_id=real_user.id,
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        predicted_income=987654.32,
        predicted_expenses=123456.78,
        free_cash_flow=864197.54,
        method="average_fallback",
        low_confidence=True,
    )
    db_session.add(forecast)
    await db_session.commit()
    return forecast


# --- Причини, коли сценарій не рахується --------------------------------

async def test_no_plan_gives_reason(db_session, real_user):
    await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user)

    result, reason = await _estimate_debt_reduction_from_assets(db_session, real_user.id, None)
    assert result is None
    assert reason == NO_PLAN_REASON


async def test_stale_plan_gives_reason(db_session, real_user):
    debt = await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user)
    await _make_plan(db_session, real_user)
    plan = await _latest_plan(db_session, real_user)

    debt.balance = 9000.0
    await db_session.commit()

    result, reason = await _estimate_debt_reduction_from_assets(db_session, real_user.id, plan)
    assert result is None
    assert reason == STALE_PLAN_REASON


async def test_plan_without_extra_gives_reason(db_session, real_user):
    await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user)
    await _make_plan(db_session, real_user)
    plan = await _latest_plan(db_session, real_user)
    plan.extra_monthly_uah = None
    await db_session.commit()

    result, reason = await _estimate_debt_reduction_from_assets(db_session, real_user.id, plan)
    assert result is None
    assert reason == UNKNOWN_BUDGET_REASON


async def test_context_without_plan_is_single_sentence(db_session, real_user):
    await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user)

    context = await build_advisor_context(real_user.id, session=db_session)
    assert context["sentences"] == [PLAN_UNAVAILABLE_SENTENCE]


async def test_context_with_stale_plan_is_single_sentence(db_session, real_user):
    debt = await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user)
    await _make_plan(db_session, real_user)
    debt.monthly_rate = 0.05
    await db_session.commit()

    context = await build_advisor_context(real_user.id, session=db_session)
    assert context["sentences"] == [PLAN_UNAVAILABLE_SENTENCE]


async def test_no_reason_when_no_debts(db_session, real_user):
    await _make_asset(db_session, real_user)

    assert await _estimate_debt_reduction_from_assets(db_session, real_user.id, None) == (None, None)


async def test_no_reason_when_no_liquid_assets(db_session, real_user):
    await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user, liquidity_level="low")

    assert await _estimate_debt_reduction_from_assets(db_session, real_user.id, None) == (None, None)


async def test_no_reason_when_no_assets(db_session, real_user):
    await _make_debt(db_session, real_user)

    assert await _estimate_debt_reduction_from_assets(db_session, real_user.id, None) == (None, None)


# --- Бюджет і узгодженість з планом -------------------------------------

async def test_what_if_budget_is_plan_minimums_plus_extra(db_session, real_user, monkeypatch):
    """Бюджет обох прогонів = мінімальні платежі (4 % від 10 000) + extra плану."""
    await _make_debt(db_session, real_user, balance=10000.0, monthly_rate=0.04)
    await _make_asset(db_session, real_user, current_amount=5000, liquidity_level="high")
    response = await _make_plan(db_session, real_user, extra=1000.0)
    plan = await _latest_plan(db_session, real_user)

    budgets = []

    def _recording_milp(debts, monthly_budget, months=36):
        budgets.append(monthly_budget)
        return optimize_milp(debts, monthly_budget, months)

    monkeypatch.setattr(advisor_context, "optimize_milp", _recording_milp)
    result, reason = await _estimate_debt_reduction_from_assets(db_session, real_user.id, plan)

    assert reason is None
    assert response["total_monthly_payment_uah"] == 1400.0
    assert budgets == [1400.0, 1400.0]
    assert result["monthly_budget"] == 1400.0


async def test_baseline_matches_saved_plan(db_session, real_user):
    await _make_debt(db_session, real_user, balance=10000.0, monthly_rate=0.04)
    await _make_debt(db_session, real_user, name="Розстрочка", balance=6000.0, monthly_rate=0.0,
                     debt_type="installment", min_payment=500.0)
    await _make_asset(db_session, real_user, current_amount=3000, liquidity_level="high")
    await _make_plan(db_session, real_user, extra=800.0)
    plan = await _latest_plan(db_session, real_user)

    result, reason = await _estimate_debt_reduction_from_assets(db_session, real_user.id, plan)

    assert reason is None
    debts = [
        {"name": "МФО швидкредит", "balance": 10000.0, "monthly_rate": 0.04, "min_payment": None,
         "debt_type": "revolving"},
        {"name": "Розстрочка", "balance": 6000.0, "monthly_rate": 0.0, "min_payment": 500.0,
         "debt_type": "installment"},
    ]
    baseline = optimize_milp(debts, result["monthly_budget"], advisor_context.WHAT_IF_HORIZON_MONTHS)
    assert abs(baseline["total_interest_paid"] - plan.total_interest_paid) <= 1.0


async def test_mismatch_with_plan_hides_what_if_and_logs(db_session, real_user, caplog):
    await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user)
    await _make_plan(db_session, real_user)
    plan = await _latest_plan(db_session, real_user)
    plan.total_interest_paid += 5.0
    await db_session.commit()

    with caplog.at_level(logging.WARNING, logger="app.services.advisor_context"):
        result, reason = await _estimate_debt_reduction_from_assets(db_session, real_user.id, plan)

    assert result is None
    assert reason == INCONSISTENT_PLAN_REASON
    assert "базовий прогін" in caplog.text


# --- Властивості сценарію ------------------------------------------------

async def test_applying_assets_never_increases_payoff_time(db_session, real_user):
    """Застосування активу не збільшує ні строк погашення, ні переплату."""
    await _make_debt(db_session, real_user, balance=10000.0, monthly_rate=0.04)
    await _make_asset(db_session, real_user, current_amount=5000, liquidity_level="high")
    await _make_plan(db_session, real_user, extra=600.0)
    plan = await _latest_plan(db_session, real_user)

    result, _ = await _estimate_debt_reduction_from_assets(db_session, real_user.id, plan)

    assert result is not None
    assert result["assets_applied_amount"] == 5000
    assert result["interest_saved"] >= 0
    if result["debt_payoff_months_saved"] is not None:
        assert result["debt_payoff_months_saved"] >= 0


async def test_asset_fully_covering_debt_pays_off_immediately(db_session, real_user):
    await _make_debt(db_session, real_user, balance=3000.0, monthly_rate=0.03)
    await _make_asset(db_session, real_user, current_amount=5000, liquidity_level="high")
    await _make_plan(db_session, real_user, extra=600.0)
    plan = await _latest_plan(db_session, real_user)

    result, _ = await _estimate_debt_reduction_from_assets(db_session, real_user.id, plan)

    assert result is not None
    assert result["adjusted_months_to_payoff"] == 1  # борг закрито одноразовим платежем


async def test_only_medium_and_high_liquidity_counted(db_session, real_user):
    await _make_debt(db_session, real_user, balance=10000.0, monthly_rate=0.04)
    await _make_asset(db_session, real_user, name="Готівка", current_amount=2000, liquidity_level="high")
    await _make_asset(db_session, real_user, name="Нерухомість", current_amount=500000, liquidity_level="low")
    await _make_plan(db_session, real_user, extra=600.0)
    plan = await _latest_plan(db_session, real_user)

    result, _ = await _estimate_debt_reduction_from_assets(db_session, real_user.id, plan)

    assert result is not None
    assert result["assets_applied_amount"] == 2000  # нерухомість (low) не враховується


# --- Прогноз не потрапляє в контекст ------------------------------------

async def test_forecast_row_not_used_in_context(db_session, real_user):
    """Рядок Forecast у БД є, але текст для моделі його чисел не містить."""
    await _make_debt(db_session, real_user)
    await _make_asset(db_session, real_user)
    await _make_plan(db_session, real_user, extra=1000.0)
    await _make_forecast(db_session, real_user)

    context = await build_advisor_context(real_user.id, session=db_session,
                                          question="Чи варто закрити борг заощадженнями?")
    text = _format_context(context)

    assert "Щомісяця на борги йде 1 400 грн: 400 грн обов'язкових платежів і 1 000 грн понад них." in text
    assert "Якби одноразово спрямувати 5 000 грн заощаджень" in text
    for number in ("987", "123 456", "864", "987654", "123456", "864197"):
        assert number not in text
    assert "прогноз" not in text.lower()


# --- Речення сценарію (pure, без Anthropic API) ---------------------------

def test_what_if_sentences_are_conditional_with_reserve():
    sentences = _what_if_sentences({"assets_applied_amount": 5000.0, "debt_payoff_months_saved": 3,
                                    "adjusted_months_to_payoff": 9, "interest_saved": 450.4,
                                    "monthly_budget": 1400.0})
    assert sentences[0] == (
        "Якби одноразово спрямувати 5 000 грн заощаджень на борги сьогодні, борги закрилися б через 9 міс. "
        "(на 3 міс. раніше), а переплата зменшилась би на 450 грн."
    )
    assert sentences[1] == "Це лише сценарій «якби», а не порада."
    assert "резерві" in sentences[2]


def test_what_if_sentences_when_still_not_paid_off():
    sentences = _what_if_sentences({"assets_applied_amount": 5000.0, "debt_payoff_months_saved": None,
                                    "adjusted_months_to_payoff": None, "interest_saved": 100.0,
                                    "monthly_budget": 1400.0})
    assert "за 36 міс. борги все одно не закрилися б." in sentences[0]
    assert "переплата" not in sentences[0]
