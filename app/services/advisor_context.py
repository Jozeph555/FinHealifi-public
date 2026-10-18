"""
Контекст для AI-радника, зібраний з БД на боці сервера.

У контекст потрапляють лише агреговані показники: суми, кількість місяців,
назви категорій. Описи транзакцій, імена отримувачів, номери карток та інші
персональні дані в Anthropic API не передаються. Усі числа, які пояснює
модель, рахуються тут, зокрема двома запусками MILP-солвера.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AsyncSessionLocal
from app.models.forecast import Forecast
from app.models.milp_plan import MilpPlan
from app.models.debt import Debt
from app.models.asset import Asset
from app.models.transaction import Transaction
from app.services.forecasting import EXPENSE_EXCLUDED_CATEGORIES
from app.services.mcc_mapping import CASH_CATEGORY
from app.services.milp_optimizer import optimize_milp

WHAT_IF_HORIZON_MONTHS = 36
# Рівні ліквідності активів, які можна застосувати до боргів одразу.
WHAT_IF_LIQUID_LEVELS = ("high", "medium")

LIQUIDITY_LABELS = {"high": "висока ліквідність", "medium": "середня ліквідність", "low": "низька ліквідність"}


def _is_budget_insufficient(milp_data: dict) -> bool:
    """Перевіряє, чи бюджет не покриває MILP-план.

    Бюджет недостатній, якщо солвер не знайшов оптимального рішення або план не
    погашає борги за горизонт. Читає сирий plan_data["milp"]: колонка
    MilpPlan.months_to_payoff ніколи не буває None.
    """
    return milp_data.get("status") != "Optimal" or milp_data.get("months_to_payoff") is None


def _liquidity_note(opportunity: dict) -> str:
    """Формує підпис ліквідності активу: рівень, строк доступу, примітка до ставки."""
    parts = [LIQUIDITY_LABELS.get(opportunity["liquidity_level"], opportunity["liquidity_level"])]
    if opportunity["liquidity_days"] is not None:
        parts.append(f"{opportunity['liquidity_days']} дн. до доступу")
    if opportunity["rate_note"]:
        parts.append(opportunity["rate_note"])
    return ", ".join(parts)


async def _find_asset_arbitrage(session: AsyncSession, user_id: str, milp_data: dict) -> dict | None:
    """Шукає найвигіднішу пару «актив — борг» для дострокового погашення.

    Працює, лише коли бюджет не покриває MILP-план. Розглядає активи зі
    ставкою, нижчою за річну ставку боргу, і обирає пару з найбільшою річною
    економією на відсотках.

    Returns:
        Словник з назвами активу й боргу, сумою зняття, економією і даними про
        ліквідність, або None.
    """
    if not milp_data or not _is_budget_insufficient(milp_data):
        return None

    assets_result = await session.execute(
        select(Asset).where(
            Asset.user_id == user_id, Asset.is_active == True, Asset.annual_rate.isnot(None)
        )
    )
    assets = assets_result.scalars().all()
    if not assets:
        return None

    debts_result = await session.execute(
        select(Debt).where(Debt.user_id == user_id, Debt.is_active == True)
    )
    debts = debts_result.scalars().all()

    best = None
    for asset in assets:
        asset_annual = asset.annual_rate / 100
        for debt in debts:
            if debt.balance <= 0:
                continue
            debt_annual = debt.monthly_rate * 12
            if asset_annual >= debt_annual:
                continue
            withdrawable = min(asset.current_amount, debt.balance)
            savings = withdrawable * (debt_annual - asset_annual)
            if best is None or savings > best["savings"]:
                best = {
                    "asset_name": asset.name,
                    "debt_name": debt.name,
                    "withdrawable": withdrawable,
                    "savings": savings,
                    "liquidity_level": asset.liquidity_level,
                    "liquidity_days": asset.liquidity_days,
                    "rate_note": asset.rate_note,
                }
    return best


async def _estimate_debt_reduction_from_assets(
    session: AsyncSession, user_id: str, forecast: Forecast | None
) -> dict | None:
    """Оцінює ефект одноразового погашення боргів ліквідними активами.

    Сума активів з ліквідністю high/medium застосовується до боргів у порядку
    спадання ставки. MILP-солвер запускається двічі — на поточних балансах і на
    зменшених — з бюджетом forecast.free_cash_flow; результат — різниця.

    Returns:
        Словник з сумою застосованих активів, скороченням строку погашення,
        новим строком і економією на відсотках, або None, якщо оцінка неможлива.
    """
    if not forecast or forecast.free_cash_flow is None or forecast.free_cash_flow <= 0:
        return None

    assets_result = await session.execute(
        select(Asset).where(
            Asset.user_id == user_id,
            Asset.is_active == True,
            Asset.liquidity_level.in_(WHAT_IF_LIQUID_LEVELS),
        )
    )
    total_applicable = sum(a.current_amount for a in assets_result.scalars().all())
    if total_applicable <= 0:
        return None

    debts_result = await session.execute(
        select(Debt).where(Debt.user_id == user_id, Debt.is_active == True)
    )
    debts = debts_result.scalars().all()
    if not debts:
        return None

    def _to_dict(d: Debt) -> dict:
        return {
            "name": d.name,
            "balance": d.balance,
            "monthly_rate": d.monthly_rate,
            "min_payment": d.min_payment,
            "debt_type": d.debt_type,
        }

    baseline_debts = [_to_dict(d) for d in debts]
    adjusted_debts = [_to_dict(d) for d in debts]

    # Сума розподіляється від боргу з найвищою ставкою, доки не вичерпається.
    remaining = total_applicable
    for d in sorted(adjusted_debts, key=lambda d: d["monthly_rate"], reverse=True):
        if remaining <= 0:
            break
        pay = min(remaining, d["balance"])
        d["balance"] -= pay
        remaining -= pay

    try:
        baseline = optimize_milp(baseline_debts, forecast.free_cash_flow, WHAT_IF_HORIZON_MONTHS)
        adjusted = optimize_milp(adjusted_debts, forecast.free_cash_flow, WHAT_IF_HORIZON_MONTHS)
    except Exception:
        return None
    if baseline["status"] != "Optimal" or adjusted["status"] != "Optimal":
        return None

    months_saved = None
    if baseline["months_to_payoff"] is not None and adjusted["months_to_payoff"] is not None:
        months_saved = baseline["months_to_payoff"] - adjusted["months_to_payoff"]

    return {
        "assets_applied_amount": round(total_applicable),
        "debt_payoff_months_saved": months_saved,
        "adjusted_months_to_payoff": adjusted["months_to_payoff"],
        "interest_saved": round(baseline["total_interest_paid"] - adjusted["total_interest_paid"], 2),
    }


async def build_advisor_context(user_id: str, session: AsyncSession | None = None) -> dict:
    """Збирає агрегований контекст користувача для AI-радника.

    Args:
        user_id: Id користувача.
        session: Сесія БД; якщо не задано, відкривається власна.

    Returns:
        Словник показників: борг, бюджет, план MILP, найбільша категорія витрат
        за 30 днів, зняття готівки, оцінки щодо активів.
    """
    if session is not None:
        return await _build_context(session, user_id)
    async with AsyncSessionLocal() as own_session:
        return await _build_context(own_session, user_id)


async def _build_context(session: AsyncSession, user_id: str) -> dict:
    """Рахує показники контексту радника в межах переданої сесії."""
    forecast_result = await session.execute(
        select(Forecast)
        .where(Forecast.user_id == user_id)
        .order_by(Forecast.generated_at.desc())
        .limit(1)
    )
    forecast = forecast_result.scalar_one_or_none()

    plan_result = await session.execute(
        select(MilpPlan)
        .where(MilpPlan.user_id == user_id)
        .order_by(MilpPlan.generated_at.desc())
        .limit(1)
    )
    plan = plan_result.scalar_one_or_none()

    debts_result = await session.execute(
        select(func.sum(Debt.balance)).where(
            Debt.user_id == user_id, Debt.is_active == True
        )
    )
    total_debt = debts_result.scalar() or 0

    # Найбільша категорія витрат за останні 30 днів. Повернення спільних
    # витрат (category_method='reimbursement') віднімаються від витрат своєї
    # категорії, як і в прогнозі.
    thirty_days_ago = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    expense_sum = func.coalesce(
        func.sum(func.abs(Transaction.amount_uah)).filter(Transaction.amount_uah < 0), 0
    )
    reimbursement_sum = func.coalesce(
        func.sum(Transaction.amount_uah).filter(
            Transaction.amount_uah > 0,
            Transaction.category_method == "reimbursement",
        ),
        0,
    )
    category_result = await session.execute(
        select(Transaction.category, (expense_sum - reimbursement_sum).label("total"))
        .where(
            Transaction.user_id == user_id,
            Transaction.time >= thirty_days_ago,
            Transaction.category.notin_(EXPENSE_EXCLUDED_CATEGORIES),
            # Готівка рахується окремо (cash_withdrawn нижче).
            Transaction.category != CASH_CATEGORY,
            Transaction.category.isnot(None),
            or_(
                Transaction.amount_uah < 0,
                and_(
                    Transaction.amount_uah > 0,
                    Transaction.category_method == "reimbursement",
                ),
            ),
        )
        .group_by(Transaction.category)
        .order_by((expense_sum - reimbursement_sum).desc())
        .limit(1)
    )
    top_category_row = category_result.first()

    cash_result = await session.execute(
        select(func.coalesce(func.sum(-Transaction.amount_uah), 0)).where(
            Transaction.user_id == user_id,
            Transaction.time >= thirty_days_ago,
            Transaction.category == CASH_CATEGORY,
            Transaction.amount_uah < 0,
        )
    )
    cash_withdrawn = float(cash_result.scalar())

    milp_data = plan.plan_data.get("milp", {}) if plan and plan.plan_data else {}

    best_strategy = None
    savings_vs_avalanche = None
    if plan and plan.plan_data:
        strategies = plan.plan_data
        milp_interest = milp_data.get("total_interest_paid")
        avalanche_interest = strategies.get("avalanche", {}).get("total_interest_paid")
        if milp_interest is not None and avalanche_interest is not None and avalanche_interest > 0:
            best_strategy = "milp"
            savings_vs_avalanche = round(avalanche_interest - milp_interest, 2)

    asset_arbitrage = await _find_asset_arbitrage(session, user_id, milp_data)
    asset_what_if = await _estimate_debt_reduction_from_assets(session, user_id, forecast)

    return {
        "total_debt": round(total_debt, 2),
        "monthly_budget_for_debt": forecast.free_cash_flow if forecast else None,
        "predicted_income": forecast.predicted_income if forecast else None,
        "predicted_expenses": forecast.predicted_expenses if forecast else None,
        "forecast_low_confidence": forecast.low_confidence if forecast else None,
        "best_strategy": best_strategy,
        "months_to_payoff": plan.months_to_payoff if plan else None,
        "total_interest_paid": plan.total_interest_paid if plan else None,
        "savings_vs_avalanche": savings_vs_avalanche,
        "top_spending_category": top_category_row[0] if top_category_row else None,
        "top_spending_category_amount": round(top_category_row[1], 2) if top_category_row else None,
        "cash_withdrawn_last_30_days": round(cash_withdrawn, 2),
        "asset_arbitrage_asset_name": asset_arbitrage["asset_name"] if asset_arbitrage else None,
        "asset_arbitrage_debt_name": asset_arbitrage["debt_name"] if asset_arbitrage else None,
        "asset_arbitrage_amount": round(asset_arbitrage["withdrawable"]) if asset_arbitrage else None,
        "asset_arbitrage_annual_savings": round(asset_arbitrage["savings"]) if asset_arbitrage else None,
        "asset_arbitrage_liquidity_note": _liquidity_note(asset_arbitrage) if asset_arbitrage else None,
        "assets_applied_amount": asset_what_if["assets_applied_amount"] if asset_what_if else None,
        "debt_payoff_months_saved": asset_what_if["debt_payoff_months_saved"] if asset_what_if else None,
        "adjusted_months_to_payoff": asset_what_if["adjusted_months_to_payoff"] if asset_what_if else None,
        "interest_saved_by_assets": asset_what_if["interest_saved"] if asset_what_if else None,
    }
