"""
Прогноз вільного грошового потоку: прогноз доходу мінус прогноз витрат.

- Дохід: лише транзакції з category IN INCOME_CATEGORIES.
- Витрати рахуються з усіх відтоків; віднімаються лише розпізнані
  переміщення грошей. Категорії потрібні лише для розбивки. Місячна сума витрат =
      categorized          усі категорії, крім EXPENSE_EXCLUDED_CATEGORIES
                           (Готівка й "Борги та кредити" — всередині), мінус
                           reimbursement-неттінг
    + pending_merchant     pending/NULL-категорія з MCC != 4829 або MCC IS NULL
                           — повністю
    + pending_p2p_net      pending/NULL-категорія з MCC 4829:
                           max(0, відтік - надходження) у межах місяця
    - debt_deduction       min(відтік "Борги та кредити", Σ мінімальних
                           платежів активних боргів) — їх платить MILP
- Вікно: лише повні місяці останнього суцільного блоку місяців з будь-якою
  транзакцією (перший місяць блоку й поточний календарний вважаються
  неповними). Місяць вікна без доходу чи витрат дорівнює 0.
- Замість Prophet береться середнє вікна (fallback_reason, у порядку
  пріоритету): partial_months_only — повних місяців немає; stale_data —
  останній повний місяць раніше за попередній календарний;
  insufficient_months — повних місяців менше за MIN_MONTHS_FOR_PROPHET.
- Prophet прогнозує на period_start (наступний календарний місяць), без
  сезонності (лише тренд). Вхід моделі вінзоризується за MAD
  (_winsorize_mad); у відповіді — фактичні суми.
"""
import uuid
from datetime import datetime, timezone, date
from dateutil.relativedelta import relativedelta

import pandas as pd
from prophet import Prophet
from sqlalchemy import select, func, or_, and_, not_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AsyncSessionLocal
from app.models.debt import Debt
from app.models.transaction import Transaction
from app.models.forecast import Forecast
from app.services.debt_rules import revolving_min_payment
from app.services.mcc_mapping import (
    INCOME_CATEGORIES,
    TRANSFER_CATEGORIES,
    CASH_CATEGORY,
    DEBT_PAYMENT_CATEGORY,
)

MIN_MONTHS_FOR_PROPHET = 3

P2P_MCC = 4829

# Категорії, що не є витратами: pending (рахується окремо, pending_*),
# перекази (TRANSFER_CATEGORIES) і дохід. Фронтенд отримує цей список як
# non_spending_categories (GET /transactions/categories).
EXPENSE_EXCLUDED_CATEGORIES = ["pending", *sorted(TRANSFER_CATEGORIES)] + list(INCOME_CATEGORIES)


async def _with_session(session: AsyncSession | None, fn):
    """Викликає fn з переданою сесією або з новою, якщо session не задано."""
    if session is not None:
        return await fn(session)
    async with AsyncSessionLocal() as own_session:
        return await fn(own_session)


async def _active_debt_min_payments_total(session: AsyncSession, user_id: str) -> float:
    """Повертає суму мінімальних платежів активних боргів за правилами debt_rules.

    Використовуються поточні борги й баланси, зокрема для минулих місяців:
    історія боргів не зберігається.
    """
    result = await session.execute(
        select(Debt.debt_type, Debt.balance, Debt.min_payment).where(
            Debt.user_id == user_id, Debt.is_active == True  # noqa: E712
        )
    )
    total = 0.0
    for debt_type, balance, min_payment in result.all():
        if debt_type == "revolving":
            total += revolving_min_payment(balance)
        else:
            total += min_payment or 0
    return total


