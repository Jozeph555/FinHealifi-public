"""
Прогноз доходу, витрат і вільного грошового потоку на наступний місяць.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.forecast import Forecast
from app.services.forecasting import generate_forecast

router = APIRouter(prefix="/forecast", tags=["forecast"])

# Поля вікна прогнозу й розбивки витрат з raw_output. Прогнози без цих
# ключів повертають None.
BREAKDOWN_SUMMARY_FIELDS = (
    "months_used",
    "fallback_reason",
    "partial_months_only",
    "data_stale",
    "last_transaction_at",
    "data_stale_days",
    "uncategorized_outflow_avg_per_month",
    "uncategorized_inflow_avg_per_month",
    "debt_payments_outside_list_avg_per_month",
)

# Опис полів для Swagger (/docs), спільний для обох ендпоінтів.
BREAKDOWN_FIELDS_DOC = """
Поля вікна прогнозу й розбивки витрат. Для прогнозів без цих полів — null.

- `months_used` — місяці ("YYYY-MM"), з яких рахується прогноз: повні місяці
  останнього суцільного блоку місяців з будь-якою транзакцією (перший місяць
  блоку й поточний календарний — неповні). Якщо повних немає — неповні
  місяці блоку (`partial_months_only`). База для всіх `*_avg_per_month`.
- `fallback_reason` — чому замість Prophet взято середнє за `months_used`:
  `partial_months_only` (немає повних місяців) > `stale_data` (останній
  місяць даних раніше за попередній календарний) > `insufficient_months`
  (повних місяців < 3); `null` — працював Prophet. Якщо діють кілька причин,
  повертається перша за цим пріоритетом.
- `partial_months_only` — прогноз з неповних місяців.
- `data_stale` — дані застарілі (незалежно від `fallback_reason`): прогноз не
  екстраполює через розрив; варто запустити синхронізацію.
- `last_transaction_at` — час останньої транзакції користувача (ISO, UTC).
- `data_stale_days` — днів від `last_transaction_at` до дати прогнозу.
- `uncategorized_outflow_avg_per_month` — середній за місяць ВАЛОВИЙ відтік
  без категорії (`category` = pending або NULL): мерчанти, P2P (MCC 4829) і
  рядки без MCC разом, ДО неттінгу.
- `uncategorized_inflow_avg_per_month` — середнє за місяць УСІХ надходжень
  без категорії (pending/NULL), незалежно від MCC. Увага: у витратах
  неттінг діє лише для P2P (MCC 4829) — надходження без MCC чи з іншим MCC
  відтік НЕ зменшують. Тому `outflow − inflow` з цих двох полів НЕ дорівнює
  внеску некатегоризованих рядків у `predicted_expenses`.
- `debt_payments_outside_list_avg_per_month` — середній за місяць відтік
  категорії "Борги та кредити" понад Σ мінімальних платежів активних боргів
  з таблиці debts (ця частина лишається витратою; мінімуми платить MILP).
"""


def _breakdown_fields(forecast: Forecast) -> dict:
    """Повертає поля вікна прогнозу й розбивки витрат з raw_output; відсутні ключі дають None."""
    raw = forecast.raw_output or {}
    return {key: raw.get(key) for key in BREAKDOWN_SUMMARY_FIELDS}


GENERATE_DOC = """
Рахує новий прогноз доходу, витрат і вільного грошового потоку на
наступний місяць і зберігає його в forecasts.
""" + BREAKDOWN_FIELDS_DOC


@router.post("/generate", description=GENERATE_DOC)
async def generate(
    current_user: User = Depends(get_current_user),
):
    """Рахує й зберігає прогноз; 400, якщо розрахунок не вдався. Опис полів — у GENERATE_DOC."""
    try:
        forecast = await generate_forecast(current_user.id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "id": forecast.id,
        "period_start": forecast.period_start.isoformat(),
        "period_end": forecast.period_end.isoformat(),
        "predicted_income": forecast.predicted_income,
        "predicted_expenses": forecast.predicted_expenses,
        "free_cash_flow": forecast.free_cash_flow,
        "method": forecast.method,
        "low_confidence": forecast.low_confidence,
        **_breakdown_fields(forecast),
        "raw_output": forecast.raw_output,
    }


@router.get("/latest", description="Найновіший збережений прогноз користувача." + BREAKDOWN_FIELDS_DOC)
async def latest(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Повертає найновіший збережений прогноз; 404, якщо прогнозів ще немає."""
    result = await db.execute(
        select(Forecast)
        .where(Forecast.user_id == current_user.id)
        .order_by(Forecast.generated_at.desc())
        .limit(1)
    )
    forecast = result.scalar_one_or_none()
    if forecast is None:
        raise HTTPException(
            status_code=404,
            detail="Прогнозів ще немає — виклич POST /forecast/generate спочатку",
        )

    return {
        "id": forecast.id,
        "generated_at": forecast.generated_at.isoformat(),
        "period_start": forecast.period_start.isoformat(),
        "period_end": forecast.period_end.isoformat(),
        "predicted_income": forecast.predicted_income,
        "predicted_expenses": forecast.predicted_expenses,
        "free_cash_flow": forecast.free_cash_flow,
        "method": forecast.method,
        "low_confidence": forecast.low_confidence,
        **_breakdown_fields(forecast),
    }
