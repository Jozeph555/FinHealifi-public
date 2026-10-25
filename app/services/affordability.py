"""
Історичний вільний грошовий потік користувача (калькулятор спроможності).

Лише читає дані. Вільний потік місяця — дохід мінус витрати (мінімальні
платежі за боргами вже вилучені з витрат). Вікно повних місяців обирається
так само, як для ряду доходів і витрат у forecasting (_select_window).
Прогнозу не будує.
"""
import statistics
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.forecasting import (
    _load_coverage,
    _load_expense_breakdown,
    _load_monthly_series,
    _month_key,
    _select_window,
)

MIN_MONTHS_FOR_SUMMARY = 3


async def historical_free_cash_flow(
    session: AsyncSession, user_id: str, extra: float, months: int = 12, today: date | None = None
) -> dict:
    """Рахує вільний потік за останні `months` повних місяців і підсумки по ньому.

    Args:
        session: Сесія БД.
        user_id: Id користувача.
        extra: Додатковий платіж, грн; рахується, скільки місяців мали потік не менший.
        months: Скільки останніх повних місяців брати.
        today: Поточна дата; за замовчуванням date.today().

    Returns:
        Словник: extra_uah, n_months_requested, n_months_used, months (помісячний
        ряд fcf_uah), count_ge_extra, share, min, max, median, p25, p75 (None,
        якщо повних місяців менше MIN_MONTHS_FOR_SUMMARY; квартилі — лінійна
        інтерполяція), data_stale і explanation.
    """
    today = today or date.today()
    income_df = await _load_monthly_series(user_id, income=True, session=session)
    breakdown = await _load_expense_breakdown(user_id, session=session)
    coverage, _ = await _load_coverage(session, user_id)
    window = _select_window(coverage, today)

    window_months = [] if window["partial_months_only"] else [_month_key(m) for m in window["months"]]
    used = window_months[-months:]
    income = {_month_key(ds): float(y) for ds, y in zip(income_df["ds"], income_df["y"])}
    expenses = {m["month"]: float(m["total"]) for m in breakdown["months"]}
    series = [{"month": m, "fcf_uah": income.get(m, 0.0) - expenses.get(m, 0.0)} for m in used]
    values = [row["fcf_uah"] for row in series]

    explanation = None
    if len(used) < months:
        explanation = f"Історії лише {len(used)} повних місяців із {months} запитаних."
    summary = dict.fromkeys(("count_ge_extra", "share", "min", "max", "median", "p25", "p75"))
    if len(values) < MIN_MONTHS_FOR_SUMMARY:
        explanation = (f"Замало повних місяців історії ({len(values)} < {MIN_MONTHS_FOR_SUMMARY}), "
                       "щоб показувати підсумки. " + (explanation or ""))
    else:
        count = sum(1 for v in values if v >= extra)
        p25, median, p75 = statistics.quantiles(values, n=4, method="inclusive")
        summary = {"count_ge_extra": count, "share": count / len(values), "min": min(values),
                   "max": max(values), "median": median, "p25": p25, "p75": p75}

    return {
        "extra_uah": extra,
        "n_months_requested": months,
        "n_months_used": len(used),
        "months": series,
        **summary,
        "data_stale": window["data_stale"],
        "explanation": explanation,
    }
