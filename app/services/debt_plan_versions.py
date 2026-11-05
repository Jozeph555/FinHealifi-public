"""
Версії прийнятого плану погашення: прийняття, знімок розкладу, блокування місяців і план-факт.

Стани: чернетка (milp_plans) → прийнята версія (accepted) → замінена (superseded).
- Перша версія діє з поточного місяця від залишків на дату прийняття; знімок
  розкладу будується одразу, у журналі з'являються звірки 'plan_accepted'.
- Наступна версія діє з першого числа наступного місяця; знімок будує
  materialize після вступу в дію від фактичних залишків на початок місяця.
- Попередня версія отримує effective_to = effective_from нової. Версія з
  effective_to <= effective_from не діяла жодного місяця.
- Місяць M заблокований, якщо є матеріалізована версія, що діяла, з
  effective_from > M.

Стан внесення місяця (на рівні користувача): 'entered' — є хоч один платіж
за будь-яким боргом; 'no_payments_confirmed' — користувач позначив, що
платежів не було; інакше 'not_entered'. Місяць 'not_entered' не дає
розбіжності: фактичні платежі й залишки в ньому і далі до наступної звірки
невідомі.
"""
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.debt import Debt
from app.models.debt_plan import (
    DebtBalanceCheck, DebtPayment, DebtPlanMonth, DebtPlanSchedule, DebtPlanVersion,
)
from app.models.milp_plan import MilpPlan
from app.models.user import User
from app.services import debt_ledger as ledger
from app.services.debt_fingerprint import debts_structure_fingerprint, draft_changes
from app.services.debt_optimizer import SOLVER_NOT_FINISHED_STATUSES, optimize_debt_repayment_async
from app.services.debt_rules import debt_min_payment

PLAN_HORIZON_MONTHS = 36
# З якого числа місяця дії наступну версію можна матеріалізувати без force.
MATERIALIZE_FROM_DAY = 6
# Залишок MILP, нижче якого борг у місяці вважається погашеним, грн.
PAID_OFF_TOLERANCE_UAH = 0.01

ENTERED = "entered"
NOT_ENTERED = "not_entered"
NO_PAYMENTS_CONFIRMED = "no_payments_confirmed"


