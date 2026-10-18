"""
Борги користувача, план погашення (MILP) і калькулятор спроможності.

- CRUD боргів: POST, GET, PATCH /debt.
- POST /debt/optimize — план з додатковим платежем, який задає користувач.
- GET /debt/affordability — історичний вільний грошовий потік, лише читання.
- GET /debt/plan/latest — збережений план і ознака його актуальності.
- POST /debt/optimize-raw — розрахунок для довільних боргів без збереження.
- POST /debt/optimize-from-forecast — план з бюджетом з прогнозу (застарілий,
  інтерфейс його не використовує).
"""
import math
import statistics
import uuid
from datetime import date
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.debt import Debt
from app.models.forecast import Forecast
from app.models.milp_plan import MilpPlan
from app.services.debt_fingerprint import debts_fingerprint
from app.services.debt_optimizer import optimize_debt_repayment
from app.services.debt_rules import revolving_min_payment
from app.services.forecasting import (
    _load_coverage,
    _load_expense_breakdown,
    _load_monthly_series,
    _month_key,
    _select_window,
)

router = APIRouter(prefix="/debt", tags=["debt"])


def _reject_non_finite(value):
    # JSON-тіло може містити NaN/Infinity. Стандартна відповідь 422 повертає
    # вхідне значення, а nan/inf не серіалізуються в JSON (500). Рядок натомість дає
    # звичайну 422.
    """Замінює NaN і ±Infinity на рядок, щоб валідація повернула 422, а не 500."""
    if isinstance(value, float) and not math.isfinite(value):
        return "не скінченне число"
    return value


# --- CRUD ---

# Правила полів боргу, спільні для POST і PATCH. monthly_rate — частка за
# місяць (0.04 = 4 %/міс). Баланс має бути більшим за 0; погашений борг
# позначається is_active=false.
DEBT_BALANCE_MAX_UAH = 1_000_000_000
DEBT_MONTHLY_RATE_MAX = 1.0
DEBT_NAME_MAX_LEN = 100

DebtName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=DEBT_NAME_MAX_LEN)]
DebtBalance = Annotated[
    float, BeforeValidator(_reject_non_finite), Field(gt=0, le=DEBT_BALANCE_MAX_UAH, allow_inf_nan=False)
]
DebtMonthlyRate = Annotated[
    float, BeforeValidator(_reject_non_finite), Field(ge=0, le=DEBT_MONTHLY_RATE_MAX, allow_inf_nan=False)
]
DebtMinPayment = Annotated[float, BeforeValidator(_reject_non_finite), Field(ge=0, allow_inf_nan=False)]
DebtType = Literal["revolving", "installment"]


def _require_installment_min_payment(debt_type: str, min_payment: Optional[float]) -> None:
    """Вимагає додатний мінімальний платіж для installment-боргу.

    Перевіряється підсумковий стан боргу (з урахуванням PATCH).

    Raises:
        HTTPException: 422 у форматі помилки pydantic з loc ["body", "min_payment"].
    """
    if debt_type == "installment" and (min_payment is None or min_payment <= 0):
        raise HTTPException(status_code=422, detail=[{
            "type": "value_error",
            "loc": ["body", "min_payment"],
            "msg": "Для розстрочки/кредиту мінімальний платіж обов'язковий і має бути більшим за 0",
            "input": min_payment,
        }])


class DebtCreateRequest(BaseModel):
    """Тіло запиту створення боргу."""
    name: DebtName
    balance: DebtBalance
    monthly_rate: DebtMonthlyRate
    min_payment: Optional[DebtMinPayment] = None
    debt_type: DebtType


class DebtUpdateRequest(BaseModel):
    """Тіло часткового оновлення боргу; пропущене поле не змінюється."""
    name: Optional[DebtName] = None
    balance: Optional[DebtBalance] = None
    monthly_rate: Optional[DebtMonthlyRate] = None
    min_payment: Optional[DebtMinPayment] = None  # null — лише для revolving (перевірка в update_debt)
    debt_type: Optional[DebtType] = None
    is_active: Optional[bool] = None

    # Пропущене поле не змінюється (exclude_unset); явний null у полі,
    # обов'язковому в БД, відхиляється.
    @field_validator("name", "balance", "monthly_rate", "debt_type", "is_active")
    @classmethod
    def _not_null(cls, value):
        """Забороняє явний null у полях, обов'язкових у БД."""
        if value is None:
            raise ValueError("поле не може бути null")
        return value


