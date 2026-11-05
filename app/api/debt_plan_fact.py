"""
План-факт для боргів: прийняття плану, знімок версії, платежі, звірки й порівняння.

- POST /debt/plans/{plan_id}/accept — прийняти чернетку плану.
- GET /debt/plan/active — прийнята версія і її розклад.
- POST /debt/plan/materialize — зафіксувати розклад версії, що набрала чинності.
- GET /debt/plan-fact — план і факт по місяцях і боргах.
- POST/GET /debt/payments, PATCH/DELETE /debt/payments/{id} — платежі за боргами.
- POST /debt/{debt_id}/balance-checks — звірка залишку за випискою.
- PUT /debt/plan-fact/months/{month}/no-payments — позначка «платежів не було».

Суми у відповідях — гривні (з копійок), місяці — рядки YYYY-MM.
"""
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.debt import DEBT_BALANCE_MAX_UAH, _reject_non_finite
from app.api.deps import get_current_user, get_db, get_today
from app.models.debt import Debt
from app.models.debt_plan import DebtPayment, DebtPlanMonth, DebtPlanSchedule
from app.models.milp_plan import MilpPlan
from app.models.user import User
from app.services import debt_ledger as ledger
from app.services import debt_plan_versions as versions_service
from app.services.debt_fingerprint import debts_structure_fingerprint
from app.services.debt_plan_versions import PlanFactError
from app.services.debt_rules import debt_min_payment

router = APIRouter(prefix="/debt", tags=["debt-plan-fact"])

PaymentAmount = Annotated[
    float, BeforeValidator(_reject_non_finite), Field(gt=0, le=DEBT_BALANCE_MAX_UAH, allow_inf_nan=False)
]
CheckBalance = Annotated[
    float, BeforeValidator(_reject_non_finite), Field(ge=0, le=DEBT_BALANCE_MAX_UAH, allow_inf_nan=False)
]