class PlanFactError(Exception):
    """Помилка правил план-факту з HTTP-кодом і текстом для користувача."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# --- дати версій ----------------------------------------------------------------------

def compute_effective_from(has_version: bool, today: date) -> date:
    """Повертає перше число місяця, з якого діє нова версія.

    Перша версія — поточний місяць, наступні — наступний місяць.
    """
    return ledger.month_start(today) if not has_version else ledger.add_months(today, 1)


def version_acted(v: DebtPlanVersion) -> bool:
    """Чи версія діяла хоч один місяць (effective_to порожній або більший за effective_from)."""
    return v.effective_to is None or v.effective_to > v.effective_from


def version_covers(v: DebtPlanVersion, month: date) -> bool:
    """Чи версія чинна для місяця (перше число місяця)."""
    return version_acted(v) and v.effective_from <= month and (v.effective_to is None or month < v.effective_to)


async def _lock_user(session: AsyncSession, user_id: str) -> None:
    """Блокує рядок користувача, щоб прийняття й матеріалізація йшли послідовно."""
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())


async def user_versions(session: AsyncSession, user_id: str) -> list[DebtPlanVersion]:
    """Повертає всі версії користувача за version_no."""
    return list((await session.execute(
        select(DebtPlanVersion).where(DebtPlanVersion.user_id == user_id).order_by(DebtPlanVersion.version_no)
    )).scalars().all())


async def accepted_version(session: AsyncSession, user_id: str, for_update: bool = False) -> DebtPlanVersion | None:
    """Повертає прийняту (не замінену) версію користувача або None."""
    query = select(DebtPlanVersion).where(DebtPlanVersion.user_id == user_id, DebtPlanVersion.status == "accepted")
    if for_update:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


def first_version(versions: list[DebtPlanVersion]) -> DebtPlanVersion | None:
    """Повертає першу матеріалізовану версію (з якої починається журнал) або None."""
    materialized = [v for v in versions if v.materialized_at is not None]
    return min(materialized, key=lambda v: v.effective_from) if materialized else None


def month_locked(versions: list[DebtPlanVersion], month: date) -> bool:
    """Чи місяць заблокований для змін: є матеріалізована версія, що діяла, з effective_from > month."""
    return any(v.materialized_at is not None and version_acted(v) and v.effective_from > month for v in versions)


async def month_entry_state(session: AsyncSession, user_id: str, month: date) -> str:
    """Повертає стан внесення місяця: entered, no_payments_confirmed або not_entered."""
    has_payment = (await session.execute(
        select(DebtPayment.id).where(
            DebtPayment.user_id == user_id,
            DebtPayment.paid_on >= month,
            DebtPayment.paid_on <= ledger.month_end(month),
        ).limit(1)
    )).first() is not None
    if has_payment:
        return ENTERED
    mark = await session.get(DebtPlanMonth, (user_id, month))
    if mark is not None and mark.no_payments_confirmed:
        return NO_PAYMENTS_CONFIRMED
    return NOT_ENTERED


# --- знімок розкладу ----------------------------------------------------------------------

@dataclass
class Snapshot:
    """Знімок розкладу версії: рядки, строк, відсотки, стратегія."""
    rows: list[DebtPlanSchedule]
    months_to_payoff: int | None
    total_interest_kop: int
    strategy: str
    solver_status: str


async def build_snapshot(version_id: str, states: list[ledger.DebtState], start: dict[str, int],
                         extra_kop: int, first_month: date) -> Snapshot:
    """Розв'язує план від стартових залишків і проводить його платежі через roll_month.

    Платежі розв'язку округлюються half-up до копійки; у місяці, де розв'язок
    погашає борг, плановий платіж дорівнює боргу з відсотками, тож залишок
    стає рівно 0. Ключ боргу в розв'язку — debt_id.

    Args:
        version_id: Id версії для рядків знімка.
        states: Борги (тип, ставка, мінімальний платіж).
        start: Стартові залишки в копійках за debt_id; борги з нулем пропускаються.
        extra_kop: Додатковий місячний платіж, копійки.
        first_month: Перше число першого місяця плану.

    Raises:
        PlanFactError: 409 — немає боргів із залишком; 422 — план неможливий;
            503 — розв'язувач не встиг.
    """
    debts = [s for s in states if start.get(s.id, 0) > 0]
    if not debts:
        raise PlanFactError(409, "Немає боргів із залишком, план будувати немає з чого.")
    dicts = [{"name": s.id, "balance": ledger.kop_to_uah(start[s.id]), "monthly_rate": s.monthly_rate,
              "min_payment": s.min_payment, "debt_type": s.debt_type} for s in debts]
    budget = sum(debt_min_payment(d["debt_type"], d["balance"], d["monthly_rate"], d["min_payment"])
                 for d in dicts) + ledger.kop_to_uah(extra_kop)
    try:
        strategies = await optimize_debt_repayment_async(dicts, budget, PLAN_HORIZON_MONTHS)
    except TimeoutError as exc:
        raise PlanFactError(503, "Розрахунок плану не встиг завершитися. Спробуй ще раз трохи пізніше.") from exc
    except Exception as exc:
        raise PlanFactError(422, "Не вдалося розрахувати план за поточних залишків.") from exc
    milp = strategies["milp"]
    fallback = bool(milp.get("fallback"))
    if milp["status"] in SOLVER_NOT_FINISHED_STATUSES and not fallback:
        raise PlanFactError(503, "Розрахунок плану не встиг завершитися. Спробуй ще раз трохи пізніше.")
    if milp["status"] != "Optimal" and not fallback:
        raise PlanFactError(422, "Не вдалося розрахувати план за поточних залишків і додаткового платежу.")

    balances = {s.id: start[s.id] for s in debts}
    rows: list[DebtPlanSchedule] = []
    total_interest = 0
    months_to_payoff = None
    for k, entry in enumerate(milp["monthly_log"]):
        month = ledger.add_months(first_month, k)
        for s in debts:
            opening = balances[s.id]
            if opening <= 0:
                continue
            payment = ledger.uah_to_kop(entry["payments"].get(s.id, 0))
            roll = ledger.roll_month(opening, s.monthly_rate, payment)
            if roll.closing > 0 and entry["balances"].get(s.id, 0) <= PAID_OFF_TOLERANCE_UAH:
                roll = ledger.roll_month(opening, s.monthly_rate, opening + roll.interest)
            rows.append(DebtPlanSchedule(version_id=version_id, month=month, debt_id=s.id, opening_kop=roll.opening,
                                         interest_kop=roll.interest, planned_payment_kop=roll.paid,
                                         closing_kop=roll.closing))
            total_interest += roll.interest
            balances[s.id] = roll.closing
        if all(b == 0 for b in balances.values()):
            months_to_payoff = k + 1
            break
    return Snapshot(rows=rows, months_to_payoff=months_to_payoff, total_interest_kop=total_interest,
                    strategy="avalanche_fallback" if fallback else "milp", solver_status=milp["status"])


def _apply_snapshot(session: AsyncSession, version: DebtPlanVersion, snapshot: Snapshot,
                    states: list[ledger.DebtState], start: dict[str, int]) -> None:
    """Записує знімок у версію й додає рядки розкладу в сесію."""
    included = [s for s in states if start.get(s.id, 0) > 0]
    version.strategy = snapshot.strategy
    version.solver_status = snapshot.solver_status
    version.months_to_payoff = snapshot.months_to_payoff
    version.total_interest_kop = snapshot.total_interest_kop
    version.structure_fingerprint = debts_structure_fingerprint(included)
    version.materialized_at = datetime.now(timezone.utc)
    session.add_all(snapshot.rows)


# --- прийняття ----------------------------------------------------------------------------

async def accept_plan(session: AsyncSession, user_id: str, plan_id: str,
                      today: date | None = None) -> tuple[DebtPlanVersion, bool]:
    """Приймає чернетку плану: створює нову версію і замінює попередню.

    Повторне прийняття тієї самої чернетки, коли її версія ще прийнята,
    нічого не змінює.

    Returns:
        Пара (версія, створено нову).

    Raises:
        PlanFactError: 404 — чернетку не знайдено; 409 — чернетка без
            додаткового платежу чи відбитка, застаріла, або немає активних
            боргів; 422/503 — див. build_snapshot.
    """
    today = ledger.today_kyiv(today)
    await _lock_user(session, user_id)
    plan = (await session.execute(
        select(MilpPlan).where(MilpPlan.id == plan_id, MilpPlan.user_id == user_id)
    )).scalar_one_or_none()
    if plan is None:
        raise PlanFactError(404, "Чернетку плану не знайдено.")
    if plan.extra_monthly_uah is None or plan.debts_fingerprint is None or plan.debts_structure_fingerprint is None:
        raise PlanFactError(409, "Цю чернетку не можна прийняти: побудуй план заново на сторінці «Борги».")

    current = await accepted_version(session, user_id, for_update=True)
    if current is not None and current.source_plan_id == plan_id:
        return current, False

    states = await ledger.active_debt_states(session, user_id, today)
    if not states:
        raise PlanFactError(409, "Немає активних боргів.")

    versions = await user_versions(session, user_id)
    next_no = max((v.version_no for v in versions), default=0) + 1
    extra_kop = ledger.uah_to_kop(plan.extra_monthly_uah)

    balances_changed, structure_changed = draft_changes(plan, states)
    if current is None:
        if balances_changed or structure_changed:
            raise PlanFactError(409, "Борги змінилися після побудови цієї чернетки: побудуй план заново.")
        version = DebtPlanVersion(
            id=str(uuid.uuid4()), user_id=user_id, source_plan_id=plan.id, version_no=next_no,
            status="accepted", accepted_on=today, effective_from=compute_effective_from(False, today),
            start_kind="acceptance_balances", extra_monthly_kop=extra_kop,
        )
        start = {s.id: s.balance_kop for s in states}
        snapshot = await build_snapshot(version.id, states, start, extra_kop, version.effective_from)
        session.add(version)
        await session.flush()
        _apply_snapshot(session, version, snapshot, states, start)
        for s in states:
            if start[s.id] > 0:
                session.add(DebtBalanceCheck(id=str(uuid.uuid4()), user_id=user_id, debt_id=s.id, as_of=today,
                                             balance_kop=start[s.id], source=ledger.PLAN_ANCHOR_SOURCE))
        await session.commit()
        return version, True

    if structure_changed:
        raise PlanFactError(409, "Склад боргів змінився після побудови цієї чернетки: побудуй план заново.")
    effective_from = compute_effective_from(True, today)
    current.status = "superseded"
    current.effective_to = effective_from
    current.superseded_at = datetime.now(timezone.utc)
    await session.flush()
    version = DebtPlanVersion(
        id=str(uuid.uuid4()), user_id=user_id, source_plan_id=plan.id, version_no=next_no,
        status="accepted", accepted_on=today, effective_from=effective_from,
        start_kind="month_start_actual", extra_monthly_kop=extra_kop,
    )
    session.add(version)
    await session.commit()
    return version, True


# --- матеріалізація -------------------------------------------------------------------------

async def materialize(session: AsyncSession, user_id: str, force: bool = False,
                      today: date | None = None) -> tuple[DebtPlanVersion, bool]:
    """Будує знімок прийнятої версії, що вже набрала чинності, від фактичних залишків.

    Ідемпотентно: для вже матеріалізованої версії нічого не змінює.

    Args:
        force: Дозволяє матеріалізацію до MATERIALIZE_FROM_DAY-го числа.

    Returns:
        Пара (версія, знімок створено зараз).

    Raises:
        PlanFactError: 404 — немає прийнятої версії; 409 — версія ще не діє,
            рано без force, попередній місяць у стані «не внесено»;
            422/503 — див. build_snapshot.
    """
    today = ledger.today_kyiv(today)
    await _lock_user(session, user_id)
    version = await accepted_version(session, user_id, for_update=True)
    if version is None:
        raise PlanFactError(404, "Прийнятого плану немає.")
    if version.materialized_at is not None:
        return version, False
    if today < version.effective_from:
        raise PlanFactError(
            409, f"План діє з {version.effective_from.strftime('%d.%m.%Y')}; зафіксувати його можна після цієї дати."
        )
    if today.day < MATERIALIZE_FROM_DAY and not force:
        raise PlanFactError(
            409,
            f"До {MATERIALIZE_FROM_DAY}-го числа попередній місяць ще можна доповнити. "
            "Щоб зафіксувати попередній місяць зараз, повтори запит з force=true.",
        )
    previous = ledger.add_months(version.effective_from, -1)
    if await month_entry_state(session, user_id, previous) == NOT_ENTERED:
        raise PlanFactError(
            409,
            f"За {ledger.format_month(previous)} не внесено жодного платежу. "
            "Внеси платежі або познач, що платежів не було.",
        )

    debts = (await session.execute(
        select(Debt).where(Debt.user_id == user_id, Debt.is_active == True)  # noqa: E712
        .order_by(Debt.created_at, Debt.id)
    )).scalars().all()
    if not debts:
        raise PlanFactError(409, "Немає активних боргів.")
    start = await ledger.balances_as_of(session, user_id, list(debts), ledger.month_end(previous))
    states = [ledger.to_state(d, start[d.id]) for d in debts]
    extra_kop = version.extra_monthly_kop
    snapshot = await build_snapshot(version.id, states, start, extra_kop, version.effective_from)
    _apply_snapshot(session, version, snapshot, states, start)
    await session.commit()
    return version, True


# --- перевірки змін ------------------------------------------------------------------------

async def ensure_month_editable(session: AsyncSession, user_id: str, day: date, today: date) -> None:
    """Перевіряє, що дату day можна змінювати (платежі, звірки, позначки).

    Raises:
        PlanFactError: 422 — дата в майбутньому; 409 — місяць заблоковано.
    """
    if day > today:
        raise PlanFactError(422, "Дата не може бути в майбутньому.")
    versions = await user_versions(session, user_id)
    if month_locked(versions, ledger.month_start(day)):
        raise PlanFactError(
            409, f"Місяць {ledger.format_month(ledger.month_start(day))} закрито: він належить попередній версії плану."
        )


async def ensure_payment_allowed(session: AsyncSession, user_id: str, paid_on: date, today: date) -> None:
    """Перевіряє дату платежу: не в майбутньому, не раніше дати прийняття першого плану, місяць не заблоковано.

    Raises:
        PlanFactError: 422 — дата в майбутньому; 409 — плану ще немає, дата
            раніше прийняття першого плану або місяць заблоковано.
    """
    if paid_on > today:
        raise PlanFactError(422, "Дата не може бути в майбутньому.")
    first = first_version(await user_versions(session, user_id))
    if first is None:
        raise PlanFactError(409, "Платежі вносяться після прийняття плану.")
    if paid_on < first.accepted_on:
        raise PlanFactError(
            409,
            f"Платіж раніше дати прийняття плану ({first.accepted_on.strftime('%d.%m.%Y')}) не входить у порівняння.",
        )
    await ensure_month_editable(session, user_id, paid_on, today)


# --- план-факт -------------------------------------------------------------------------------

def _sum_or_none(values: list) -> int | None:
    """Сума значень або None, якщо хоч одне невідоме."""
    return None if any(v is None for v in values) else sum(values)


def _sum_known(values: list) -> int | None:
    """Сума відомих значень або None, якщо відомих немає."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _diff(a: int | None, b: int | None) -> int | None:
    """a − b або None."""
    return None if a is None or b is None else a - b


