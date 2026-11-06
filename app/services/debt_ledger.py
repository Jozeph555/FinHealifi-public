"""
Журнал боргу: залишок з звірок і платежів, одна формула місяця для плану й факту.

Залишок боргу на дату D рахує лише цей модуль (balance_as_of / debt_series):
- опора — остання звірка (debt_balance_checks) з датою <= D; звірки на одну
  дату впорядковуються за часом створення;
- залишок на дату D = залишок останньої звірки <= D мінус платежі після неї;
- відсотки місяця нараховуються на кінець місяця (останній день): усередині
  місяця до залишку вони не входять;
- до першої прийнятої версії плану залишок дорівнює останній звірці;
- з місяця дії першої версії (ledger_start) кожен завершений місяць проходить
  через roll_month: відсотки = opening · ставка з округленням half-up до
  копійки, потім віднімаються платежі місяця;
- звірка 'plan_accepted' діє з початку свого дня: платежі з датою >= as_of
  віднімаються, на кінець її місяця нараховуються відсотки на її залишок
  (як у першому місяці плану);
- інші звірки — залишок на кінець дня as_of: віднімаються платежі після
  as_of, відсотків до кінця її місяця немає.

Debt.balance — лише запасне значення для боргу без жодної звірки.
Усі суми — цілі копійки; гривні з Float переводяться через Decimal(str(x)).
"""
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import today_override
from app.models.debt import Debt
from app.models.debt_plan import DebtBalanceCheck, DebtPayment, DebtPlanVersion

KYIV_TZ = ZoneInfo("Europe/Kyiv")
PLAN_ANCHOR_SOURCE = "plan_accepted"


# --- дати ---------------------------------------------------------------------------

def today_kyiv(today: date | datetime | None = None) -> date:
    """Повертає календарну дату за Europe/Kyiv.

    Args:
        today: Дата (повертається як є), aware-datetime (переводиться в
            Europe/Kyiv) або None — дата з APP_TODAY_OVERRIDE, якщо її задано,
            інакше поточний момент.
    """
    if today is None:
        override = today_override()
        return override if override is not None else datetime.now(KYIV_TZ).date()
    if isinstance(today, datetime):
        if today.tzinfo is None:
            raise ValueError("datetime без часової зони")
        return today.astimezone(KYIV_TZ).date()
    return today


