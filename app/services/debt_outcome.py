"""
Наслідок план-факту: прогноз закриття боргів від фактичних залишків.

Прогноз будується від фактичних залишків на початок поточного місяця (журнал
боргу: звірки й платежі) за стратегією чинної версії: її місячний бюджет
(обов'язкові платежі від стартових залишків розкладу + додатковий платіж)
розподіляється симуляцією avalanche (debt_optimizer.simulate_strategy), а
платежі симуляції проводяться через roll_month так само, як знімок версії.

Переплата прогнозу = відсотки минулих місяців версії (з журналу; для місяця
зі звіркою чи без внесених платежів — планові) + відсотки симуляції від
поточного місяця. Вона порівнюється із total_interest версії.

Місяці від початку першого плану до попереднього включно, у яких платежів не
внесено й не позначено «платежів не було», роблять прогноз неможливим
(status "unresolved_months"), якщо після них немає звірки залишку за
випискою для кожного боргу.
"""
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.debt import Debt
from app.models.debt_plan import DebtPayment, DebtPlanMonth, DebtPlanSchedule, DebtPlanVersion
from app.services import debt_ledger as ledger
from app.services import debt_plan_versions as versions_service
from app.services.debt_optimizer import simulate_strategy
from app.services.debt_plan_versions import PLAN_HORIZON_MONTHS, PlanFactError
from app.services.debt_rules import debt_min_payment

OUTCOME_STRATEGY = "avalanche"

STATUS_OK = "ok"
STATUS_UNRESOLVED = "unresolved_months"
STATUS_PAID_OFF = "paid_off"

MONTHS_LOCATIVE = (
    "січні", "лютому", "березні", "квітні", "травні", "червні",
    "липні", "серпні", "вересні", "жовтні", "листопаді", "грудні",
)


# --- тексти ---------------------------------------------------------------------------------