async def plan_fact(session: AsyncSession, user_id: str, from_month: date | None, to_month: date | None,
                    today: date | None = None) -> dict:
    """Складає порівняння плану й факту по місяцях і боргах (суми в копійках).

    Лише читає дані. Місяць від поточного й пізніше відкритий: його рядки
    мають remaining_to_plan_kop (max(0, план − факт)) і не входять у
    cumulative; cumulative рахується лише по закритих місяцях. У місяці зі
    звіркою (check) balance_diff_kop боргу — None: залишок після звірки не
    містить відсотків решти місяця. Розбіжність залишку боргу в cumulative —
    з останнього закритого місяця, де вона відома, а після місяця зі звіркою
    — None; загальна — сума відомих.

    Raises:
        PlanFactError: 404 — плану ще не прийнято; 422 — from_month > to_month.
    """
    today = ledger.today_kyiv(today)
    versions = await user_versions(session, user_id)
    first = first_version(versions)
    if first is None:
        raise PlanFactError(404, "Прийнятого плану ще немає.")
    start = first.effective_from
    current_month = ledger.month_start(today)
    from_month = max(from_month or start, start)
    to_month = to_month or current_month
    if from_month > to_month:
        raise PlanFactError(422, "Початковий місяць пізніший за кінцевий.")

    all_debts = (await session.execute(
        select(Debt).where(Debt.user_id == user_id).order_by(Debt.created_at, Debt.id)
    )).scalars().all()
    debt_by_id = {d.id: d for d in all_debts}
    anchors, payments = await ledger.load_ledger(session, list(debt_by_id))

    version_ids = [v.id for v in versions]
    schedule_rows = (await session.execute(
        select(DebtPlanSchedule).where(DebtPlanSchedule.version_id.in_(version_ids))
    )).scalars().all() if version_ids else []
    planned = {(r.version_id, r.month, r.debt_id): r for r in schedule_rows}

    marks = {m.month: m.no_payments_confirmed for m in (await session.execute(
        select(DebtPlanMonth).where(DebtPlanMonth.user_id == user_id)
    )).scalars().all()}
    pay_months = {ledger.month_start(d) for lst in payments.values() for d, _ in lst}

    def entry_state(month: date) -> str:
        if month in pay_months:
            return ENTERED
        return NO_PAYMENTS_CONFIRMED if marks.get(month) else NOT_ENTERED

    # Залишок на кінець кожного місяця, зокрема поточного (з уже внесеними
    # платежами): так фактичний і плановий залишки порівнюються на ту саму дату.
    through = ledger.month_end(to_month)
    facts: dict[str, dict[date, ledger.MonthFact]] = {}
    known: dict[str, dict[date, bool]] = {}
    for debt_id, debt in debt_by_id.items():
        series = ledger.debt_series(anchors[debt_id], payments[debt_id], debt.monthly_rate, start, through)
        facts[debt_id] = {f.month: f for f in series}
        flags, is_known = {}, True
        for f in series:
            state = entry_state(f.month)
            if f.anchor is not None and not f.anchor.starts_day:
                is_known = True
            elif f.anchor is not None:
                is_known = state != NOT_ENTERED
            else:
                is_known = is_known and state != NOT_ENTERED
            flags[f.month] = is_known
        known[debt_id] = flags

    months_out = []
    cumulative: dict[str, dict] = {}
    month = from_month
    while month <= to_month:
        gov = next((v for v in versions if version_covers(v, month)), None)
        state = entry_state(month)
        is_open = month >= current_month
        debt_ids = []
        for debt_id, debt in debt_by_id.items():
            in_plan = gov is not None and (gov.id, month, debt_id) in planned
            has_fact = month in facts[debt_id] and (debt.is_active or any(
                ledger.month_start(d) == month for d, _ in payments[debt_id]))
            if in_plan or has_fact:
                debt_ids.append(debt_id)

        rows = []
        for debt_id in debt_ids:
            plan_row = planned.get((gov.id, month, debt_id)) if gov is not None else None
            fact = facts[debt_id].get(month)
            fact_known = fact is not None and known[debt_id].get(month, False)
            actual_payment = None
            if state != NOT_ENTERED and fact is not None:
                actual_payment = sum(p for d, p in payments[debt_id] if ledger.month_start(d) == month)
            planned_payment = plan_row.planned_payment_kop if plan_row else None
            planned_closing = plan_row.closing_kop if plan_row else None
            actual_closing = fact.closing if fact_known else None
            check = None
            if fact is not None and fact.anchor is not None and not fact.anchor.starts_day:
                check = {"as_of": fact.anchor.as_of, "balance_kop": fact.anchor.balance_kop,
                         "source": fact.anchor.source}
            row = {
                "debt_id": debt_id,
                "name": debt_by_id[debt_id].name,
                "planned_payment_kop": planned_payment,
                "actual_payment_kop": actual_payment,
                "payment_diff_kop": _diff(actual_payment, planned_payment),
                "planned_closing_kop": planned_closing,
                "actual_closing_kop": actual_closing,
                # Залишок місяця зі звіркою не містить відсотків решти місяця, тож не порівнюється.
                "balance_diff_kop": None if check is not None else _diff(actual_closing, planned_closing),
                "overpaid_kop": fact.overpaid if fact is not None and state != NOT_ENTERED else None,
                "remaining_to_plan_kop": (max(planned_payment - (actual_payment or 0), 0)
                                          if is_open and planned_payment is not None else None),
                "check": check,
            }
            rows.append(row)
            if is_open:
                continue
            acc = cumulative.setdefault(debt_id, {"debt_id": debt_id, "name": row["name"],
                                                  "payment_diff_kop": 0, "balance_diff_kop": None})
            if row["payment_diff_kop"] is not None:
                acc["payment_diff_kop"] += row["payment_diff_kop"]
            if check is not None:
                acc["balance_diff_kop"] = None
            elif row["balance_diff_kop"] is not None:
                acc["balance_diff_kop"] = row["balance_diff_kop"]

        planned_rows = [r for r in rows if r["planned_payment_kop"] is not None]
        months_out.append({
            "month": month,
            "version_id": gov.id if gov else None,
            "version_no": gov.version_no if gov else None,
            "materialized": bool(gov and gov.materialized_at is not None),
            "closed": month < current_month,
            "editable": (not month_locked(versions, month)) and month <= current_month,
            "entry_state": state,
            "counted_from": first.accepted_on if month == first.effective_from else None,
            "debts": rows,
            "totals": {
                "planned_payment_kop": sum(r["planned_payment_kop"] for r in planned_rows) if planned_rows else None,
                "actual_payment_kop": _sum_or_none([r["actual_payment_kop"] for r in rows]) if rows else None,
                "payment_diff_kop": _sum_or_none([r["payment_diff_kop"] for r in planned_rows]) if planned_rows else None,
                "planned_closing_kop": sum(r["planned_closing_kop"] for r in planned_rows) if planned_rows else None,
                "actual_closing_kop": _sum_or_none([r["actual_closing_kop"] for r in rows]) if rows else None,
                "balance_diff_kop": _sum_or_none([r["balance_diff_kop"] for r in planned_rows]) if planned_rows else None,
                "remaining_to_plan_kop": (sum(r["remaining_to_plan_kop"] for r in planned_rows)
                                          if is_open and planned_rows else None),
            },
        })
        month = ledger.add_months(month, 1)

    cum = list(cumulative.values())
    return {
        "months": months_out,
        "cumulative": {
            "debts": cum,
            "payment_diff_kop": sum(c["payment_diff_kop"] for c in cum),
            "balance_diff_kop": _sum_known([c["balance_diff_kop"] for c in cum]),
        },
    }