class DebtResponse(BaseModel):
    """Борг у відповіді API."""
    id: str
    name: str
    balance: float
    monthly_rate: float
    min_payment: Optional[float]
    debt_type: str
    is_active: bool

    class Config:
        from_attributes = True


@router.post("", response_model=DebtResponse, status_code=201)
async def create_debt(
    request: DebtCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Створює борг; 422, якщо installment-борг не має мінімального платежу."""
    _require_installment_min_payment(request.debt_type, request.min_payment)

    debt = Debt(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        name=request.name,
        balance=request.balance,
        monthly_rate=request.monthly_rate,
        min_payment=request.min_payment,
        debt_type=request.debt_type,
        is_active=True,
    )
    db.add(debt)
    await db.commit()
    await db.refresh(debt)
    return debt


@router.get("", response_model=list[DebtResponse])
async def list_debts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    include_inactive: bool = False,
):
    """Повертає активні борги користувача; з include_inactive — також погашені."""
    query = select(Debt).where(Debt.user_id == current_user.id)
    if not include_inactive:
        query = query.where(Debt.is_active == True)
    result = await db.execute(query)
    return result.scalars().all()


@router.patch("/{debt_id}", response_model=DebtResponse)
async def update_debt(
    debt_id: str,
    request: DebtUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Частково оновлює борг (лише передані поля).

    Повертає 404, якщо борг не знайдено, і 422, якщо після змін installment-борг
    не має мінімального платежу.
    """
    result = await db.execute(
        select(Debt).where(Debt.id == debt_id, Debt.user_id == current_user.id)
    )
    debt = result.scalar_one_or_none()
    if debt is None:
        raise HTTPException(status_code=404, detail="Борг не знайдено")

    update_data = request.model_dump(exclude_unset=True)
    _require_installment_min_payment(
        update_data.get("debt_type", debt.debt_type),
        update_data["min_payment"] if "min_payment" in update_data else debt.min_payment,
    )
    for field, value in update_data.items():
        setattr(debt, field, value)

    await db.commit()
    await db.refresh(debt)
    return debt


# --- Оптимізація ---

class DebtItem(BaseModel):
    """Борг у тілі запиту /debt/optimize-raw."""
    name: str
    balance: float
    monthly_rate: float
    min_payment: Optional[float] = None
    debt_type: str


class DebtOptimizeRequest(BaseModel):
    """Тіло запиту /debt/optimize-raw: борги, загальний місячний бюджет і горизонт."""
    debts: list[DebtItem]
    monthly_budget: float
    months: int = 36


@router.post("/optimize-raw")
async def optimize(request: DebtOptimizeRequest):
    """Рахує чотири стратегії для боргів і бюджету з тіла запиту; нічого не зберігає."""
    try:
        debts = [d.model_dump() for d in request.debts]
        return optimize_debt_repayment(debts, request.monthly_budget, request.months)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/optimize-from-forecast")
async def optimize_from_forecast(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    months: int = 36,
):
    """Будує план з бюджетом free_cash_flow з найновішого прогнозу (застарілий ендпоінт).

    Бере активні борги користувача, зберігає результат у milp_plans з
    посиланням на прогноз.
    """
    debts_result = await db.execute(
        select(Debt).where(Debt.user_id == current_user.id, Debt.is_active == True)
    )
    debts = debts_result.scalars().all()
    if not debts:
        raise HTTPException(
            status_code=400,
            detail="Немає активних боргів. Додай хоча б один через POST /debt",
        )

    forecast_result = await db.execute(
        select(Forecast)
        .where(Forecast.user_id == current_user.id)
        .order_by(Forecast.generated_at.desc())
        .limit(1)
    )
    forecast = forecast_result.scalar_one_or_none()
    if forecast is None:
        raise HTTPException(
            status_code=400,
            detail="Немає прогнозу. Виклич POST /forecast/generate спочатку",
        )

    if forecast.free_cash_flow <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Прогнозований вільний грошовий потік від'ємний або нульовий "
                f"({forecast.free_cash_flow} грн) — оптимізація боргів неможлива "
                f"без вільного бюджету. Це реальний сигнал, не помилка коду."
            ),
        )

    debts_dicts = [
        {
            "name": d.name,
            "balance": d.balance,
            "monthly_rate": d.monthly_rate,
            "min_payment": d.min_payment,
            "debt_type": d.debt_type,
        }
        for d in debts
    ]

    try:
        strategies = optimize_debt_repayment(debts_dicts, forecast.free_cash_flow, months)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MILP-оптимізація впала: {e}")

    milp_result = strategies["milp"]

    plan = MilpPlan(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        forecast_id=forecast.id,
        months_to_payoff=milp_result["months_to_payoff"] or months,
        total_interest_paid=milp_result["total_interest_paid"],
        plan_data=strategies,  # усі 4 стратегії для порівняння
        status="active",
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)

    return {
        "plan_id": plan.id,
        "forecast_id": forecast.id,
        "monthly_budget_used": forecast.free_cash_flow,
        "months_to_payoff": milp_result["months_to_payoff"],
        "total_interest_paid": milp_result["total_interest_paid"],
        "comparison": {
            strategy: {
                "months_to_payoff": data["months_to_payoff"],
                "total_interest_paid": data["total_interest_paid"],
            }
            for strategy, data in strategies.items()
        },
    }