async def _query_expense_breakdown(session: AsyncSession, user_id: str) -> dict:
    """Рахує помісячні компоненти витрат (див. докстрінг модуля) і суми за категоріями."""
    is_pending = or_(Transaction.category == "pending", Transaction.category.is_(None))
    is_categorized_expense = and_(
        not_(is_pending), Transaction.category.notin_(EXPENSE_EXCLUDED_CATEGORIES)
    )
    is_outflow = Transaction.amount_uah < 0
    is_reimbursement = and_(
        Transaction.amount_uah > 0,
        Transaction.category_method == "reimbursement",
        is_categorized_expense,
    )
    is_p2p = Transaction.mcc == P2P_MCC
    # Місяць потрапляє в ряд лише за наявності витрати чи reimbursement-рядка;
    # одне лише P2P-надходження місяця не створює.
    counts_month = or_(
        and_(is_outflow, or_(is_pending, is_categorized_expense)),
        is_reimbursement,
    )

    def outflow_sum(*conditions):
        return func.coalesce(func.sum(-Transaction.amount_uah).filter(is_outflow, *conditions), 0)

    month_expr = func.date_trunc("month", func.to_timestamp(Transaction.time))
    query = (
        select(
            month_expr.label("month"),
            outflow_sum(is_categorized_expense).label("categorized_out"),
            func.coalesce(func.sum(Transaction.amount_uah).filter(is_reimbursement), 0).label("reimbursement"),
            outflow_sum(Transaction.category == CASH_CATEGORY).label("cash"),
            outflow_sum(Transaction.category == DEBT_PAYMENT_CATEGORY).label("debt_category_out"),
            outflow_sum(is_pending, or_(Transaction.mcc.is_(None), Transaction.mcc != P2P_MCC)).label("pending_merchant"),
            outflow_sum(is_pending, is_p2p).label("pending_p2p_out"),
            func.coalesce(
                func.sum(Transaction.amount_uah).filter(Transaction.amount_uah > 0, is_pending, is_p2p), 0
            ).label("pending_p2p_in"),
            func.coalesce(
                func.sum(Transaction.amount_uah).filter(Transaction.amount_uah > 0, is_pending), 0
            ).label("pending_inflow"),
        )
        .where(Transaction.user_id == user_id)
        .group_by(month_expr)
        .having(func.count().filter(counts_month) > 0)
        .order_by(month_expr)
    )
    rows = (await session.execute(query)).all()

    debt_min_total = await _active_debt_min_payments_total(session, user_id)

    months = []
    for r in rows:
        categorized = float(r.categorized_out) - float(r.reimbursement)
        p2p_out, p2p_in = float(r.pending_p2p_out), float(r.pending_p2p_in)
        p2p_net = max(0.0, p2p_out - p2p_in)
        debt_out = float(r.debt_category_out)
        debt_deduction = min(debt_out, debt_min_total)
        months.append({
            "month": r.month.strftime("%Y-%m"),
            "categorized": categorized,
            "cash": float(r.cash),
            "pending_merchant": float(r.pending_merchant),
            "pending_p2p_out": p2p_out,
            "pending_p2p_in": p2p_in,
            "pending_p2p_net": p2p_net,
            "pending_inflow": float(r.pending_inflow),
            "debt_category_out": debt_out,
            "debt_deduction": debt_deduction,
            "debt_payments_outside_list": debt_out - debt_deduction,
            "total": categorized + float(r.pending_merchant) + p2p_net - debt_deduction,
        })

    by_category = {}
    if months:
        first_month_ts = int(
            datetime.strptime(months[0]["month"], "%Y-%m").replace(tzinfo=timezone.utc).timestamp()
        )
        category_total = (
            func.coalesce(func.sum(-Transaction.amount_uah).filter(is_outflow), 0)
            - func.coalesce(func.sum(Transaction.amount_uah).filter(is_reimbursement), 0)
        )
        category_rows = (await session.execute(
            select(Transaction.category, category_total)
            .where(
                Transaction.user_id == user_id,
                Transaction.time >= first_month_ts,
                is_categorized_expense,
                or_(is_outflow, is_reimbursement),
            )
            .group_by(Transaction.category)
            .order_by(category_total.desc())
        )).all()
        by_category = {category: float(total) for category, total in category_rows}

    return {"months": months, "by_category": by_category, "debt_min_payments_total": debt_min_total}


async def _load_expense_breakdown(user_id: str, session: AsyncSession | None = None) -> dict:
    """Повертає помісячну розбивку витрат на компоненти.

    Для кожного місяця: categorized + pending_merchant + pending_p2p_net -
    debt_deduction == total.
    """
    return await _with_session(session, lambda s: _query_expense_breakdown(s, user_id))


def _expense_series_from_breakdown(breakdown: dict) -> pd.DataFrame:
    """Перетворює розбивку витрат на ряд (ds, y) з місячними сумами total."""
    if not breakdown["months"]:
        return pd.DataFrame(columns=["ds", "y"])
    return pd.DataFrame({
        "ds": pd.to_datetime([m["month"] for m in breakdown["months"]], format="%Y-%m"),
        "y": [float(m["total"]) for m in breakdown["months"]],
    })