def whole_uah(kop: int) -> int:
    """Переводить копійки в цілі гривні, половина — від нуля."""
    sign = -1 if kop < 0 else 1
    return sign * int((Decimal(abs(kop)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def month_in_text(month: date) -> str:
    """Місяць у місцевому відмінку з прийменником: «в березні 2028 р.», «у вересні 2027 р.»."""
    name = MONTHS_LOCATIVE[month.month - 1]
    preposition = "у" if name.startswith("в") else "в"
    return f"{preposition} {name} {month.year} р."


def month_name(month: date) -> str:
    """Місяць у називному відмінку: «листопад 2026 р.»."""
    return ledger.month_name(month)


def _join(items: list[str]) -> str:
    """Перелік через кому з «і» перед останнім."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " і " + items[-1]


def unresolved_sentence(months: list[date]) -> str:
    """Речення для місяців без внесених платежів."""
    return (f"Щоб порахувати наслідок, внеси платежі за {_join([month_name(m) for m in months])} "
            "або познач, що їх не було.")


def _group(value: int) -> str:
    """Ціле число з пробілом між тисячами."""
    return f"{value:,}".replace(",", " ")


def _comparison_tail(shift: int | None, interest_diff: int | None) -> str:
    """Частина речення про зсув строку й різницю переплати проти чинного плану ("" — порівняння немає)."""
    tail = ""
    if shift is not None:
        if shift > 0:
            tail = f", на {shift} міс. пізніше, ніж у чинному плані"
        elif shift < 0:
            tail = f", на {-shift} міс. раніше, ніж у чинному плані"
        else:
            tail = ", як і в чинному плані"
    if interest_diff is not None:
        if interest_diff > 0:
            money = f"переплата більша на {_group(interest_diff)} грн"
        elif interest_diff < 0:
            money = f"переплата менша на {_group(-interest_diff)} грн"
        else:
            money = "переплата така сама"
        tail += ("; " if tail else ", ") + money
    return tail + "." if tail else ""


def outcome_sentence(status: str, payoff_month: date | None, shift: int | None,
                     interest_diff: int | None, horizon: int = PLAN_HORIZON_MONTHS) -> str:
    """Складає нейтральне речення наслідку.

    Args:
        status: STATUS_OK або STATUS_PAID_OFF.
        payoff_month: Місяць закриття боргів за прогнозом (None — не за горизонт
            або, для paid_off, місяць невідомий).
        shift: Зсув у місяцях проти чинного плану (None — порівняння немає).
        interest_diff: Різниця переплати в цілих гривнях (None — порівняння немає).
        horizon: Горизонт прогнозу, міс.
    """
    if status == STATUS_PAID_OFF:
        if payoff_month is None:
            return "За внесеними платежами всі борги закрито."
        return f"За внесеними платежами всі борги закрито {month_in_text(payoff_month)}" + \
            _comparison_tail(shift, interest_diff)
    if payoff_month is None:
        return f"За внесеними платежами борги не закриються за {horizon} міс., тому переплата не порівнюється."
    return f"За внесеними платежами борги закриються {month_in_text(payoff_month)}" + \
        _comparison_tail(shift, interest_diff)


# --- розрахунок -----------------------------------------------------------------------------

@dataclass(frozen=True)
class _Ledger:
    """Дані журналу для прогнозу."""
    debts: list[Debt]
    anchors: dict
    payments: dict
    series: dict[str, list[ledger.MonthFact]]


def _months_between(a: date, b: date) -> int:
    """Кількість місяців від a до b (перші числа місяців)."""
    return (b.year - a.year) * 12 + (b.month - a.month)


def _opening_kop(debt: Debt, series: list[ledger.MonthFact], payments: list[tuple[date, int]],
                 current: date) -> int:
    """Залишок боргу на початок поточного місяця з журналу.

    Місяць зі звіркою за випискою: залишок звірки плюс платежі місяця до її
    дати (усередині місяця відсотки до залишку не входять). Без звірок —
    Debt.balance.
    """
    if not series:
        return ledger.uah_to_kop(debt.balance)
    fact = series[-1]
    if fact.anchor is not None and not fact.anchor.starts_day:
        return fact.anchor.balance_kop + sum(
            p for d, p in payments if ledger.month_start(d) == current and d <= fact.anchor.as_of)
    return fact.opening


def _unresolved(data: _Ledger, entry_state, first_from: date, current: date) -> list[date]:
    """Закриті місяці без внесених платежів, від яких залежить поточний залишок хоч одного боргу.

    Місяць не впливає на борг, якщо в журналі боргу є місяць зі звіркою за
    випискою (опора місяця — не стартовий залишок плану) у цьому місяці чи
    пізніше.
    """
    last_statement: dict[str, date | None] = {}
    for d in data.debts:
        months = [f.month for f in data.series[d.id] if f.anchor is not None and not f.anchor.starts_day]
        last_statement[d.id] = max(months) if months else None
    result = []
    month = first_from
    while month < current:
        if entry_state(month) == versions_service.NOT_ENTERED and any(
                last_statement[d.id] is None or last_statement[d.id] < month for d in data.debts):
            result.append(month)
        month = ledger.add_months(month, 1)
    return result


def _past_interest(data: _Ledger, entry_state, planned: dict, version: DebtPlanVersion, current: date) -> int:
    """Відсотки місяців версії до поточного: з журналу, а для місяця зі звіркою чи без платежів — планові."""
    total = 0
    month = version.effective_from
    while month < current:
        for d in data.debts:
            fact = next((f for f in data.series[d.id] if f.month == month), None)
            plan_row = planned.get((month, d.id))
            if (fact is not None and fact.interest is not None and fact.complete
                    and entry_state(month) != versions_service.NOT_ENTERED):
                total += fact.interest
            elif plan_row is not None:
                total += plan_row.interest_kop
        month = ledger.add_months(month, 1)
    return total


def _paid_off_month(data: _Ledger) -> date | None:
    """Останній місяць, у якому залишок боргу з додатного став нулем."""
    result = None
    for d in data.debts:
        previous = None
        for f in data.series[d.id]:
            before = f.opening if f.opening is not None else previous
            if f.closing == 0 and (before or 0) > 0:
                result = f.month if result is None else max(result, f.month)
            previous = f.closing
    return result


async def plan_outcome(session: AsyncSession, user_id: str, today: date | None = None) -> dict:
    """Рахує наслідок внесених платежів для чинної версії плану (лише читає дані).

    Returns:
        Словник: status ("ok" | "unresolved_months" | "paid_off"), version_id,
        version_no, payoff_month і version_payoff_month (перші числа місяців або
        None), payoff_shift_months, projected_total_interest_kop,
        version_total_interest_kop, interest_diff_kop, unresolved_months
        (перші числа місяців), sentence.

    Raises:
        PlanFactError: 404 — прийнятого плану немає або жодна версія не чинна
            в поточному місяці.
    """
    today = ledger.today_kyiv(today)
    current = ledger.month_start(today)
    versions = await versions_service.user_versions(session, user_id)
    first = versions_service.first_version(versions)
    version = next((v for v in versions if versions_service.version_covers(v, current)), None)
    if first is None or version is None:
        raise PlanFactError(404, "Прийнятого плану ще немає.")

    debts = list((await session.execute(
        select(Debt).where(Debt.user_id == user_id, Debt.is_active == True)  # noqa: E712
        .order_by(Debt.created_at, Debt.id)
    )).scalars().all())
    anchors, payments = await ledger.load_ledger(session, [d.id for d in debts])
    series = {d.id: ledger.debt_series(anchors[d.id], payments[d.id], d.monthly_rate, first.effective_from, today)
              for d in debts}
    data = _Ledger(debts, anchors, payments, series)

    pay_months = {ledger.month_start(d) for (d,) in (await session.execute(
        select(DebtPayment.paid_on).where(DebtPayment.user_id == user_id, DebtPayment.paid_on < current)
    )).all()}
    marks = {m.month: m.no_payments_confirmed for m in (await session.execute(
        select(DebtPlanMonth).where(DebtPlanMonth.user_id == user_id)
    )).scalars().all()}

    def entry_state(month: date) -> str:
        if month in pay_months:
            return versions_service.ENTERED
        if marks.get(month) and ledger.month_closed(month, today):
            return versions_service.NO_PAYMENTS_CONFIRMED
        return versions_service.NOT_ENTERED

    result = {
        "status": STATUS_OK, "version_id": version.id, "version_no": version.version_no,
        "payoff_month": None, "version_payoff_month": None, "payoff_shift_months": None,
        "projected_total_interest_kop": None, "version_total_interest_kop": None, "interest_diff_kop": None,
        "unresolved_months": [], "sentence": None,
    }
    unresolved = _unresolved(data, entry_state, first.effective_from, current)
    if unresolved:
        result.update(status=STATUS_UNRESOLVED, unresolved_months=unresolved,
                      sentence=unresolved_sentence(unresolved))
        return result

    rows = (await session.execute(
        select(DebtPlanSchedule).where(DebtPlanSchedule.version_id == version.id)
    )).scalars().all()
    planned = {(r.month, r.debt_id): r for r in rows}
    materialized = version.materialized_at is not None
    version_payoff = (ledger.add_months(version.effective_from, version.months_to_payoff - 1)
                      if materialized and version.months_to_payoff else None)
    version_interest = version.total_interest_kop if materialized else None

    start = {d.id: _opening_kop(d, series[d.id], payments[d.id], current) for d in debts}
    past_interest = _past_interest(data, entry_state, planned, version, current)
    live = [ledger.to_state(d, start[d.id]) for d in debts if start[d.id] > 0]

    if not live:
        status, payoff, projected_interest = STATUS_PAID_OFF, _paid_off_month(data), past_interest
    else:
        all_debts = {d.id: d for d in (await session.execute(
            select(Debt).where(Debt.user_id == user_id))).scalars().all()}
        in_schedule = {r.debt_id for r in rows}
        min_total = versions_service.schedule_min_payments_total(list(rows), all_debts) or 0.0
        min_total += sum(debt_min_payment(s.debt_type, s.balance, s.monthly_rate, s.min_payment)
                         for s in live if s.id not in in_schedule)
        budget = min_total + ledger.kop_to_uah(version.extra_monthly_kop)
        dicts = [{"name": s.id, "balance": s.balance, "monthly_rate": s.monthly_rate,
                  "min_payment": s.min_payment, "debt_type": s.debt_type} for s in live]
        simulation = simulate_strategy(dicts, budget, PLAN_HORIZON_MONTHS, OUTCOME_STRATEGY)
        rolled = versions_service.roll_solution(live, start, simulation["monthly_log"])
        status = STATUS_OK
        payoff = (ledger.add_months(current, rolled.months_to_payoff - 1)
                  if rolled.months_to_payoff is not None else None)
        projected_interest = past_interest + rolled.total_interest_kop if payoff is not None else None

    shift = _months_between(version_payoff, payoff) if payoff and version_payoff else None
    diff_kop = (projected_interest - version_interest
                if projected_interest is not None and version_interest is not None else None)
    diff_whole = (whole_uah(projected_interest) - whole_uah(version_interest)) if diff_kop is not None else None
    result.update(
        status=status, payoff_month=payoff, version_payoff_month=version_payoff, payoff_shift_months=shift,
        projected_total_interest_kop=projected_interest, version_total_interest_kop=version_interest,
        interest_diff_kop=diff_kop,
        sentence=outcome_sentence(status, payoff, shift, diff_whole),
    )
    return result
