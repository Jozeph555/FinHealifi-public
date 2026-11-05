"""
Борги користувача, план погашення (MILP) і калькулятор спроможності.

- CRUD боргів: POST, GET, PATCH /debt.
- POST /debt/optimize — план з додатковим платежем, який задає користувач.
- GET /debt/affordability — історичний вільний грошовий потік, лише читання.
- GET /debt/plan/latest — остання чернетка плану і ознака її актуальності.
- POST /debt/optimize-raw — розрахунок для довільних боргів без збереження.

Залишок боргу у відповідях і розрахунках береться з журналу
(app/services/debt_ledger.py); зміна залишку через PATCH створює звірку.
"""
import math
import uuid
from datetime import date
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user, get_today, resolve_today
from app.models.user import User
from app.models.debt import Debt
from app.models.milp_plan import MilpPlan
from app.services import debt_ledger as ledger
from app.services.debt_fingerprint import debts_fingerprint, debts_structure_fingerprint, draft_changes
from app.services.debt_optimizer import SOLVER_NOT_FINISHED_STATUSES, optimize_debt_repayment_async
from app.services.debt_rules import debt_min_payment
from app.services.affordability import historical_free_cash_flow

router = APIRouter(prefix="/debt", tags=["debt"])

# Пояснення для користувача, коли план складено за avalanche (fallback=True).
FALLBACK_PLAN_NOTE = (
    "Розрахований план зараз не вдалося скласти, тому показано варіант «спочатку найдорожчий борг»."
)


# Відповідь 503, коли розв'язувач не встиг завершити розрахунок.
SOLVER_NOT_FINISHED_DETAIL = (
    "Розрахований план зараз не вдалося скласти: розрахунок не встиг завершитися. "
    "Спробуй ще раз трохи пізніше."
)


def _calc_error_detail(exc: Exception, suffix: str = "") -> str:
    """Текст помилки розрахунку; причина з exc додається лише непорожньою."""
    reason = str(exc).strip()
    text = "Не вдалося розрахувати план" + (f": {reason}" if reason else "") + "."
    return f"{text} {suffix}".strip()


def _raise_calc_error(exc: Exception, status_code: int, suffix: str = "") -> None:
    """Перетворює виняток розрахунку на HTTPException; TimeoutError — 503.

    Raises:
        HTTPException: 503 з SOLVER_NOT_FINISHED_DETAIL для TimeoutError,
            інакше status_code з текстом _calc_error_detail.
    """
    if isinstance(exc, TimeoutError):
        raise HTTPException(status_code=503, detail=SOLVER_NOT_FINISHED_DETAIL) from exc
    raise HTTPException(status_code=status_code, detail=_calc_error_detail(exc, suffix)) from exc


def _fallback_fields(milp: dict) -> dict:
    """Повертає поля fallback і plan_note для відповіді за результатом milp."""
    fallback = bool(milp.get("fallback", False))
    return {"fallback": fallback, "plan_note": FALLBACK_PLAN_NOTE if fallback else None}


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


def _state_response(state: ledger.DebtState) -> DebtResponse:
    """Борг з журнальним залишком у форматі відповіді."""
    return DebtResponse(id=state.id, name=state.name, balance=state.balance, monthly_rate=state.monthly_rate,
                        min_payment=state.min_payment, debt_type=state.debt_type, is_active=state.is_active)


async def _debt_response(db: AsyncSession, debt: Debt, today: date) -> DebtResponse:
    """Відповідь про борг із залишком на сьогодні з журналу."""
    return _state_response((await ledger.debt_states(db, debt.user_id, [debt], today))[0])