# --- План з додатковим платежем і калькулятор спроможності ---

EXTRA_PAYMENT_MAX_UAH = 10_000_000
AFFORDABILITY_DISCLAIMER = "Це історичні дані, а не прогноз і не гарантія"


class ExtraPaymentRequest(BaseModel):
    """Тіло запиту POST /debt/optimize: додатковий місячний платіж, грн."""
    extra_monthly_uah: Annotated[
        float,
        BeforeValidator(_reject_non_finite),
        Field(ge=0, le=EXTRA_PAYMENT_MAX_UAH, allow_inf_nan=False),
    ]


def _min_payment(debt: Debt) -> float:
    """Повертає поточний мінімальний платіж боргу за правилами debt_rules."""
    if debt.debt_type == "revolving":
        return revolving_min_payment(debt.balance)
    return debt.min_payment or 0


@router.post("/optimize", status_code=201)
async def optimize_with_extra(
    request: ExtraPaymentRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    months: int = 36,
):
    """Будує й зберігає план погашення активних боргів з додатковим платежем.

    Загальний місячний бюджет = сума поточних мінімальних платежів +
    extra_monthly_uah. План зберігається в milp_plans з forecast_id = NULL,
    додатковим платежем і відбитком боргів.

    Повертає 409, якщо активних боргів немає, і 422, якщо план неможливий або
    солвер не впорався (план тоді не зберігається).
    """
    debts = (await db.execute(
        select(Debt).where(Debt.user_id == current_user.id, Debt.is_active == True)  # noqa: E712
    )).scalars().all()
    if not debts:
        raise HTTPException(status_code=409, detail="Немає активних боргів. Додай хоча б один борг через POST /debt.")

    extra = request.extra_monthly_uah
    min_total = sum(_min_payment(d) for d in debts)
    budget = min_total + extra
    debts_dicts = [
        {"name": d.name, "balance": d.balance, "monthly_rate": d.monthly_rate,
         "min_payment": d.min_payment, "debt_type": d.debt_type}
        for d in debts
    ]
    hint = "Спробуй збільшити додатковий платіж або перевір ставки й мінімальні платежі боргів."
    try:
        strategies = optimize_debt_repayment(debts_dicts, budget, months)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Не вдалося розрахувати план: {e}. {hint}")

    milp = strategies["milp"]
    if milp["status"] != "Optimal":
        raise HTTPException(
            status_code=422,
            detail=(
                f"План неможливий за місячного бюджету {budget:.0f} грн (мінімальні платежі "
                f"{min_total:.0f} + додатковий {extra:.0f}); статус солвера: {milp['status']}. {hint}"
            ),
        )

    plan = MilpPlan(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        forecast_id=None,
        months_to_payoff=milp["months_to_payoff"] or months,
        total_interest_paid=milp["total_interest_paid"],
        plan_data=strategies,
        status="active",
        extra_monthly_uah=extra,
        debts_fingerprint=debts_fingerprint(debts),
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)

    return {
        "plan_id": plan.id,
        "forecast_id": None,
        "extra_monthly_uah": extra,
        "min_payments_total_uah": min_total,
        "total_monthly_payment_uah": budget,
        "horizon_months": months,
        "status": milp["status"],
        "months_to_payoff": milp["months_to_payoff"],
        "total_interest_paid": milp["total_interest_paid"],
        "comparison": {
            strategy: {
                "months_to_payoff": data["months_to_payoff"],
                "total_interest_paid": data["total_interest_paid"],
            }
            for strategy, data in strategies.items()
        },
        "monthly_schedule": milp["monthly_log"],
    }