def month_start(d: date) -> date:
    """Повертає перше число місяця дати d."""
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    """Повертає перше число місяця, зсунутого на n від місяця дати d."""
    index = d.year * 12 + (d.month - 1) + n
    return date(index // 12, index % 12 + 1, 1)


def month_end(d: date) -> date:
    """Повертає останній день місяця дати d."""
    return add_months(d, 1) - timedelta(days=1)


def parse_month(value: str) -> date:
    """Перетворює рядок YYYY-MM на перше число місяця.

    Raises:
        ValueError: рядок не у форматі YYYY-MM.
    """
    parsed = datetime.strptime(value, "%Y-%m")
    if len(value) != 7:
        raise ValueError("очікується формат YYYY-MM")
    return parsed.date().replace(day=1)


def format_month(d: date) -> str:
    """Повертає місяць дати d у форматі YYYY-MM."""
    return d.strftime("%Y-%m")


MONTHS_NOMINATIVE = (
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
)


def month_name(d: date) -> str:
    """Місяць дати d у називному відмінку для текстів користувачу: «листопад 2026 р.»."""
    return f"{MONTHS_NOMINATIVE[d.month - 1]} {d.year} р."


MONTHS_GENITIVE = (
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
)


def date_text(d: date) -> str:
    """Дата для текстів користувачу: «1 листопада 2026»."""
    return f"{d.day} {MONTHS_GENITIVE[d.month - 1]} {d.year}"


def month_closed(month: date, today: date) -> bool:
    """Чи місяць закрито: він строго раніше за місяць дати today."""
    return month_start(month) < month_start(today)


# --- гроші --------------------------------------------------------------------------

def uah_to_kop(value) -> int:
    """Переводить гривні (float, int, Decimal або рядок) у цілі копійки, half-up."""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def kop_to_uah(kop: int | None) -> float | None:
    """Переводить копійки в гривні для відповіді API (None лишається None)."""
    if kop is None:
        return None
    return float(Decimal(kop) / 100)


def interest_kop(opening_kop: int, monthly_rate: float) -> int:
    """Відсотки за місяць: opening · ставка, округлення half-up до копійки."""
    return int((Decimal(opening_kop) * Decimal(str(monthly_rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class MonthRoll:
    """Результат місяця: залишок на початок, відсотки, сплачено, залишок на кінець, переплата."""
    opening: int
    interest: int
    paid: int
    closing: int
    overpaid: int


def roll_month(opening_kop: int, monthly_rate: float, payments_kop: int) -> MonthRoll:
    """Проводить місяць боргу: нараховує відсотки й віднімає платежі.

    Одна формула для планового знімка і для факту.

    Args:
        opening_kop: Залишок на початок місяця, копійки.
        monthly_rate: Місячна ставка (0.03 = 3 %).
        payments_kop: Сума платежів місяця, копійки.

    Returns:
        MonthRoll; платежі понад борг з відсотками йдуть у overpaid, closing не
        буває від'ємним.
    """
    interest = interest_kop(opening_kop, monthly_rate) if opening_kop > 0 else 0
    due = opening_kop + interest
    paid = min(payments_kop, due)
    return MonthRoll(opening=opening_kop, interest=interest, paid=paid, closing=due - paid,
                     overpaid=payments_kop - paid)


# --- ряд по місяцях (чиста функція) -------------------------------------------------

@dataclass(frozen=True)
class Anchor:
    """Звірка для розрахунку: дата, залишок у копійках, джерело."""
    as_of: date
    balance_kop: int
    source: str

    @property
    def starts_day(self) -> bool:
        """Чи діє звірка з початку дня (стартовий залишок плану)."""
        return self.source == PLAN_ANCHOR_SOURCE


@dataclass(frozen=True)
class MonthFact:
    """Розрахований місяць боргу.

    anchor — звірка, від якої рахується місяць (остання в цьому місяці), або
    None, якщо місяць продовжує попередній. opening і interest — None для
    місяця зі звичайною звіркою. complete — чи місяць враховано до останнього
    дня: лише тоді interest нараховано й входить у closing.
    """
    month: date
    opening: int | None
    interest: int | None
    paid: int
    closing: int
    overpaid: int
    anchor: Anchor | None
    complete: bool = True


def _partial_month(opening: int, monthly_rate: float, payments: int) -> tuple[int, int, int]:
    """Місяць без відсотків до його кінця: (сплачено, залишок, переплата).

    Переплата — понад залишок разом із відсотками, що нарахуються на кінець
    місяця, тож вона не зникає після їх нарахування.
    """
    pending = interest_kop(opening, monthly_rate) if opening > 0 else 0
    paid = min(payments, opening + pending)
    return paid, max(opening - paid, 0), payments - paid


def debt_series(anchors: list[Anchor], payments: list[tuple[date, int]], monthly_rate: float,
                ledger_start: date | None, through: date) -> list[MonthFact]:
    """Рахує залишок боргу по місяцях від найранішої звірки до дати through.

    Args:
        anchors: Звірки боргу в порядку (as_of, час створення).
        payments: Пари (дата, сума в копійках).
        monthly_rate: Місячна ставка боргу.
        ledger_start: Перше число місяця дії першої версії плану; None — версій
            немає, і залишок дорівнює останній звірці без відсотків.
        through: Остання дата, що враховується (платежі й звірки після неї
            ігноруються). Відсотки місяця нараховуються, лише якщо through —
            останній день місяця або пізніше.

    Returns:
        Список MonthFact від місяця першої звірки до місяця through; порожній,
        якщо звірок до through немає. closing останнього елемента — залишок
        на кінець дня through.
    """
    usable = [a for a in anchors if a.as_of <= through]
    if not usable:
        return []
    pays = [(d, amount) for d, amount in payments if d <= through]

    if ledger_start is None:
        last = usable[-1]
        return [MonthFact(month=month_start(last.as_of), opening=None, interest=None, paid=0,
                          closing=last.balance_kop, overpaid=0, anchor=last)]

    by_month: dict[date, Anchor] = {}
    for a in usable:
        by_month[month_start(a.as_of)] = a  # остання звірка місяця перекриває ранні

    series: list[MonthFact] = []
    month = month_start(usable[0].as_of)
    last_month = month_start(through)
    closing: int | None = None
    while month <= last_month:
        anchor = by_month.get(month)
        rate = monthly_rate if month >= ledger_start else 0.0
        complete = through >= month_end(month)
        if anchor is not None and not anchor.starts_day:
            paid_raw = sum(p for d, p in pays if anchor.as_of < d and month_start(d) == month)
            paid = min(paid_raw, anchor.balance_kop)
            fact = MonthFact(month, None, None, paid, anchor.balance_kop - paid, paid_raw - paid, anchor, complete)
        else:
            if anchor is not None:
                opening = anchor.balance_kop
                paid_raw = sum(p for d, p in pays if anchor.as_of <= d and month_start(d) == month)
            else:
                opening = closing
                paid_raw = sum(p for d, p in pays if month_start(d) == month)
            if complete:
                roll = roll_month(opening, rate, paid_raw)
                fact = MonthFact(month, roll.opening, roll.interest, roll.paid, roll.closing, roll.overpaid,
                                 anchor, True)
            else:
                paid, partial_closing, overpaid = _partial_month(opening, rate, paid_raw)
                fact = MonthFact(month, opening, 0, paid, partial_closing, overpaid, anchor, False)
        series.append(fact)
        closing = fact.closing
        month = add_months(month, 1)
    return series


# --- завантаження з БД ----------------------------------------------------------------

@dataclass(frozen=True)
class DebtState:
    """Борг із залишком з журналу; атрибути збігаються з Debt для оптимізаторів і відбитків."""
    id: str
    name: str
    debt_type: str
    monthly_rate: float
    min_payment: float | None
    is_active: bool
    balance_kop: int

    @property
    def balance(self) -> float:
        """Залишок у гривнях."""
        return kop_to_uah(self.balance_kop)


async def ledger_start(session: AsyncSession, user_id: str) -> date | None:
    """Повертає effective_from першої матеріалізованої версії плану користувача або None."""
    return (await session.execute(
        select(func.min(DebtPlanVersion.effective_from)).where(
            DebtPlanVersion.user_id == user_id, DebtPlanVersion.materialized_at.is_not(None)
        )
    )).scalar_one_or_none()


async def load_ledger(session: AsyncSession, debt_ids: list[str]) -> tuple[dict, dict]:
    """Завантажує звірки й платежі боргів.

    Returns:
        Пара словників за debt_id: список Anchor у порядку (as_of, created_at) і
        список пар (дата, копійки).
    """
    anchors: dict[str, list[Anchor]] = {i: [] for i in debt_ids}
    payments: dict[str, list[tuple[date, int]]] = {i: [] for i in debt_ids}
    if not debt_ids:
        return anchors, payments
    rows = (await session.execute(
        select(DebtBalanceCheck).where(DebtBalanceCheck.debt_id.in_(debt_ids))
        .order_by(DebtBalanceCheck.as_of, DebtBalanceCheck.created_at, DebtBalanceCheck.id)
    )).scalars().all()
    for c in rows:
        anchors[c.debt_id].append(Anchor(c.as_of, c.balance_kop, c.source))
    pay_rows = (await session.execute(
        select(DebtPayment.debt_id, DebtPayment.paid_on, DebtPayment.amount_kop)
        .where(DebtPayment.debt_id.in_(debt_ids))
    )).all()
    for debt_id, paid_on, amount in pay_rows:
        payments[debt_id].append((paid_on, amount))
    return anchors, payments


def _balance_from(debt: Debt, anchors: list[Anchor], payments: list[tuple[date, int]],
                  start: date | None, as_of: date) -> int:
    """Залишок боргу на кінець дня as_of; без звірок — Debt.balance."""
    series = debt_series(anchors, payments, debt.monthly_rate, start, as_of)
    if not series:
        return uah_to_kop(debt.balance)
    return series[-1].closing


async def balances_as_of(session: AsyncSession, user_id: str, debts: list[Debt], as_of: date) -> dict[str, int]:
    """Повертає залишки боргів на кінець дня as_of, копійки за debt_id."""
    start = await ledger_start(session, user_id)
    anchors, payments = await load_ledger(session, [d.id for d in debts])
    return {d.id: _balance_from(d, anchors[d.id], payments[d.id], start, as_of) for d in debts}


async def balance_as_of(session: AsyncSession, debt: Debt, as_of: date) -> int:
    """Повертає залишок одного боргу на кінець дня as_of, копійки."""
    return (await balances_as_of(session, debt.user_id, [debt], as_of))[debt.id]


def to_state(debt: Debt, balance_kop: int) -> DebtState:
    """Складає DebtState з боргу й залишку."""
    return DebtState(id=debt.id, name=debt.name, debt_type=debt.debt_type, monthly_rate=debt.monthly_rate,
                     min_payment=debt.min_payment, is_active=bool(debt.is_active), balance_kop=balance_kop)


async def debt_states(session: AsyncSession, user_id: str, debts: list[Debt],
                      today: date | None = None) -> list[DebtState]:
    """Повертає DebtState для переданих боргів із залишком на сьогодні (Europe/Kyiv)."""
    balances = await balances_as_of(session, user_id, list(debts), today_kyiv(today))
    return [to_state(d, balances[d.id]) for d in debts]


async def active_debt_states(session: AsyncSession, user_id: str, today: date | None = None) -> list[DebtState]:
    """Повертає активні борги користувача із залишком з журналу на сьогодні."""
    debts = (await session.execute(
        select(Debt).where(Debt.user_id == user_id, Debt.is_active == True)  # noqa: E712
        .order_by(Debt.created_at, Debt.id)
    )).scalars().all()
    return await debt_states(session, user_id, debts, today)


async def upsert_check(session: AsyncSession, user_id: str, debt_id: str, as_of: date, balance_kop: int,
                       source: str) -> tuple[DebtBalanceCheck, bool]:
    """Створює звірку або замінює наявну на ту саму дату (стартові залишки плану не замінюються).

    Замінена звірка отримує новий created_at, тож серед якорів тієї самої дати
    вона діє як найпізніша.

    Returns:
        Пара (звірка, створено нову).
    """
    existing = (await session.execute(
        select(DebtBalanceCheck).where(
            DebtBalanceCheck.debt_id == debt_id, DebtBalanceCheck.as_of == as_of,
            DebtBalanceCheck.source != PLAN_ANCHOR_SOURCE,
        )
    )).scalar_one_or_none()
    if existing is not None:
        existing.balance_kop = balance_kop
        existing.source = source
        existing.created_at = func.clock_timestamp()
        await session.flush()
        await session.refresh(existing, ["created_at"])
        return existing, False
    check = DebtBalanceCheck(id=str(uuid.uuid4()), user_id=user_id, debt_id=debt_id, as_of=as_of,
                             balance_kop=balance_kop, source=source)
    session.add(check)
    return check, True