async def _load_monthly_series(
    user_id: str, income: bool, session: AsyncSession | None = None
) -> pd.DataFrame:
    """Повертає місячний ряд доходу або витрат.

    Повернення спільних витрат (category_method='reimbursement') віднімаються
    від витрат свого місяця; місяць лише з таким рядком дає від'ємну суму.

    Args:
        user_id: Id користувача.
        income: True — дохід (category IN INCOME_CATEGORIES); False — витрати
            (total з _load_expense_breakdown).
        session: Сесія БД; якщо не задано, відкривається власна.

    Returns:
        DataFrame з колонками ds (перший день місяця) і y (сума, грн).
    """
    if not income:
        return _expense_series_from_breakdown(await _load_expense_breakdown(user_id, session))

    month_expr = func.date_trunc("month", func.to_timestamp(Transaction.time))
    query = (
        select(month_expr.label("month"), func.sum(Transaction.amount_uah).label("total"))
        .where(
            Transaction.user_id == user_id,
            Transaction.category.in_(INCOME_CATEGORIES),
        )
        .group_by(month_expr)
        .order_by(month_expr)
    )

    async def run(s):
        """Виконує запит місячних сум доходу."""
        return (await s.execute(query)).all()

    rows = await _with_session(session, run)

    if not rows:
        return pd.DataFrame(columns=["ds", "y"])

    df = pd.DataFrame(rows, columns=["ds", "y"])
    df["ds"] = pd.to_datetime(df["ds"]).dt.tz_localize(None)
    df["y"] = df["y"].astype(float)
    return df


def _breakdown_summary(breakdown: dict, months_used: list[str]) -> dict:
    """Рахує середні за місяць показники розбивки по місяцях вікна прогнозу.

    Місяць вікна без рядків у розбивці дає 0; без місяців — None.
    uncategorized_* — увесь рух без категорії (мерчанти, P2P, без MCC) до
    неттінгу.
    """
    by_month = {m["month"]: m for m in breakdown["months"]}

    def avg(value_of):
        """Середнє value_of по months_used або None."""
        if not months_used:
            return None
        values = [value_of(by_month[m]) if m in by_month else 0.0 for m in months_used]
        return round(sum(values) / len(values), 2)

    return {
        "uncategorized_outflow_avg_per_month": avg(lambda m: m["pending_merchant"] + m["pending_p2p_out"]),
        "uncategorized_inflow_avg_per_month": avg(lambda m: m["pending_inflow"]),
        "debt_payments_outside_list_avg_per_month": avg(lambda m: m["debt_payments_outside_list"]),
    }


def _month_key(month: date) -> str:
    """Форматує місяць як 'YYYY-MM'."""
    return month.strftime("%Y-%m")


async def _load_coverage(session: AsyncSession, user_id: str) -> tuple[list[date], int | None]:
    """Повертає календарні місяці з будь-якою транзакцією користувача і час останньої транзакції."""
    month_expr = func.date_trunc("month", func.to_timestamp(Transaction.time))
    rows = (await session.execute(
        select(month_expr, func.max(Transaction.time))
        .where(Transaction.user_id == user_id)
        .group_by(month_expr)
        .order_by(month_expr)
    )).all()
    months = [pd.Timestamp(m).date().replace(day=1) for m, _ in rows]
    last_ts = max((ts for _, ts in rows), default=None)
    return months, last_ts


def _select_window(coverage: list[date], today: date) -> dict:
    """Обирає місяці вікна прогнозу й причину fallback (див. докстрінг модуля).

    Args:
        coverage: Місяці з будь-якою транзакцією, за зростанням.
        today: Поточна дата.

    Returns:
        Словник months, partial_months_only, data_stale, fallback_reason.
    """
    current = today.replace(day=1)
    previous = current - relativedelta(months=1)
    months = [m for m in coverage if m <= current]
    if not months:
        return {"months": [], "partial_months_only": True, "data_stale": False,
                "fallback_reason": "partial_months_only"}

    block = [months[-1]]
    for m in reversed(months[:-1]):
        if m != block[0] - relativedelta(months=1):
            break
        block.insert(0, m)

    full = [m for m in block[1:] if m != current]
    partial_only = not full
    used = block if partial_only else full
    data_stale = used[-1] < previous

    if partial_only:
        reason = "partial_months_only"
    elif data_stale:
        reason = "stale_data"
    elif len(full) < MIN_MONTHS_FOR_PROPHET:
        reason = "insufficient_months"
    else:
        reason = None

    return {"months": used, "partial_months_only": partial_only, "data_stale": data_stale,
            "fallback_reason": reason}


def _winsorize_mad(values: list[float], k: float = 3.0) -> list[float]:
    """Притискає значення до меж median ± k·1.4826·MAD.

    Для менш ніж 3 значень або MAD = 0 повертає значення без змін.
    """
    if len(values) < MIN_MONTHS_FOR_PROPHET:
        return list(values)
    series = pd.Series(values, dtype=float)
    median = series.median()
    mad = (series - median).abs().median()
    if mad == 0:
        return list(values)
    bound = k * 1.4826 * mad
    return [float(v) for v in series.clip(lower=median - bound, upper=median + bound)]


