"""
Тести пошуку вигідної пари «актив — борг» (advisor_context._find_asset_arbitrage),
перевірки недостатності бюджету і закріплення відповідного запитання радника.
"""
import uuid

from app.api.debt import create_debt, DebtCreateRequest
from app.api.assets import create_asset, AssetCreateRequest
from app.api.advisor import ASSET_ARBITRAGE_QUESTION, _apply_asset_arbitrage_pin
from app.services.advisor_context import _find_asset_arbitrage, _is_budget_insufficient


async def _make_debt(db_session, real_user, **overrides):
    defaults = dict(
        name="МФО швидкредит", balance=10000.0, monthly_rate=0.04,  # 4%/міс = 48%/рік
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


UNCOVERED_PLAN = {"status": "Optimal", "months_to_payoff": None}
INFEASIBLE_PLAN = {"status": "Infeasible", "months_to_payoff": None}
FULLY_COVERED_PLAN = {"status": "Optimal", "months_to_payoff": 12}


# --- _is_budget_insufficient ------------------------------------------------

def test_is_budget_insufficient_true_when_months_to_payoff_none():
    assert _is_budget_insufficient(UNCOVERED_PLAN) is True


def test_is_budget_insufficient_true_when_not_optimal():
    assert _is_budget_insufficient(INFEASIBLE_PLAN) is True


def test_is_budget_insufficient_false_when_fully_covered():
    assert _is_budget_insufficient(FULLY_COVERED_PLAN) is False


# --- _find_asset_arbitrage ----------------------------------------------------

async def test_finds_opportunity_when_asset_rate_below_debt_rate(db_session, real_user):
    await _make_debt(db_session, real_user, balance=10000.0, monthly_rate=0.04)  # 48%/рік
    await _make_asset(db_session, real_user, current_amount=5000, annual_rate=2.0)  # 2%/рік

    result = await _find_asset_arbitrage(db_session, real_user.id, UNCOVERED_PLAN)

    assert result is not None
    assert result["asset_name"] == "Депозит ПриватБанк"
    assert result["debt_name"] == "МФО швидкредит"
    assert result["withdrawable"] == 5000  # min(current_amount, balance)
    # savings = withdrawable * (0.48 - 0.02) = 5000 * 0.46 = 2300
    assert abs(result["savings"] - 2300) < 0.01


async def test_no_opportunity_when_asset_rate_not_below_debt_rate(db_session, real_user):
    # Річні ставки боргу й активу рівні (12 %) — порада не формується, зокрема
    # якщо не сплутати відсотки й частку (12.0 і 0.12).
    await _make_debt(db_session, real_user, balance=10000.0, monthly_rate=0.01)  # 12%/рік
    await _make_asset(db_session, real_user, current_amount=5000, annual_rate=12.0)  # 12%/рік

    result = await _find_asset_arbitrage(db_session, real_user.id, UNCOVERED_PLAN)
    assert result is None


async def test_no_opportunity_when_plan_is_fully_covered(db_session, real_user):
    await _make_debt(db_session, real_user, balance=10000.0, monthly_rate=0.04)
    await _make_asset(db_session, real_user, current_amount=5000, annual_rate=2.0)

    result = await _find_asset_arbitrage(db_session, real_user.id, FULLY_COVERED_PLAN)
    assert result is None


async def test_no_opportunity_when_no_milp_plan(db_session, real_user):
    await _make_debt(db_session, real_user, balance=10000.0, monthly_rate=0.04)
    await _make_asset(db_session, real_user, current_amount=5000, annual_rate=2.0)

    result = await _find_asset_arbitrage(db_session, real_user.id, {})
    assert result is None


async def test_picks_highest_savings_among_multiple_pairs(db_session, real_user):
    await _make_debt(db_session, real_user, name="Борг A", balance=10000.0, monthly_rate=0.02)  # 24%/рік
    await _make_debt(db_session, real_user, name="Борг B", balance=10000.0, monthly_rate=0.04)  # 48%/рік
    await _make_asset(db_session, real_user, name="Актив", current_amount=5000, annual_rate=2.0)

    result = await _find_asset_arbitrage(db_session, real_user.id, UNCOVERED_PLAN)

    assert result["debt_name"] == "Борг B"  # найбільша різниця ставок -> найбільша економія


async def test_withdrawable_capped_at_debt_balance(db_session, real_user):
    await _make_debt(db_session, real_user, balance=1000.0, monthly_rate=0.04)
    await _make_asset(db_session, real_user, current_amount=5000, annual_rate=2.0)

    result = await _find_asset_arbitrage(db_session, real_user.id, UNCOVERED_PLAN)
    assert result["withdrawable"] == 1000  # обмежено балансом боргу, а не сумою активу


# --- закріплення запитання в GET /advisor/questions (без Anthropic API) ---

def test_apply_asset_arbitrage_pin_prepends_and_trims():
    context = {"asset_arbitrage": True}
    questions = ["Q1?", "Q2?", "Q3?"]

    result = _apply_asset_arbitrage_pin(questions, context)

    assert result == [ASSET_ARBITRAGE_QUESTION, "Q1?", "Q2?"]
    assert len(result) == 3


def test_apply_asset_arbitrage_pin_noop_without_opportunity():
    context = {"asset_arbitrage": False}
    questions = ["Q1?", "Q2?", "Q3?"]

    result = _apply_asset_arbitrage_pin(questions, context)
    assert result == questions