def _raise(exc: PlanFactError) -> None:
    """Перетворює PlanFactError на HTTPException."""
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _to_api(value):
    """Готує значення для відповіді: *_kop → гривні без суфікса, місяці → YYYY-MM."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key.endswith("_kop"):
                out[key[:-4]] = ledger.kop_to_uah(item)
            elif key == "month" and isinstance(item, date):
                out[key] = ledger.format_month(item)
            else:
                out[key] = _to_api(item)
        return out
    if isinstance(value, list):
        return [_to_api(v) for v in value]
    return value


def _month_param(value: Optional[str], name: str) -> Optional[date]:
    """Розбирає параметр YYYY-MM.

    Raises:
        HTTPException: 422 для неправильного формату.
    """
    if value is None:
        return None
    try:
        return ledger.parse_month(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name}: очікується формат YYYY-MM") from exc


async def _own_debt(db: AsyncSession, user_id: str, debt_id: str) -> Debt:
    """Повертає борг користувача.

    Raises:
        HTTPException: 404, якщо борг не знайдено.
    """
    debt = (await db.execute(select(Debt).where(Debt.id == debt_id, Debt.user_id == user_id))).scalar_one_or_none()
    if debt is None:
        raise HTTPException(status_code=404, detail="Борг не знайдено")
    return debt


# --- версії плану ---------------------------------------------------------------------------

async def _debt_set_changes(db: AsyncSession, version, rows: list[DebtPlanSchedule],
                            names: dict[str, str]) -> tuple[list[dict] | None, list[dict] | None]:
    """Порівнює набір боргів у розкладі версії з боргами вихідної чернетки.

    Борг чернетки — debt_id із ненульовим залишком у plan_data["milp"]["start_balances"].

    Returns:
        Пара (added_debts, removed_debts) — списки {debt_id, name}, упорядковані
        за назвою; (None, None), якщо розкладу ще немає або чернетка не має
        start_balances.
    """
    if not rows:
        return None, None
    source = await db.get(MilpPlan, version.source_plan_id)
    start_balances = ((source.plan_data or {}).get("milp") or {}).get("start_balances") if source else None
    if start_balances is None:
        return None, None
    draft_ids = {debt_id for debt_id, balance in start_balances.items() if ledger.uah_to_kop(balance) > 0}
    schedule_ids = {r.debt_id for r in rows}

    def _listed(ids: set[str]) -> list[dict]:
        return sorted(({"debt_id": i, "name": names.get(i)} for i in ids),
                      key=lambda d: (d["name"] or "", d["debt_id"]))

    return _listed(schedule_ids - draft_ids), _listed(draft_ids - schedule_ids)


def _draft_preview(source: MilpPlan | None) -> dict | None:
    """Повертає орієнтовні місяці й переплату з чернетки, з якої прийнято версію.

    horizon_months — горизонт плану, якщо чернетка не погашає борги за нього
    (тоді months_to_payoff — null). None, якщо чернетки немає.
    """
    if source is None or not source.plan_data:
        return None
    milp = source.plan_data.get("milp") or {}
    months = milp.get("months_to_payoff")
    return {
        "months_to_payoff": months,
        "total_interest": milp.get("total_interest_paid", source.total_interest_paid),
        "horizon_months": source.months_to_payoff if months is None else None,
        "based_on": "balances_at_build",
    }


def _draft_comparison(source: MilpPlan | None) -> dict | None:
    """Повертає порівняння стратегій чернетки ({стратегія: {months_to_payoff, total_interest_paid}}) або None."""
    if source is None or not source.plan_data:
        return None
    return {
        strategy: {"months_to_payoff": data["months_to_payoff"], "total_interest_paid": data["total_interest_paid"]}
        for strategy, data in source.plan_data.items()
    }


async def _version_body(db: AsyncSession, user_id: str, version, today: date) -> dict:
    """Складає відповідь про версію з розкладом і ознакою зміни складу боргів."""
    rows = (await db.execute(
        select(DebtPlanSchedule).where(DebtPlanSchedule.version_id == version.id)
        .order_by(DebtPlanSchedule.month, DebtPlanSchedule.debt_id)
    )).scalars().all()
    debts = {d.id: d for d in (await db.execute(select(Debt).where(Debt.user_id == user_id))).scalars().all()}
    names = {debt_id: d.name for debt_id, d in debts.items()}
    schedule: dict[date, dict] = {}
    for r in rows:
        month = schedule.setdefault(r.month, {"month": r.month, "debts": [], "total_payment_kop": 0})
        month["debts"].append({"debt_id": r.debt_id, "name": names.get(r.debt_id), "opening_kop": r.opening_kop,
                               "interest_kop": r.interest_kop, "payment_kop": r.planned_payment_kop,
                               "closing_kop": r.closing_kop})
        month["total_payment_kop"] += r.planned_payment_kop

    # Склад зі знімка (борги з ненульовим стартовим залишком), а до знімка — склад
    # чернетки, з якої прийнято версію.
    source = await db.get(MilpPlan, version.source_plan_id)
    states = await ledger.active_debt_states(db, user_id, today)
    structure_changed = None
    if version.structure_fingerprint is not None:
        live = [s for s in states if s.balance_kop > 0]
        structure_changed = debts_structure_fingerprint(live) != version.structure_fingerprint
    else:
        if source is not None and source.debts_structure_fingerprint is not None:
            structure_changed = debts_structure_fingerprint(states) != source.debts_structure_fingerprint

    # Бюджет знімка: обов'язкові платежі від стартових залишків першого місяця + додатковий.
    min_total = None
    if rows:
        first_month = rows[0].month
        min_total = round(sum(
            debt_min_payment(debts[r.debt_id].debt_type, ledger.kop_to_uah(r.opening_kop),
                             debts[r.debt_id].monthly_rate, debts[r.debt_id].min_payment)
            for r in rows if r.month == first_month
        ), 2)
    extra = ledger.kop_to_uah(version.extra_monthly_kop)
    added_debts, removed_debts = await _debt_set_changes(db, version, rows, names)
    preview = _draft_preview(source) if version.materialized_at is None else None
    comparison = _draft_comparison(source) if version.start_kind == "acceptance_balances" else None

    versions = await versions_service.user_versions(db, user_id)
    in_force = next((v for v in versions if versions_service.version_covers(v, ledger.month_start(today))), None)
    payoff_month = (ledger.add_months(version.effective_from, version.months_to_payoff - 1)
                    if version.months_to_payoff else None)
    return _to_api({
        "version_id": version.id,
        "version_no": version.version_no,
        "source_plan_id": version.source_plan_id,
        "status": version.status,
        "accepted_at": version.accepted_at.isoformat() if version.accepted_at else None,
        "accepted_on": version.accepted_on,
        "effective_from": version.effective_from,
        "effective_to": version.effective_to,
        "start_kind": version.start_kind,
        "extra_monthly_kop": version.extra_monthly_kop,
        "min_payments_total": min_total,
        "total_monthly_payment": round(min_total + extra, 2) if min_total is not None else None,
        "strategy": version.strategy,
        "solver_status": version.solver_status,
        "materialized": version.materialized_at is not None,
        "months_to_payoff": version.months_to_payoff,
        "payoff_month": ledger.format_month(payoff_month) if payoff_month else None,
        "total_interest_kop": version.total_interest_kop,
        "structure_changed": structure_changed,
        "added_debts": added_debts,
        "removed_debts": removed_debts,
        "preview": preview,
        "comparison": comparison,
        "comparison_based_on": "balances_at_acceptance" if comparison is not None else None,
        "in_force_version_id": in_force.id if in_force else None,
        "schedule": list(schedule.values()),
    })


@router.post("/plans/{plan_id}/accept", status_code=201)
async def accept_plan(
    plan_id: str,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Приймає чернетку плану й повертає нову версію.

    Перша версія діє з поточного місяця від залишків на дату прийняття, її
    розклад будується одразу. Наступна діє з першого числа наступного місяця,
    розклад з'явиться після POST /debt/plan/materialize. Попередня версія стає
    замінена. Повторне прийняття тієї самої чернетки повертає наявну версію
    (200).

    Повертає 404 — чернетку не знайдено; 409 — чернетка без додаткового
    платежу чи відбитка, борги змінилися після її побудови або немає активних
    боргів; 422 — план неможливий; 503 — розв'язувач не встиг.
    """
    try:
        version, created = await versions_service.accept_plan(db, current_user.id, plan_id, today)
    except PlanFactError as exc:
        _raise(exc)
    if not created:
        response.status_code = 200
    return await _version_body(db, current_user.id, version, today)