def _run_prophet(df: pd.DataFrame, target: date) -> float:
    """Прогнозує значення ряду на місяць target моделлю Prophet.

    Кількість кроків — місяці від останньої точки ряду до target. Від'ємний
    прогноз замінюється нулем.
    """
    model = Prophet(
        yearly_seasonality=False,
        weekly_seasonality=False,
        daily_seasonality=False,
        seasonality_mode="additive",
        growth="linear",
        interval_width=0.80,
        changepoint_prior_scale=0.05,
    )
    model.fit(df)

    last = df["ds"].max()
    target_ts = pd.Timestamp(target)
    periods = (target_ts.year - last.year) * 12 + (target_ts.month - last.month)
    future = model.make_future_dataframe(periods=max(periods, 1), freq="MS")
    forecast = model.predict(future)
    yhat = forecast.loc[forecast["ds"] == target_ts, "yhat"]
    return float(max(yhat.iloc[0], 0.0))


async def generate_forecast(
    user_id: str, session: AsyncSession | None = None, today: date | None = None
) -> Forecast:
    """Рахує прогноз доходу й витрат на наступний календарний місяць і зберігає його.

    Вікно, причини fallback і розбивка витрат записуються в Forecast.raw_output.

    Args:
        user_id: Id користувача.
        session: Сесія БД; якщо не задано, відкривається власна.
        today: Поточна дата; за замовчуванням date.today().

    Returns:
        Збережений Forecast.
    """
    today = today or date.today()
    return await _with_session(session, lambda s: _generate_forecast(s, user_id, today))


async def _generate_forecast(session: AsyncSession, user_id: str, today: date) -> Forecast:
    """Рахує й зберігає прогноз у межах переданої сесії."""
    income_df = await _load_monthly_series(user_id, income=True, session=session)
    breakdown = await _load_expense_breakdown(user_id, session=session)
    coverage, last_ts = await _load_coverage(session, user_id)
    window = _select_window(coverage, today)
    months_used = [_month_key(m) for m in window["months"]]

    income_by_month = {_month_key(ds): float(y) for ds, y in zip(income_df["ds"], income_df["y"])}
    expense_by_month = {m["month"]: float(m["total"]) for m in breakdown["months"]}
    actual = {
        "income": {m: income_by_month.get(m, 0.0) for m in months_used},
        "expenses": {m: expense_by_month.get(m, 0.0) for m in months_used},
    }

    period_start = today.replace(day=1) + relativedelta(months=1)
    period_end = period_start + relativedelta(months=1) - relativedelta(days=1)

    use_prophet = window["fallback_reason"] is None
    model_input = {}
    predictions = {}
    for name, label in (("income", "ДОХОДУ"), ("expenses", "ВИТРАТ")):
        values = list(actual[name].values())
        if use_prophet:
            model_input[name] = dict(zip(months_used, _winsorize_mad(values)))
            df = pd.DataFrame({
                "ds": pd.to_datetime(months_used, format="%Y-%m"),
                "y": list(model_input[name].values()),
            })
            try:
                predictions[name] = _run_prophet(df, period_start)
            except Exception as e:
                raise RuntimeError(f"Prophet впав на прогнозі {label} ({len(df)} місяців даних): {e}") from e
        else:
            model_input[name] = dict(actual[name])
            predictions[name] = sum(values) / len(values) if values else 0.0

    predicted_income, predicted_expenses = predictions["income"], predictions["expenses"]
    low_confidence = not use_prophet
    method = "prophet" if use_prophet else "average_fallback"
    free_cash_flow = predicted_income - predicted_expenses

    last_transaction_at = (
        datetime.fromtimestamp(last_ts, tz=timezone.utc) if last_ts is not None else None
    )

    forecast = Forecast(
        id=str(uuid.uuid4()),
        user_id=user_id,
        period_start=period_start,
        period_end=period_end,
        predicted_income=round(predicted_income, 2),
        predicted_expenses=round(predicted_expenses, 2),
        free_cash_flow=round(free_cash_flow, 2),
        method=method,
        low_confidence=low_confidence,
        raw_output={
            "income_months_available": len(months_used),
            "expense_months_available": len(months_used),
            "income_low_confidence": low_confidence,
            "expense_low_confidence": low_confidence,
            "months_used": months_used,
            "fallback_reason": window["fallback_reason"],
            "partial_months_only": window["partial_months_only"],
            "data_stale": window["data_stale"],
            "last_transaction_at": last_transaction_at.isoformat() if last_transaction_at else None,
            "data_stale_days": (today - last_transaction_at.date()).days if last_transaction_at else None,
            "series_actual": actual,
            "series_model_input": model_input,
            **_breakdown_summary(breakdown, months_used),
            "expense_breakdown": breakdown,
        },
    )

    session.add(forecast)
    await session.commit()
    await session.refresh(forecast)

    return forecast