@router.get("/affordability")
async def affordability(
    extra: float = Query(ge=0, le=EXTRA_PAYMENT_MAX_UAH, allow_inf_nan=False),
    months: int = Query(12, ge=3, le=24),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Показує історичний вільний грошовий потік за останні `months` повних місяців.

    Лише читає дані. Вільний потік — дохід мінус витрати (мінімальні платежі за
    боргами вже вилучені з витрат); вікно місяців обирається так само, як для
    прогнозу (_select_window). Повертає, скільки місяців мали потік не менший за
    extra, а також мінімум, максимум, медіану, P25 і P75 (лінійна інтерполяція).
    Якщо повних місяців менше 3 — порожні підсумки з поясненням. Суми в гривнях.
    """
    income_df = await _load_monthly_series(current_user.id, income=True, session=db)
    breakdown = await _load_expense_breakdown(current_user.id, session=db)
    coverage, _ = await _load_coverage(db, current_user.id)
    window = _select_window(coverage, date.today())

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
    if len(values) < 3:
        explanation = (f"Замало повних місяців історії ({len(values)} < 3), щоб показувати підсумки. "
                       + (explanation or ""))
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
        "historical_not_forecast": True,
        "disclaimer": AFFORDABILITY_DISCLAIMER,
    }


@router.get("/plan/latest")
async def latest_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Повертає збережений план без перерахунку MILP.

    Містить порівняння стратегій, помісячний розклад до погашення, додатковий
    платіж і is_stale — чи змінився склад боргів після побудови плану (None,
    якщо відбиток не збережено). Повертає 404, якщо планів ще немає.
    """
    result = await db.execute(
        select(MilpPlan)
        .where(MilpPlan.user_id == current_user.id)
        .order_by(MilpPlan.generated_at.desc())
        .limit(1)
    )
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail="Плану ще немає. Побудуй план у розділі «Борги»: вкажи додатковий платіж і натисни «Побудувати план».",
        )

    milp_data = plan.plan_data.get("milp", {})

    # Актуальність: якщо відбиток збережено — порівнюємо з поточними активними
    # боргами; якщо ні — None.
    is_stale = None
    if plan.debts_fingerprint is not None:
        active_debts = (await db.execute(
            select(Debt).where(Debt.user_id == current_user.id, Debt.is_active == True)  # noqa: E712
        )).scalars().all()
        is_stale = debts_fingerprint(active_debts) != plan.debts_fingerprint

    return {
        "plan_id": plan.id,
        "forecast_id": plan.forecast_id,
        "generated_at": plan.generated_at.isoformat(),
        "months_to_payoff": plan.months_to_payoff,
        "total_interest_paid": plan.total_interest_paid,
        "status": plan.status,
        "comparison": {
            strategy: {
                "months_to_payoff": data["months_to_payoff"],
                "total_interest_paid": data["total_interest_paid"],
            }
            for strategy, data in plan.plan_data.items()
        },
        # Помісячний розклад MILP-плану до повного погашення.
        "monthly_schedule": milp_data.get("monthly_log", []),
        "extra_monthly_uah": plan.extra_monthly_uah,
        "is_stale": is_stale,
    }