@router.get("/plan/active")
async def active_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Повертає прийняту версію плану з помісячним розкладом.

    materialized=false — версія ще не має розкладу (див. POST
    /debt/plan/materialize). structure_changed — чи склад активних боргів
    (id, тип, ставка, мінімальний платіж) відрізняється від складу розкладу, а
    без розкладу — від складу чернетки, з якої прийнято версію. in_force_version_id —
    версія, чинна в поточному місяці. effective — версія, чинна в поточному
    місяці (та сама або попередня, замінена), pending — прийнята версія, що
    почне діяти з наступного місяця (null, якщо такої немає); обидві в тому
    самому форматі, що й відповідь. min_payments_total — обов'язкові платежі
    від стартових залишків розкладу, total_monthly_payment — разом із
    додатковим (обидва null без розкладу). added_debts і removed_debts —
    борги ({debt_id, name}), які є в розкладі версії, але не в чернетці, з
    якої її прийнято, і навпаки (null без розкладу або для чернетки без
    стартових залишків). preview — лише для версії без розкладу: орієнтовні
    months_to_payoff і total_interest з її чернетки (based_on:
    "balances_at_build" — за залишками на момент побудови; horizon_months —
    горизонт, якщо борги за нього не погашаються). comparison — порівняння
    стратегій з чернетки для версії зі start_kind=acceptance_balances
    (comparison_based_on: "balances_at_acceptance"), інакше null. Повертає
    404, якщо прийнятого плану немає.
    """
    version = await versions_service.accepted_version(db, current_user.id)
    if version is None:
        raise HTTPException(status_code=404, detail="Прийнятого плану ще немає.")
    body = await _version_body(db, current_user.id, version, today)
    current_month = ledger.month_start(today)
    if versions_service.version_covers(version, current_month):
        return {**body, "effective": body, "pending": None}
    versions = await versions_service.user_versions(db, current_user.id)
    in_force = next((v for v in versions if versions_service.version_covers(v, current_month)), None)
    effective = await _version_body(db, current_user.id, in_force, today) if in_force else None
    pending = body if version.effective_from > current_month else None
    return {**body, "effective": effective, "pending": pending}


@router.post("/plan/materialize")
async def materialize_plan(
    response: Response,
    force: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Фіксує розклад прийнятої версії від фактичних залишків на початок місяця її дії.

    Дозволено з 6-го числа місяця дії, раніше — з force=true. Після цього
    попередній місяць закрито для змін. Повторний виклик нічого не змінює
    (200); новий розклад — 201.

    Повертає 404 — немає прийнятого плану; 409 — версія ще не діє, рано без
    force або за попередній місяць не внесено платежів; 422 — план неможливий;
    503 — розв'язувач не встиг.
    """
    try:
        version, created = await versions_service.materialize(db, current_user.id, force=force, today=today)
    except PlanFactError as exc:
        _raise(exc)
    response.status_code = 201 if created else 200
    return await _version_body(db, current_user.id, version, today)