@router.post("", response_model=DebtResponse, status_code=201)
async def create_debt(
    request: DebtCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Створює борг; 422, якщо installment-борг не має мінімального платежу."""
    today = resolve_today(today)
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
    await db.flush()
    await ledger.upsert_check(db, current_user.id, debt.id, today, ledger.uah_to_kop(request.balance),
                              "debt_created")
    await db.commit()
    await db.refresh(debt)
    # Щойно створений борг: залишок журналу дорівнює debts.balance.
    return debt


@router.get("", response_model=list[DebtResponse])
async def list_debts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    include_inactive: bool = False,
    today: date = Depends(get_today),
):
    """Повертає активні борги користувача; з include_inactive — також погашені.

    balance — залишок на сьогодні з журналу звірок і платежів.
    """
    today = resolve_today(today)
    query = select(Debt).where(Debt.user_id == current_user.id)
    if not include_inactive:
        query = query.where(Debt.is_active == True)
    debts = (await db.execute(query.order_by(Debt.created_at, Debt.id))).scalars().all()
    states = await ledger.debt_states(db, current_user.id, list(debts), today)
    return [_state_response(s) for s in states]


@router.patch("/{debt_id}", response_model=DebtResponse)
async def update_debt(
    debt_id: str,
    request: DebtUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Частково оновлює борг (лише передані поля).

    Новий balance не перезаписує поле боргу, а створює звірку 'manual_edit' на
    сьогодні: з неї рахується залишок далі. Повертає 404, якщо борг не знайдено, і 422, якщо після змін installment-борг
    не має мінімального платежу.
    """
    today = resolve_today(today)
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
    new_balance = update_data.pop("balance", None)
    for field, value in update_data.items():
        setattr(debt, field, value)
    if new_balance is not None:
        await ledger.upsert_check(db, current_user.id, debt.id, today, ledger.uah_to_kop(new_balance),
                                  "manual_edit")

    await db.commit()
    await db.refresh(debt)
    return await _debt_response(db, debt, today)


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
    """Рахує чотири стратегії для боргів і бюджету з тіла запиту; нічого не зберігає.

    Якщо MILP-розв'язок не вдався, milp містить результат «спочатку найдорожчий
    борг» з fallback=true. Повертає 503, якщо розв'язувач не встиг, а запасного
    плану немає, і 500 у разі іншої помилки розрахунку.
    """
    try:
        debts = [d.model_dump() for d in request.debts]
        return await optimize_debt_repayment_async(debts, request.monthly_budget, request.months)
    except Exception as e:
        _raise_calc_error(e, 500)


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


def _min_payment(debt: ledger.DebtState) -> float:
    """Повертає поточний мінімальний платіж боргу за правилами debt_rules."""
    return debt_min_payment(debt.debt_type, debt.balance, debt.monthly_rate, debt.min_payment)


@router.post("/optimize", status_code=201)
async def optimize_with_extra(
    request: ExtraPaymentRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    months: int = 36,
    today: date = Depends(get_today),
):
    """Будує й зберігає план погашення активних боргів з додатковим платежем.

    Загальний місячний бюджет = сума поточних мінімальних платежів +
    extra_monthly_uah; залишки — на сьогодні з журналу. План зберігається в
    milp_plans як чернетка (plan_state='draft') з додатковим платежем,
    відбитками боргів і стартовими залишками за debt_id
    (plan_data["milp"]["start_balances"]); прийняти її — POST
    /debt/plans/{plan_id}/accept.

    Якщо MILP-розв'язок не вдався (ліміт часу, помилка), а варіант «спочатку
    найдорожчий борг» вкладається в бюджет, зберігається він: fallback=true,
    status — статус розв'язувача, plan_note — пояснення для користувача.

    Повертає 409, якщо активних боргів немає; 422, якщо план неможливий; 503,
    якщо розв'язувач не встиг (Time Limit, Not Solved, Timeout), а запасного
    плану немає. В обох останніх випадках план не зберігається.
    """
    today = resolve_today(today)
    debts = await ledger.active_debt_states(db, current_user.id, today)
    if not debts:
        raise HTTPException(status_code=409, detail="Немає активних боргів. Додай хоча б один борг на сторінці «Борги».")

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
        strategies = await optimize_debt_repayment_async(debts_dicts, budget, months)
    except Exception as e:
        _raise_calc_error(e, 422, hint)

    milp = strategies["milp"]
    if milp["status"] in SOLVER_NOT_FINISHED_STATUSES and not milp.get("fallback"):
        raise HTTPException(status_code=503, detail=SOLVER_NOT_FINISHED_DETAIL)
    if milp["status"] != "Optimal" and not milp.get("fallback"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Не вдалося розрахувати план за цих умов: місячний бюджет {budget:.0f} грн "
                f"(обов'язкові платежі {min_total:.0f} + додатковий {extra:.0f}). {hint}"
            ),
        )

    # Стартові залишки за debt_id: з ними версія плану порівнює склад свого розкладу.
    milp["start_balances"] = {d.id: d.balance for d in debts}
    plan = MilpPlan(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        forecast_id=None,
        months_to_payoff=milp["months_to_payoff"] or months,
        total_interest_paid=milp["total_interest_paid"],
        plan_data=strategies,
        status="draft",
        extra_monthly_uah=extra,
        debts_fingerprint=debts_fingerprint(debts),
        debts_structure_fingerprint=debts_structure_fingerprint(debts),
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)

    return {
        "plan_id": plan.id,
        "plan_state": "draft",
        "forecast_id": None,
        "extra_monthly_uah": extra,
        "min_payments_total_uah": min_total,
        "total_monthly_payment_uah": budget,
        "horizon_months": months,
        "status": milp["status"],
        **_fallback_fields(milp),
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
    result = await historical_free_cash_flow(db, current_user.id, extra, months)
    return {
        **result,
        "historical_not_forecast": True,
        "disclaimer": AFFORDABILITY_DISCLAIMER,
    }


@router.get("/plan/latest")
async def latest_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Повертає останню чернетку плану без перерахунку MILP.

    Містить порівняння стратегій, помісячний розклад до погашення, додатковий
    платіж, months_to_payoff (null, якщо план не погашає борги за горизонт; тоді
    horizon_months — горизонт плану, інакше null). Зміни боргів після побудови:
    structure_changed — змінився склад активних боргів, тип, ставка чи
    мінімальний платіж; balances_changed — за того самого складу змінилися лише
    залишки (обидва null, якщо відбиток не збережено). Першу чернетку можна
    прийняти лише без жодних змін, наступні — без зміни складу.
    min_payments_total_uah і total_monthly_payment_uah — обов'язкові платежі й
    разом із додатковим, лише коли змін немає (інакше null). fallback=true і plan_note — план складено за
    «спочатку найдорожчий борг», бо MILP-розв'язок не вдався. Повертає 404,
    якщо планів ще немає.
    """
    today = resolve_today(today)
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
    # Колонка months_to_payoff для плану, що не погашає борги за горизонт,
    # зберігає сам горизонт; справжнє значення — у plan_data["milp"].
    months_to_payoff = milp_data.get("months_to_payoff") if milp_data else plan.months_to_payoff
    horizon_months = plan.months_to_payoff if milp_data and months_to_payoff is None else None

    # Актуальність: якщо відбиток збережено — порівнюємо з поточними активними
    # боргами; якщо ні — None.
    active_debts = await ledger.active_debt_states(db, current_user.id, today)
    balances_changed, structure_changed = draft_changes(plan, active_debts)
    min_total = None
    # Борги ті самі, що під час побудови, тож обов'язкові платежі збігаються з бюджетом плану.
    if balances_changed is False and structure_changed is False:
        min_total = sum(_min_payment(d) for d in active_debts)
    total = min_total + plan.extra_monthly_uah if min_total is not None and plan.extra_monthly_uah is not None else None

    return {
        "plan_id": plan.id,
        "forecast_id": plan.forecast_id,
        "generated_at": plan.generated_at.isoformat(),
        "months_to_payoff": months_to_payoff,
        "horizon_months": horizon_months,
        "total_interest_paid": plan.total_interest_paid,
        "status": plan.status,
        "plan_state": plan.status,
        **_fallback_fields(milp_data),
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
        "min_payments_total_uah": min_total,
        "total_monthly_payment_uah": total,
        "balances_changed": balances_changed,
        "structure_changed": structure_changed,
    }