@router.get("/plan-fact")
async def plan_fact(
    from_month: Optional[str] = Query(None, alias="from"),
    to_month: Optional[str] = Query(None, alias="to"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Порівнює план і факт по місяцях і боргах; лише читає дані.

    Для кожного місяця: чинна версія, closed, editable, entry_state
    (entered / not_entered / no_payments_confirmed), counted_from (дата прийняття
    для першого місяця першого плану) і рядки боргів: плановий і фактичний
    платіж, плановий і фактичний залишок на кінець місяця, розбіжності (факт
    мінус план), переплата понад борг, звірка місяця. У рядку зі звіркою
    (check) balance_diff — null: після звірки відсотки решти місяця не
    враховано, тож залишок із плановим не порівнюється. У місяці not_entered
    фактичних платежів і розбіжностей немає (null); фактичний залишок невідомий
    до наступної звірки. remaining_to_plan — лише для поточного й майбутніх
    місяців (інакше null): скільки ще лишилося внести за планом, max(0, план −
    факт), по боргу й у totals. cumulative — сума розбіжностей платежів і
    остання відома розбіжність залишку лише по закритих місяцях, по кожному
    боргу (null, якщо після неї був місяць зі звіркою) і разом (сума відомих).

    Параметри from і to — YYYY-MM (за замовчуванням від місяця першого плану
    до поточного). Повертає 404 без прийнятого плану, 422 для неправильного
    діапазону.
    """
    start = _month_param(from_month, "from")
    end = _month_param(to_month, "to")
    try:
        result = await versions_service.plan_fact(db, current_user.id, start, end, today)
    except PlanFactError as exc:
        _raise(exc)
    return _to_api(result)


# --- позначка «платежів не було» --------------------------------------------------------------

class NoPaymentsRequest(BaseModel):
    """Тіло запиту позначки місяця."""
    confirmed: bool


@router.put("/plan-fact/months/{month}/no-payments")
async def set_no_payments(
    month: str,
    request: NoPaymentsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Ставить або знімає позначку «платежів не було» для місяця YYYY-MM.

    Повертає 422 — неправильний формат або майбутній місяць; 409 — плану ще
    немає, місяць до початку першого плану чи заблоковано, або в місяці вже є
    платежі (для confirmed=true).
    """
    month_date = _month_param(month, "month")
    if month_date > ledger.month_start(today):
        raise HTTPException(status_code=422, detail="Місяць не може бути в майбутньому.")
    versions = await versions_service.user_versions(db, current_user.id)
    first = versions_service.first_version(versions)
    if first is None or month_date < first.effective_from:
        raise HTTPException(status_code=409, detail="Для цього місяця немає прийнятого плану.")
    try:
        await versions_service.ensure_month_editable(db, current_user.id, month_date, today)
    except PlanFactError as exc:
        _raise(exc)
    if request.confirmed and await versions_service.month_entry_state(db, current_user.id, month_date) == "entered":
        raise HTTPException(status_code=409, detail="У цьому місяці вже є платежі.")

    mark = await db.get(DebtPlanMonth, (current_user.id, month_date))
    if mark is None:
        mark = DebtPlanMonth(user_id=current_user.id, month=month_date)
        db.add(mark)
    mark.no_payments_confirmed = request.confirmed
    mark.confirmed_at = datetime.now(timezone.utc) if request.confirmed else None
    await db.commit()
    return {"month": ledger.format_month(month_date),
            "entry_state": await versions_service.month_entry_state(db, current_user.id, month_date)}


# --- платежі ----------------------------------------------------------------------------------

class PaymentCreateRequest(BaseModel):
    """Тіло запиту платежу: борг, дата, сума в гривнях."""
    debt_id: str
    paid_on: date
    amount: PaymentAmount


class PaymentUpdateRequest(BaseModel):
    """Часткова зміна платежу; пропущене поле не змінюється."""
    paid_on: Optional[date] = None
    amount: Optional[PaymentAmount] = None


def _payment_body(p: DebtPayment) -> dict:
    """Платіж у відповіді API."""
    return {"id": p.id, "debt_id": p.debt_id, "paid_on": p.paid_on, "amount": ledger.kop_to_uah(p.amount_kop)}


async def _own_payment(db: AsyncSession, user_id: str, payment_id: str) -> DebtPayment:
    """Повертає платіж користувача.

    Raises:
        HTTPException: 404, якщо платіж не знайдено.
    """
    payment = (await db.execute(
        select(DebtPayment).where(DebtPayment.id == payment_id, DebtPayment.user_id == user_id)
    )).scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404, detail="Платіж не знайдено")
    return payment


@router.post("/payments", status_code=201)
async def create_payment(
    request: PaymentCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Вносить платіж за боргом.

    Повертає 404 — борг не знайдено; 422 — сума не більша за 0 або дата в
    майбутньому; 409 — плану ще немає, дата раніше прийняття першого плану або
    місяць заблоковано.
    """
    await _own_debt(db, current_user.id, request.debt_id)
    try:
        await versions_service.ensure_payment_allowed(db, current_user.id, request.paid_on, today)
    except PlanFactError as exc:
        _raise(exc)
    payment = DebtPayment(id=str(uuid.uuid4()), user_id=current_user.id, debt_id=request.debt_id,
                          paid_on=request.paid_on, amount_kop=ledger.uah_to_kop(request.amount))
    db.add(payment)
    await db.commit()
    return _payment_body(payment)


@router.get("/payments")
async def list_payments(
    month: Optional[str] = None,
    debt_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Повертає платежі користувача за датою; month (YYYY-MM) і debt_id звужують список.

    Повертає 422 для неправильного month.
    """
    query = select(DebtPayment).where(DebtPayment.user_id == current_user.id)
    month_date = _month_param(month, "month")
    if month_date is not None:
        query = query.where(DebtPayment.paid_on >= month_date, DebtPayment.paid_on <= ledger.month_end(month_date))
    if debt_id is not None:
        query = query.where(DebtPayment.debt_id == debt_id)
    rows = (await db.execute(query.order_by(DebtPayment.paid_on, DebtPayment.created_at))).scalars().all()
    return [_payment_body(p) for p in rows]


@router.patch("/payments/{payment_id}")
async def update_payment(
    payment_id: str,
    request: PaymentUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Змінює дату або суму платежу.

    Повертає 404 — платіж не знайдено; 422 — некоректна сума чи майбутня
    дата або явний null; 409 — старий чи новий місяць заблоковано або нова
    дата раніше прийняття першого плану.
    """
    payment = await _own_payment(db, current_user.id, payment_id)
    data = request.model_dump(exclude_unset=True)
    if any(v is None for v in data.values()):
        raise HTTPException(status_code=422, detail="Поле не може бути null")
    try:
        await versions_service.ensure_month_editable(db, current_user.id, payment.paid_on, today)
        if "paid_on" in data:
            await versions_service.ensure_payment_allowed(db, current_user.id, data["paid_on"], today)
    except PlanFactError as exc:
        _raise(exc)
    if "paid_on" in data:
        payment.paid_on = data["paid_on"]
    if "amount" in data:
        payment.amount_kop = ledger.uah_to_kop(data["amount"])
    await db.commit()
    return _payment_body(payment)


@router.delete("/payments/{payment_id}", status_code=204)
async def delete_payment(
    payment_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Видаляє платіж; 404 — не знайдено, 409 — місяць заблоковано."""
    payment = await _own_payment(db, current_user.id, payment_id)
    try:
        await versions_service.ensure_month_editable(db, current_user.id, payment.paid_on, today)
    except PlanFactError as exc:
        _raise(exc)
    await db.execute(delete(DebtPayment).where(DebtPayment.id == payment.id))
    await db.commit()
    return Response(status_code=204)


# --- звірки -----------------------------------------------------------------------------------

class BalanceCheckRequest(BaseModel):
    """Тіло запиту звірки: дата і фактичний залишок за випискою, грн."""
    as_of: date
    balance: CheckBalance


@router.post("/{debt_id}/balance-checks", status_code=201)
async def create_balance_check(
    debt_id: str,
    request: BalanceCheckRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    today: date = Depends(get_today),
):
    """Вносить фактичний залишок боргу за випискою на дату.

    З цієї дати залишок рахується від звірки: залишок місяця = звірка −
    платежі після неї, без додаткових відсотків. Звірка на ту саму дату
    замінює попередню (200).

    Повертає 404 — борг не знайдено; 422 — дата в майбутньому або від'ємний
    залишок; 409 — місяць заблоковано.
    """
    await _own_debt(db, current_user.id, debt_id)
    try:
        await versions_service.ensure_month_editable(db, current_user.id, request.as_of, today)
    except PlanFactError as exc:
        _raise(exc)
    check, created = await ledger.upsert_check(db, current_user.id, debt_id, request.as_of,
                                                ledger.uah_to_kop(request.balance), "statement")
    await db.commit()
    if not created:
        response.status_code = 200
    return {"id": check.id, "debt_id": debt_id, "as_of": check.as_of,
            "balance": ledger.kop_to_uah(check.balance_kop), "source": check.source}
