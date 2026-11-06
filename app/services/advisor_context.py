"""
Контекст для AI-радника, зібраний з БД на боці сервера.

Контекст — список готових речень українською: модель лише пояснює їх і нічого
не рахує. Усі числа рахуються тут (план з БД, мінімальні платежі, MILP-солвер
для сценаріїв «якби», історія вільних грошей за повні місяці) і подаються в
цілих гривнях. У контекст не потрапляють категорії витрат, зняття готівки,
перцентилі чи медіани, прогноз, описи транзакцій, імена отримувачів і номери
карток.

Основа контексту — прийнята версія плану, чинна в поточному місяці: її бюджет
(обов'язкові платежі від стартових залишків розкладу + додатковий платіж), її
розклад на поточний місяць («цього місяця платити»), строк і переплата
версії, речення наслідку з debt_outcome (крім версії від фактичних залишків
на початок місяця без жодного її закритого місяця: тоді прогноз повторює саму
версію). Версія актуальна, доки склад боргів
(structure_fingerprint) збігається; зміна залишків від платежів її не
застарює. Сценарії «якби» для прийнятої версії рахуються від поточних
залишків з бюджетом версії й порівнюються з прогоном від тих самих залишків.
Без прийнятої версії основа — остання чернетка (актуальна за відбитком із
залишками) і речення «Цей план ще не прийнято.». Без актуального плану
контекст — одне речення про те, що план треба перебудувати.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AsyncSessionLocal
from app.models.debt import Debt
from app.models.debt_plan import DebtPlanSchedule, DebtPlanVersion
from app.models.milp_plan import MilpPlan
from app.models.asset import Asset
from app.services import debt_ledger as ledger
from app.services import debt_outcome
from app.services import debt_plan_versions as versions_service
from app.services.affordability import historical_free_cash_flow
from app.services.debt_fingerprint import debts_fingerprint, debts_structure_fingerprint
from app.services.debt_ledger import DebtState, active_debt_states
from app.services.debt_optimizer import simulate_strategy
from app.services.debt_rules import debt_min_payment
from app.services.milp_optimizer import optimize_milp, solve_in_thread

logger = logging.getLogger(__name__)

# Горизонт what-if; той самий, що за замовчуванням у POST /debt/optimize.
WHAT_IF_HORIZON_MONTHS = 36
# Допуск звірки базового прогону what-if зі збереженим планом, грн.
WHAT_IF_PLAN_TOLERANCE_UAH = 1.0
# Скільки останніх повних місяців історії вільних грошей брати.
HISTORY_MONTHS = 12

NO_PLAN_REASON = "плану погашення ще немає — побудуй план на сторінці «Борги»"
STALE_PLAN_REASON = "план застарів: борги змінились, перебудуй план на сторінці «Борги»"
UNVERIFIABLE_PLAN_REASON = (
    "актуальність збереженого плану невідома (немає відбитка боргів) — "
    "перебудуй план на сторінці «Борги»"
)
UNKNOWN_BUDGET_REASON = (
    "збережений план не містить додаткового платежу або відбитка боргів — "
    "перебудуй план на сторінці «Борги»"
)
INCONSISTENT_PLAN_REASON = (
    "розрахунок не збігся зі збереженим планом — перебудуй план на сторінці «Борги»"
)
# Рівні ліквідності активів, які вважаються заощадженнями (доступні одразу).
WHAT_IF_LIQUID_LEVELS = ("high", "medium")

# Частка переплати за евристикою, нижче якої різниця з розрахованим планом
# вважається несуттєвою.
NEGLIGIBLE_SAVINGS_SHARE = 0.01

PLAN_UNAVAILABLE_SENTENCE = (
    "План застарів або відсутній: спочатку перебудуй його на сторінці «Борги» — "
    "вкажи додатковий платіж на місяць і натисни «Побудувати план»."
)
NO_DEBTS_SENTENCE = "Активних боргів зараз немає."
DRAFT_NOT_ACCEPTED_SENTENCE = "Цей план ще не прийнято."
ACCEPTED_PLAN_STALE_SENTENCE = (
    "Склад боргів змінився після прийняття плану: побудуй і прийми новий план на сторінці «Борги»."
)
ACCEPTED_PLAN_NOT_FIXED_SENTENCE = (
    "Новий прийнятий план уже діє, але його розклад ще не зафіксовано: "
    "натисни «Зафіксувати попередній місяць» на сторінці «Борги»."
)
# Розподіл бюджету в контрольному прогоні сценарію «якби» для прийнятої версії.
REFERENCE_STRATEGY = "avalanche"
MONTH_STATUS_HEADER = "Статус закритих місяців від першого прийнятого плану:"
NO_CLOSED_MONTHS_SENTENCE = "Закритих місяців за прийнятим планом ще немає."
MINUS_SIGN = "−"
FACT_SOURCE_PAYMENTS = "внесені платежі"
FACT_SOURCE_NO_PAYMENTS = "позначка «платежів не було»"
FACT_SOURCE_CHECK = "звірка залишку з випискою"
RESERVE_SENTENCE = (
    "Частину грошей варто залишити в резерві на непередбачені витрати, а рішення ухвалюєш ти."
)
HISTORY_NOTE = "це оцінка за твоїми минулими операціями"

# Евристики для порівняння з розрахованим планом: (ключ у plan_data, назва).
COMPARED_STRATEGIES = (
    ("avalanche", "спочатку найдорожчий борг"),
    ("snowball", "спочатку найменший борг"),
)

# Сума в питанні: «2000», «1 500 грн»; не строки («36 міс.», «2 роки») і не відсотки.
_QUESTION_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?!\d|[.,]\d)(?!\s*(?:міс|рок|рік|дн|тиж|%))",
    re.IGNORECASE,
)
# Питання про зміну щомісячного платежу: «а якби я платив ще 2000».
_PAYMENT_QUESTION_RE = re.compile(r"плат", re.IGNORECASE)
_SCENARIO_WORDS_RE = re.compile(
    r"\b(?:ще|додатково|понад|більше|замість|усього|загалом|рівно|якби|якщо)\b", re.IGNORECASE
)
# Слова, з якими сума в питанні — новий додатковий платіж, а не збільшення до поточного.
_TOTAL_WORDS_RE = re.compile(r"\b(?:замість|усього|загалом|рівно)\b", re.IGNORECASE)
# Питання про заощадження чи дострокове погашення.
_SAVINGS_QUESTION_RE = re.compile(
    r"заощадж|відкла|достроков|закрити\s+раніше|одним\s+платежем", re.IGNORECASE
)
# Питання про різницю з попереднім планом.
_PREVIOUS_PLAN_QUESTION_RE = re.compile(
    r"попередн\w*\s+план|стар\w*\s+план|минул\w*\s+план|перш\w*\s+план", re.IGNORECASE
)
# Питання про виконання плану й внесені платежі за минулі місяці.
_PLAN_STATUS_QUESTION_RE = re.compile(
    r"викону|дотриму|вніс|внесл|внесен|сплатив|сплатил|заплатив|заплатил|не\s+платив|пропуст"
    r"|минул\w*\s+місяц|закрит\w*\s+місяц|за\s+(?:січень|лютий|березень|квітень|травень|червень"
    r"|липень|серпень|вересень|жовтень|листопад|грудень)",
    re.IGNORECASE,
)
# Категорії питань для детермінованої відповіді (fallback_sentences контексту).
QUESTION_CATEGORIES = ("what_if", "savings", "previous_plan", "plan_status", "general")


def format_uah(value: float) -> str:
    """Округлює суму до цілих гривень (половина — вгору) і ставить пробіл між тисячами."""
    whole = int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"{whole:,}".replace(",", " ")


def parse_payment_question(question: str | None) -> tuple[float, str] | None:
    """Розбирає питання «а якби я платив ще Z».

    Сума в питанні — збільшення до поточного додаткового платежу (mode "add").
    Зі словами «замість», «усього», «загалом», «рівно» — новий додатковий платіж
    рівно Z (mode "total").

    Returns:
        Пара (Z, mode) або None, якщо питання не про зміну платежу чи суми немає.
    """
    if not question or not _PAYMENT_QUESTION_RE.search(question) or not _SCENARIO_WORDS_RE.search(question):
        return None
    match = _QUESTION_AMOUNT_RE.search(question)
    if not match:
        return None
    value = float(match.group(1).replace(" ", "").replace("\u00a0", ""))
    if value <= 0:
        return None
    return value, "total" if _TOTAL_WORDS_RE.search(question) else "add"


def is_savings_question(question: str | None) -> bool:
    """Перевіряє, чи питання про заощадження або дострокове погашення."""
    return bool(question and _SAVINGS_QUESTION_RE.search(question))


def question_category(question: str | None) -> str:
    """Визначає категорію питання для детермінованої відповіді.

    Returns:
        Одна з QUESTION_CATEGORIES: "what_if" (зміна додаткового платежу),
        "savings", "previous_plan" (різниця з попереднім планом),
        "plan_status" (виконання плану, внесені платежі), інакше "general".
    """
    if parse_payment_question(question) is not None:
        return "what_if"
    if is_savings_question(question):
        return "savings"
    if question and _PREVIOUS_PLAN_QUESTION_RE.search(question):
        return "previous_plan"
    if question and _PLAN_STATUS_QUESTION_RE.search(question):
        return "plan_status"
    return "general"


def _whole(value: float) -> int:
    """Повертає суму в цілих гривнях (як у format_uah) як число."""
    return int(format_uah(value).replace(" ", ""))


def _is_budget_insufficient(milp_data: dict) -> bool:
    """Перевіряє, чи бюджет не покриває MILP-план.

    Бюджет недостатній, якщо солвер не знайшов оптимального рішення або план не
    погашає борги за горизонт. Читає сирий plan_data["milp"]: колонка
    MilpPlan.months_to_payoff ніколи не буває None.
    """
    return milp_data.get("status") != "Optimal" or milp_data.get("months_to_payoff") is None


async def _find_asset_arbitrage(session: AsyncSession, user_id: str, milp_data: dict) -> dict | None:
    """Шукає найвигіднішу пару «актив — борг» для дострокового погашення.

    Працює, лише коли бюджет не покриває MILP-план. Розглядає активи зі
    ставкою, нижчою за річну ставку боргу, і обирає пару з найбільшою річною
    економією на відсотках.

    Returns:
        Словник з назвами активу й боргу, сумою зняття, економією і строком
        доступу до грошей, або None.
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

    debts = await active_debt_states(session, user_id)

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
                }
    return best


def _usable_plan(plan: MilpPlan | None, debts: list[DebtState]) -> tuple[MilpPlan | None, str | None]:
    """Повертає план, якщо він актуальний для поточних боргів, або причину, чому ні.

    План актуальний, якщо його відбиток боргів збігається з відбитком поточних
    активних боргів. Без активних боргів причина не повертається.

    Returns:
        Пара (план, None) або (None, причина).
    """
    if not debts:
        return None, None
    if plan is None:
        return None, NO_PLAN_REASON
    if plan.debts_fingerprint is None:
        return None, UNVERIFIABLE_PLAN_REASON
    if debts_fingerprint(debts) != plan.debts_fingerprint:
        return None, STALE_PLAN_REASON
    return plan, None


def _min_payments_total(debts: list[DebtState]) -> float:
    """Повертає суму обов'язкових платежів активних боргів за правилом debt_rules."""
    return sum(debt_min_payment(d.debt_type, d.balance, d.monthly_rate, d.min_payment) for d in debts)


def _plan_budget(plan: MilpPlan | None, debts: list[DebtState]) -> tuple[float | None, str | None]:
    """Повертає місячний бюджет збереженого плану або причину, чому його немає.

    Бюджет = сума поточних мінімальних платежів активних боргів +
    plan.extra_monthly_uah. Він дійсний, лише якщо відбиток поточних боргів
    збігається з відбитком плану.

    Returns:
        Пара (бюджет, None) або (None, причина).
    """
    if plan is None:
        return None, NO_PLAN_REASON
    if plan.extra_monthly_uah is None or plan.debts_fingerprint is None:
        return None, UNKNOWN_BUDGET_REASON
    if debts_fingerprint(debts) != plan.debts_fingerprint:
        return None, STALE_PLAN_REASON
    return _min_payments_total(debts) + plan.extra_monthly_uah, None


def _debt_dicts(debts: list[DebtState]) -> list[dict]:
    """Перетворює борги з БД на словники для оптимізаторів."""
    return [
        {"name": d.name, "balance": d.balance, "monthly_rate": d.monthly_rate,
         "min_payment": d.min_payment, "debt_type": d.debt_type}
        for d in debts
    ]


@dataclass
class AcceptedBasis:
    """Чинна прийнята версія як основа контексту (суми в гривнях, ключі — назви боргів).

    budget — обов'язкові платежі від стартових залишків розкладу (min_total) +
    додатковий платіж (extra). current_payments — плановий платіж поточного
    місяця, current_openings — залишок на його початок за розкладом.
    not_paid_off — борги із залишком на кінець розкладу, якщо версія не
    погашає всі борги за горизонт. payoff_month — місяць закриття за версією
    або None. comparison_plan_data — plan_data вихідної чернетки для версії від
    залишків на дату прийняття, інакше None.
    """
    version: DebtPlanVersion
    min_total: float
    extra: float
    current_payments: dict[str, float] = field(default_factory=dict)
    current_openings: dict[str, float] = field(default_factory=dict)
    not_paid_off: set[str] = field(default_factory=set)
    payoff_month: date | None = None
    months_left: int | None = None
    total_interest: float | None = None
    comparison_plan_data: dict | None = None

    @property
    def budget(self) -> float:
        """Місячний бюджет версії, грн."""
        return self.min_total + self.extra


async def _accepted_basis(session: AsyncSession, user_id: str, debts: list[DebtState],
                          today: date) -> tuple[AcceptedBasis | None, str | None]:
    """Повертає основу з прийнятої версії, чинної в поточному місяці, або речення, чому її не використано.

    Склад порівнюється за structure_fingerprint: до активних боргів із
    залишком додаються борги розкладу, погашені платежами, тож зміна
    залишків версію не застарює.

    Returns:
        (основа, None); (None, речення) — версія чинна, але без розкладу або
        склад боргів змінився; (None, None) — чинної прийнятої версії немає.
    """
    current = ledger.month_start(today)
    versions = await versions_service.user_versions(session, user_id)
    version = next((v for v in versions if versions_service.version_covers(v, current)), None)
    if version is None:
        return None, None
    if version.materialized_at is None:
        return None, ACCEPTED_PLAN_NOT_FIXED_SENTENCE
    rows = list((await session.execute(
        select(DebtPlanSchedule).where(DebtPlanSchedule.version_id == version.id)
        .order_by(DebtPlanSchedule.month, DebtPlanSchedule.debt_id)
    )).scalars().all())
    in_schedule = {r.debt_id for r in rows}
    live = [d for d in debts if d.balance_kop > 0 or d.id in in_schedule]
    if version.structure_fingerprint is None or debts_structure_fingerprint(live) != version.structure_fingerprint:
        return None, ACCEPTED_PLAN_STALE_SENTENCE

    all_debts = {d.id: d for d in (await session.execute(select(Debt).where(Debt.user_id == user_id))).scalars()}
    names = {debt_id: d.name for debt_id, d in all_debts.items()}
    basis = AcceptedBasis(
        version=version,
        min_total=versions_service.schedule_min_payments_total(rows, all_debts) or 0.0,
        extra=ledger.kop_to_uah(version.extra_monthly_kop),
    )
    for r in rows:
        if r.month == current:
            basis.current_payments[names[r.debt_id]] = ledger.kop_to_uah(r.planned_payment_kop)
            basis.current_openings[names[r.debt_id]] = ledger.kop_to_uah(r.opening_kop)
    if version.months_to_payoff:
        basis.payoff_month = ledger.add_months(version.effective_from, version.months_to_payoff - 1)
        basis.months_left = (basis.payoff_month.year - current.year) * 12 + basis.payoff_month.month - current.month + 1
        basis.total_interest = ledger.kop_to_uah(version.total_interest_kop)
    elif rows:
        last_month = rows[-1].month
        basis.not_paid_off = {names[r.debt_id] for r in rows if r.month == last_month and r.closing_kop > 0}
    if version.start_kind == "acceptance_balances":
        source = await session.get(MilpPlan, version.source_plan_id)
        basis.comparison_plan_data = source.plan_data if source is not None else None
    return basis, None


async def _savings_total(session: AsyncSession, user_id: str) -> float:
    """Повертає суму активних активів з ліквідністю WHAT_IF_LIQUID_LEVELS (заощадження)."""
    result = await session.execute(
        select(Asset).where(
            Asset.user_id == user_id,
            Asset.is_active == True,
            Asset.liquidity_level.in_(WHAT_IF_LIQUID_LEVELS),
        )
    )
    return sum(a.current_amount for a in result.scalars().all())


async def _estimate_debt_reduction_from_assets(
    session: AsyncSession, user_id: str, plan: MilpPlan | None, basis: AcceptedBasis | None = None,
) -> tuple[dict | None, str | None]:
    """Оцінює сценарій одноразового погашення боргів заощадженнями.

    Сума активів з ліквідністю high/medium застосовується до боргів у порядку
    спадання ставки. MILP-солвер запускається двічі в окремому потоці
    (solve_in_thread) — на поточних балансах і на зменшених — з бюджетом
    прийнятої версії (basis) або збереженої чернетки (_plan_budget); результат —
    різниця; без розв'язку "Optimal" оцінки немає. Базовий прогін звіряється за
    сумарною переплатою з допуском WHAT_IF_PLAN_TOLERANCE_UAH: для прийнятої
    версії — з прогоном REFERENCE_STRATEGY від тих самих залишків і з тим самим
    бюджетом, для чернетки — зі збереженою переплатою плану. Якщо звірка не
    проходить, оцінка не повертається, а розбіжність логується.

    Returns:
        Пара (оцінка, причина). Оцінка — словник з бюджетом, сумою застосованих
        активів, скороченням строку погашення, новим строком і економією на
        відсотках. Причина — текст, чому оцінки немає; (None, None), якщо немає
        активних боргів або заощаджень.
    """
    total_applicable = await _savings_total(session, user_id)
    if total_applicable <= 0:
        return None, None

    debts = await active_debt_states(session, user_id)
    if not debts:
        return None, None

    if basis is not None:
        budget = basis.budget
    else:
        budget, reason = _plan_budget(plan, debts)
        if reason is not None:
            return None, reason

    baseline_debts = _debt_dicts(debts)
    adjusted_debts = _debt_dicts(debts)

    # Сума розподіляється від боргу з найвищою ставкою, доки не вичерпається.
    remaining = total_applicable
    for d in sorted(adjusted_debts, key=lambda d: d["monthly_rate"], reverse=True):
        if remaining <= 0:
            break
        pay = min(remaining, d["balance"])
        d["balance"] -= pay
        remaining -= pay

    try:
        baseline = await solve_in_thread(optimize_milp, baseline_debts, budget, WHAT_IF_HORIZON_MONTHS)
        adjusted = await solve_in_thread(optimize_milp, adjusted_debts, budget, WHAT_IF_HORIZON_MONTHS)
    except Exception:
        logger.exception("what-if за заощадженнями: MILP-солвер впав або не вклався в ліміт часу (user_id=%s)", user_id)
        return None, None
    if baseline["status"] != "Optimal" or adjusted["status"] != "Optimal":
        return None, None

    if basis is not None:
        reference = simulate_strategy(_debt_dicts(debts), budget, WHAT_IF_HORIZON_MONTHS,
                                      REFERENCE_STRATEGY)["total_interest_paid"]
        reference_id = f"version_id={basis.version.id}"
    else:
        reference = plan.total_interest_paid
        reference_id = f"plan_id={plan.id}"
    mismatch = abs(baseline["total_interest_paid"] - reference)
    if mismatch > WHAT_IF_PLAN_TOLERANCE_UAH:
        logger.warning(
            "what-if за заощадженнями не показано: базовий прогін %.2f грн проти контрольного %.2f грн "
            "(різниця %.2f > %.2f), бюджет %.2f, %s",
            baseline["total_interest_paid"], reference, mismatch,
            WHAT_IF_PLAN_TOLERANCE_UAH, budget, reference_id,
        )
        return None, INCONSISTENT_PLAN_REASON

    months_saved = None
    if baseline["months_to_payoff"] is not None and adjusted["months_to_payoff"] is not None:
        months_saved = baseline["months_to_payoff"] - adjusted["months_to_payoff"]

    return {
        "monthly_budget": budget,
        "assets_applied_amount": total_applicable,
        "debt_payoff_months_saved": months_saved,
        "adjusted_months_to_payoff": adjusted["months_to_payoff"],
        "interest_saved": baseline["total_interest_paid"] - adjusted["total_interest_paid"],
    }, None


# --- Речення контексту -----------------------------------------------------

def _plan_sentences(plan: MilpPlan, debts: list[DebtState], min_total: float) -> list[str]:
    """Речення про бюджет на борги, кожен борг і підсумок розрахованого плану."""
    milp = plan.plan_data.get("milp", {})
    mandatory = int(format_uah(min_total).replace(" ", ""))
    extra = int(format_uah(plan.extra_monthly_uah).replace(" ", ""))
    sentences = [
        f"Щомісяця на борги йде {format_uah(mandatory + extra)} грн: {format_uah(mandatory)} грн "
        f"обов'язкових платежів і {format_uah(extra)} грн понад них."
    ]

    months = milp.get("months_to_payoff")
    horizon = plan.months_to_payoff  # для плану без погашення колонка зберігає горизонт
    first_month = (milp.get("monthly_log") or [{}])[0].get("payments", {})
    final_balances = milp.get("final_balances", {})
    for debt in sorted(debts, key=lambda d: d.name):
        line = f"{debt.name}: залишилось {format_uah(debt.balance)} грн"
        if debt.name in first_month:
            line += f", цього місяця платити {format_uah(first_month[debt.name])} грн"
        if months is None and final_balances.get(debt.name, 0) > 0.01:
            line += f"; за {horizon} міс. цей борг не погашається, переплату за ним не називай"
        sentences.append(line + ".")

    focus = _focus_debt_sentence(debts, first_month)
    if focus:
        sentences.append(focus)

    if months is not None:
        sentences.append(
            f"За планом усі борги закриються через {months} міс., "
            f"загальна переплата {format_uah(plan.total_interest_paid)} грн."
        )
    else:
        sentences.append(
            f"За планом не всі борги закриються за {horizon} міс., тому загальну переплату не називай."
        )
    return sentences


def _focus_debt_sentence(debts: list[DebtState], first_month: dict, openings: dict | None = None) -> str | None:
    """Речення про борг, на який цього місяця йде найбільше понад його обов'язковий платіж.

    Обов'язковий платіж рахується від залишку боргу на початок місяця за
    розкладом (openings, за назвою), а без нього — від поточного залишку. Якщо
    в цього боргу найвища ставка серед активних боргів, речення пояснює вибір
    цим. Без такої частини (план платить лише обов'язкові) речення немає.
    """
    best, best_over = None, 0.0
    for debt in debts:
        if debt.name not in first_month:
            continue
        balance = (openings or {}).get(debt.name, debt.balance)
        over = first_month[debt.name] - debt_min_payment(
            debt.debt_type, balance, debt.monthly_rate, debt.min_payment
        )
        if over > best_over:
            best, best_over = debt, over
    if best is None or format_uah(best_over) == "0":
        return None
    sentence = (f"Цього місяця понад обов'язкові платежі найбільше йде на «{best.name}»: "
                f"{format_uah(best_over)} грн понад його обов'язковий платіж")
    top_rate = max(d.monthly_rate for d in debts)
    if best.monthly_rate == top_rate:
        unique = sum(1 for d in debts if d.monthly_rate == top_rate) == 1
        sentence += (", бо це борг з найвищою ставкою серед твоїх боргів" if unique
                     else ", бо це один з боргів з найвищою ставкою")
    return sentence + "."


def _accepted_plan_sentences(basis: AcceptedBasis, debts: list[DebtState]) -> list[str]:
    """Речення про бюджет, кожен борг і підсумок прийнятої версії."""
    mandatory, extra = _whole(basis.min_total), _whole(basis.extra)
    sentences = [
        f"Щомісяця на борги йде {format_uah(mandatory + extra)} грн: {format_uah(mandatory)} грн "
        f"обов'язкових платежів і {format_uah(extra)} грн понад них."
    ]
    horizon = versions_service.PLAN_HORIZON_MONTHS
    for debt in sorted(debts, key=lambda d: d.name):
        line = f"{debt.name}: залишилось {format_uah(debt.balance)} грн"
        if debt.name in basis.current_payments:
            line += f", цього місяця платити {format_uah(basis.current_payments[debt.name])} грн"
        if debt.name in basis.not_paid_off:
            line += f"; за {horizon} міс. цей борг не погашається, переплату за ним не називай"
        sentences.append(line + ".")

    focus = _focus_debt_sentence(debts, basis.current_payments, basis.current_openings)
    if focus:
        sentences.append(focus)

    if basis.payoff_month is not None:
        line = f"За прийнятим планом усі борги закриються {debt_outcome.month_in_text(basis.payoff_month)}"
        if basis.months_left and basis.months_left > 0:
            line += f" (через {basis.months_left} міс. від цього місяця)"
        sentences.append(line + f", загальна переплата за планом {format_uah(basis.total_interest)} грн.")
    else:
        sentences.append(
            f"За прийнятим планом не всі борги закриються за {horizon} міс., тому загальну переплату не називай."
        )
    return sentences


def _comparison_sentences(plan_data: dict) -> list[str]:
    """Речення порівняння розрахованого плану з евристиками.

    Порівнюються лише стратегії, що погашають усі борги за горизонт, і лише
    коли сам план їх погашає. Різниця менше NEGLIGIBLE_SAVINGS_SHARE від
    переплати стратегії — одне речення «та сама переплата».
    """
    milp = plan_data.get("milp", {})
    if milp.get("months_to_payoff") is None or milp.get("total_interest_paid") is None:
        return []
    plan_interest = milp["total_interest_paid"]

    sentences = []
    for key, label in COMPARED_STRATEGIES:
        strategy = plan_data.get(key, {})
        if strategy.get("months_to_payoff") is None or strategy.get("total_interest_paid") is None:
            continue
        interest = strategy["total_interest_paid"]
        diff = interest - plan_interest
        if interest <= 0 or diff < NEGLIGIBLE_SAVINGS_SHARE * interest or format_uah(diff) == "0":
            sentences.append(f"{label[0].upper()}{label[1:]} дає ту саму переплату, окремо порівнювати не треба.")
        else:
            sentences.append(
                f"Якщо гасити {label}, переплата буде на {format_uah(diff)} грн більша, ніж за розрахованим планом."
            )

    minimum = plan_data.get("minimum", {})
    if minimum.get("months_to_payoff") is not None and minimum.get("total_interest_paid") is not None:
        diff = minimum["total_interest_paid"] - plan_interest
        if format_uah(diff) != "0" and diff > 0:
            sentences.append(
                f"Якщо платити тільки обов'язкові платежі, переплата буде на {format_uah(diff)} грн більша, "
                "ніж за розрахованим планом."
            )
    return sentences


async def _availability_sentence(session: AsyncSession, user_id: str, extra: float) -> str | None:
    """Речення про те, у скількох останніх повних місяцях вільних грошей вистачало б на extra.

    k рахується саме для суми extra; без суми (extra <= 0) або без достатньої
    історії речення немає.
    """
    if extra <= 0:
        return None
    history = await historical_free_cash_flow(session, user_id, extra, HISTORY_MONTHS)
    if history["count_ge_extra"] is None:
        return None
    k, n = history["count_ge_extra"], history["n_months_used"]
    return (
        f"У {k} з {n} останніх місяців вільних грошей вистачало б на додатковий платіж "
        f"{format_uah(extra)} грн; {HISTORY_NOTE}."
    )


def _base_sentence(plan: MilpPlan) -> str:
    """Речення про поточний план: додатковий платіж, строк і переплата."""
    milp = plan.plan_data.get("milp", {})
    line = f"Зараз додатковий платіж {format_uah(plan.extra_monthly_uah)} грн: "
    if milp.get("months_to_payoff") is not None:
        return line + (f"борги закриються через {milp['months_to_payoff']} міс., "
                       f"переплата {format_uah(plan.total_interest_paid)} грн.")
    return line + f"за {plan.months_to_payoff} міс. не всі борги закриються."


async def _extra_question_sentences(
    session: AsyncSession, user_id: str, debts: list[DebtState], min_total: float, current_extra: float,
    amount: float, mode: str, plan: MilpPlan | None = None,
) -> list[str]:
    """Речення сценарію зміни додаткового платежу з базою і різницею.

    mode "add": новий додатковий платіж = поточний + amount; mode "total":
    новий додатковий платіж = amount. Бюджет = обов'язкові платежі (min_total) +
    новий додатковий платіж; строк і переплату рахує MILP-солвер в окремому
    потоці; без розв'язку "Optimal" сценарій позначається як нерозрахований.
    База — збережена чернетка (plan), а для прийнятої версії (plan=None) —
    прогін від поточних залишків з поточним бюджетом, і різниця рахується від
    нього. Доступність рахується для нового додаткового платежу.
    """
    new_extra = current_extra + amount if mode == "add" else amount
    if mode == "add":
        line = (f"Якби до цього додати ще {format_uah(amount)} грн "
                f"(разом {format_uah(_whole(current_extra) + _whole(amount))} грн понад обов'язкові), ")
    else:
        line = f"Якби додатковий платіж був {format_uah(amount)} грн замість {format_uah(current_extra)} грн, "

    async def _solve(budget: float) -> dict | None:
        try:
            return await solve_in_thread(optimize_milp, _debt_dicts(debts), budget, WHAT_IF_HORIZON_MONTHS)
        except Exception:
            logger.exception("сценарій додаткового платежу: MILP-солвер впав або не вклався в ліміт часу")
            return None

    if plan is not None:
        base_sentence = _base_sentence(plan)
        base_interest = plan.total_interest_paid
        base_paid = plan.plan_data.get("milp", {}).get("months_to_payoff") is not None
    else:
        base = await _solve(min_total + current_extra)
        base_paid = bool(base and base["status"] == "Optimal" and base["months_to_payoff"] is not None)
        base_interest = base["total_interest_paid"] if base_paid else None
        base_sentence = f"Зараз додатковий платіж {format_uah(current_extra)} грн: "
        if base_paid:
            base_sentence += (f"від нинішніх залишків борги закриються через {base['months_to_payoff']} міс., "
                              f"переплата від цього місяця {format_uah(base_interest)} грн.")
        elif base and base["status"] == "Optimal":
            base_sentence += f"за {WHAT_IF_HORIZON_MONTHS} міс. не всі борги закриються."
        else:
            base_sentence = f"Зараз додатковий платіж {format_uah(current_extra)} грн."

    result = await _solve(min_total + new_extra)
    if not result or result["status"] != "Optimal":
        line += "розрахувати цей варіант не вдалося."
    elif result["months_to_payoff"] is None:
        line += f"за {WHAT_IF_HORIZON_MONTHS} міс. не всі борги закрилися б."
    else:
        line += (f"борги закрилися б через {result['months_to_payoff']} міс., "
                 f"переплата {format_uah(result['total_interest_paid'])} грн")
        if base_paid:
            diff = _whole(base_interest) - _whole(result["total_interest_paid"])
            if diff > 0:
                line += f", тобто на {format_uah(diff)} грн менше"
            elif diff < 0:
                line += f", тобто на {format_uah(-diff)} грн більше"
            else:
                line += ", тобто така сама"
        line += "."

    sentences = [base_sentence, line]
    if result and result["status"] == "Optimal":
        availability = await _availability_sentence(session, user_id, new_extra)
        if availability:
            sentences.append(availability)
    sentences.append(RESERVE_SENTENCE)
    return sentences


def _what_if_sentences(what_if: dict) -> list[str]:
    """Речення сценарію «якби спрямувати заощадження на борги»."""
    line = f"Якби одноразово спрямувати {format_uah(what_if['assets_applied_amount'])} грн заощаджень на борги сьогодні, "
    adjusted = what_if["adjusted_months_to_payoff"]
    if adjusted is None:
        line += f"за {WHAT_IF_HORIZON_MONTHS} міс. борги все одно не закрилися б"
    else:
        line += f"борги закрилися б через {adjusted} міс."
        if what_if["debt_payoff_months_saved"]:
            line += f" (на {what_if['debt_payoff_months_saved']} міс. раніше)"
    if adjusted is not None and format_uah(what_if["interest_saved"]) != "0":
        line += f", а переплата зменшилась би на {format_uah(what_if['interest_saved'])} грн"
    return [line.rstrip(".") + ".", "Це лише сценарій «якби», а не порада.", RESERVE_SENTENCE]


def _arbitrage_sentence(arbitrage: dict) -> str:
    """Речення про заощадження зі ставкою, нижчою за ставку боргу."""
    sentence = (
        f"Заощадження «{arbitrage['asset_name']}» приносять менше, ніж коштує борг «{arbitrage['debt_name']}»: "
        f"якби спрямувати {format_uah(arbitrage['withdrawable'])} грн із них на цей борг, це зекономило б "
        f"приблизно {format_uah(arbitrage['savings'])} грн на рік на відсотках."
    )
    if arbitrage["liquidity_days"] is not None:
        sentence += f" Гроші з них доступні через {arbitrage['liquidity_days']} дн."
    return sentence


async def build_advisor_context(
    user_id: str, session: AsyncSession | None = None, question: str | None = None
) -> dict:
    """Збирає контекст користувача для AI-радника як готові речення.

    Args:
        user_id: Id користувача.
        session: Сесія БД; якщо не задано, відкривається власна.
        question: Питання користувача. Для питання «а якби я платив ще Z»
            додаються речення сценарію зміни додаткового платежу; для питання
            про заощадження чи дострокове погашення — сценарій погашення із
            заощаджень.

    Returns:
        Словник: sentences — список речень українською (усі суми в цілих
        гривнях); plan_available — чи використано актуальний план;
        plan_kind — "accepted" (чинна прийнята версія), "draft" (остання
        чернетка) або None;
        asset_arbitrage — чи є заощадження зі ставкою, нижчою за ставку боргу
        (речення про них додається лише для питання про заощадження);
        savings_scenario — чи є в реченнях сценарій використання заощаджень;
        fallback_sentences — речення контексту за категоріями
        QUESTION_CATEGORIES (порожній список — даних для категорії немає),
        якими відповідає радник, коли модель повторно порушила правила.
    """
    if session is not None:
        return await _build_context(session, user_id, question)
    async with AsyncSessionLocal() as own_session:
        return await _build_context(own_session, user_id, question)


async def _build_context(session: AsyncSession, user_id: str, question: str | None) -> dict:
    """Складає речення контексту радника в межах переданої сесії."""
    today = ledger.today_kyiv()
    debts = await active_debt_states(session, user_id, today)

    savings = await _savings_total(session, user_id)
    savings_sentence = f"Заощадження: {format_uah(savings)} грн." if savings > 0 else None
    unavailable = {"plan_available": False, "plan_kind": None, "asset_arbitrage": False, "savings_scenario": False}

    if not debts:
        sentences = [NO_DEBTS_SENTENCE] + ([savings_sentence] if savings_sentence else [])
        return {"sentences": sentences, **unavailable, "fallback_sentences": _fallback(general=sentences)}

    basis, accepted_note = await _accepted_basis(session, user_id, debts, today)
    if accepted_note is not None:
        sentences = [accepted_note]
        status: list[str] = []
        if accepted_note == ACCEPTED_PLAN_NOT_FIXED_SENTENCE:
            sentences += await _outcome_sentences(session, user_id, today)
            status = await _month_status_sentences(session, user_id, today)
            sentences += status
        return {"sentences": sentences, **unavailable,
                "fallback_sentences": _fallback(general=[accepted_note], plan_status=status)}

    plan = None
    status, previous_sentences = [], []
    if basis is not None:
        sentences = _accepted_plan_sentences(basis, debts)
        general = list(sentences)
        sentences += await _outcome_sentences(session, user_id, today,
                                              forecast=_outcome_informative(basis.version, today))
        status = await _month_status_sentences(session, user_id, today)
        sentences += status
        previous = _previous_version_sentence(await versions_service.user_versions(session, user_id), basis.version)
        if previous:
            sentences.append(previous)
            previous_sentences = [previous]
        sentences.append(f"Разом боргів зараз {format_uah(sum(d.balance for d in debts))} грн.")
        if basis.comparison_plan_data:
            sentences += _comparison_sentences(basis.comparison_plan_data)
        min_total, extra = basis.min_total, basis.extra
        milp_data = {"status": "Optimal", "months_to_payoff": basis.version.months_to_payoff}
        plan_kind = "accepted"
    else:
        latest = (await session.execute(
            select(MilpPlan)
            .where(MilpPlan.user_id == user_id)
            .order_by(MilpPlan.generated_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        plan, _ = _usable_plan(latest, debts)
        if plan is None or plan.extra_monthly_uah is None or not plan.plan_data:
            return {"sentences": [PLAN_UNAVAILABLE_SENTENCE], **unavailable,
                    "fallback_sentences": _fallback(general=[PLAN_UNAVAILABLE_SENTENCE])}
        min_total, extra = _min_payments_total(debts), plan.extra_monthly_uah
        sentences = [DRAFT_NOT_ACCEPTED_SENTENCE] + _plan_sentences(plan, debts, min_total)
        general = list(sentences)
        sentences.append(f"Разом боргів зараз {format_uah(sum(d.balance for d in debts))} грн.")
        sentences += _comparison_sentences(plan.plan_data)
        milp_data = plan.plan_data.get("milp", {})
        plan_kind = "draft"

    availability = await _availability_sentence(session, user_id, extra)
    if availability:
        sentences.append(availability)

    if savings_sentence:
        sentences.append(savings_sentence)
    arbitrage = await _find_asset_arbitrage(session, user_id, milp_data)
    savings_scenario = False
    savings_sentences = [savings_sentence] if savings_sentence else []
    if is_savings_question(question):
        what_if, _ = await _estimate_debt_reduction_from_assets(session, user_id, plan, basis)
        if what_if:
            savings_sentences += _what_if_sentences(what_if)
            savings_scenario = True
        if arbitrage:
            savings_sentences.append(_arbitrage_sentence(arbitrage))
            savings_scenario = True
        sentences += savings_sentences[1 if savings_sentence else 0:]

    what_if_sentences: list[str] = []
    payment_question = parse_payment_question(question)
    if payment_question is not None:
        amount, mode = payment_question
        what_if_sentences = await _extra_question_sentences(
            session, user_id, debts, min_total, extra, amount, mode, plan,
        )
        sentences += what_if_sentences

    # Однакові речення (нагадування про резерв) лишаються один раз.
    sentences = list(dict.fromkeys(sentences))
    fallback = _fallback(general=general, plan_status=status, previous_plan=previous_sentences,
                         what_if=what_if_sentences, savings=savings_sentences)
    return {"sentences": sentences, "plan_available": True, "plan_kind": plan_kind,
            "asset_arbitrage": arbitrage is not None, "savings_scenario": savings_scenario,
            "fallback_sentences": fallback}


def _fallback(**by_category: list[str]) -> dict[str, list[str]]:
    """Речення за всіма QUESTION_CATEGORIES без повторів; категорія без речень — порожній список."""
    return {c: list(dict.fromkeys(by_category.get(c) or [])) for c in QUESTION_CATEGORIES}


def _outcome_informative(version: DebtPlanVersion, today: date) -> bool:
    """Чи речення наслідку несе інформацію для чинної версії.

    Версія від фактичних залишків на початок місяця (month_start_actual) без
    жодного свого закритого місяця побудована від тих самих залишків, що й
    прогноз наслідку, тож прогноз повторює версію.
    """
    return version.start_kind != "month_start_actual" or version.effective_from < ledger.month_start(today)


def _signed_uah(whole: int) -> str:
    """Ціла сума зі знаком мінус для від'ємної: «−9 000»."""
    return (MINUS_SIGN if whole < 0 else "") + format_uah(abs(whole))


def payment_diff_text(diff: int) -> str:
    """Різниця платежу (факт − план, цілі гривні) словами, як у блоці «План і факт»."""
    if diff == 0:
        return "внесено стільки, скільки в плані"
    if diff < 0:
        return f"внесено на {format_uah(-diff)} грн менше за план"
    return f"внесено на {format_uah(diff)} грн більше за план"


async def _month_status_sentences(session: AsyncSession, user_id: str, today: date) -> list[str]:
    """Рядки про кожен закритий місяць від першої прийнятої версії (через усі версії) і підсумок.

    Для місяця: план, внесено, різниця (факт − план) і джерело факту —
    внесені платежі, позначка «платежів не було» (внесено 0 грн) або звірка з
    випискою. Місяць без внесених платежів і без позначки внесеної суми не має
    і в підсумок не входить. Порожньо, якщо прийнятого плану немає.
    """
    versions = await versions_service.user_versions(session, user_id)
    first = versions_service.first_version(versions)
    if first is None:
        return []
    last_closed = ledger.add_months(ledger.month_start(today), -1)
    if last_closed < first.effective_from:
        return [NO_CLOSED_MONTHS_SENTENCE]
    data = await versions_service.plan_fact(session, user_id, first.effective_from, last_closed, today)

    lines = [MONTH_STATUS_HEADER]
    planned_sum = actual_sum = 0
    counted, skipped = 0, []
    for month in data["months"]:
        name = ledger.month_name(month["month"])
        totals = month["totals"]
        planned = totals["planned_payment_kop"]
        has_check = any(row["check"] is not None for row in month["debts"])
        state = month["entry_state"]
        label = name[0].upper() + name[1:]
        if month["version_no"] is not None:
            label += f" (план №{month['version_no']})"
        parts = [f"за планом {format_uah(ledger.kop_to_uah(planned))} грн" if planned is not None
                 else "плану на цей місяць немає"]
        if state == versions_service.NOT_ENTERED:
            parts.append("платежів не внесено, внесена сума невідома")
            source = FACT_SOURCE_CHECK if has_check else None
            skipped.append(name)
        else:
            actual = totals["actual_payment_kop"] or 0
            parts.append(f"внесено {format_uah(ledger.kop_to_uah(actual))} грн")
            if planned is not None:
                diff = debt_outcome.whole_uah(actual - planned)
                parts.append(f"різниця {_signed_uah(diff)} грн ({payment_diff_text(diff)})")
                planned_sum += planned
                actual_sum += actual
                counted += 1
            source = FACT_SOURCE_PAYMENTS if state == versions_service.ENTERED else FACT_SOURCE_NO_PAYMENTS
            if has_check:
                source += f" і {FACT_SOURCE_CHECK}"
        line = f"{label}: " + ", ".join(parts)
        if source:
            line += f"; джерело факту — {source}"
        lines.append(line + ".")

    if counted:
        diff = debt_outcome.whole_uah(actual_sum - planned_sum)
        total = (f"Разом за закриті місяці: за планом {format_uah(ledger.kop_to_uah(planned_sum))} грн, "
                 f"внесено {format_uah(ledger.kop_to_uah(actual_sum))} грн, "
                 f"різниця {_signed_uah(diff)} грн ({payment_diff_text(diff)})")
        if skipped:
            total += f"; без місяців, де платежів не внесено ({', '.join(skipped)})"
        lines.append(total + ".")
    return lines


def _start_date_text(v: DebtPlanVersion) -> str:
    """Дата стартових залишків розрахунку версії: «5 листопада 2026 р.»."""
    start = v.accepted_on if v.start_kind == "acceptance_balances" else v.effective_from
    return f"{ledger.date_text(start)} р."


def _version_delta_text(prev: DebtPlanVersion, prev_payoff: date, current: DebtPlanVersion,
                        current_payoff: date) -> str:
    """Різниця закриття (міс.) і переплати (цілі грн) чинної версії проти попередньої словами."""
    shift = (current_payoff.year - prev_payoff.year) * 12 + current_payoff.month - prev_payoff.month
    if shift == 0:
        payoff = "закриття в тому самому місяці"
    else:
        payoff = f"закриття на {abs(shift)} міс. {'пізніше' if shift > 0 else 'раніше'}"
    diff = debt_outcome.whole_uah(current.total_interest_kop) - debt_outcome.whole_uah(prev.total_interest_kop)
    if diff == 0:
        interest = "переплата та сама"
    else:
        interest = f"переплата на {format_uah(abs(diff))} грн {'більша' if diff > 0 else 'менша'}"
    return f"у чинному {payoff}, {interest}"


def _previous_version_sentence(versions: list[DebtPlanVersion], current: DebtPlanVersion) -> str | None:
    """Речення порівняння чинної версії з попередньою, що діяла; None — немає попередньої чи розрахунку.

    Містить дати стартових залишків обох розрахунків (дата прийняття для
    версії від залишків на дату прийняття, перше число місяця дії для версії
    від фактичних залишків), закриття й переплату кожної та їх різницю.
    """
    previous = [v for v in versions
                if v.id != current.id and versions_service.version_acted(v) and v.effective_from < current.effective_from]
    if not previous:
        return None
    prev = max(previous, key=lambda v: v.effective_from)

    def payoff(v: DebtPlanVersion) -> date | None:
        if v.materialized_at is None or not v.months_to_payoff or v.total_interest_kop is None:
            return None
        return ledger.add_months(v.effective_from, v.months_to_payoff - 1)

    def summary(v: DebtPlanVersion, month: date) -> str:
        return (f"закриття — {ledger.month_name(month)}, "
                f"переплата {format_uah(ledger.kop_to_uah(v.total_interest_kop))} грн")

    prev_payoff, current_payoff = payoff(prev), payoff(current)
    if prev_payoff is None or current_payoff is None:
        return None
    return (f"Попередній план (діяв з {ledger.date_text(prev.effective_from)}) рахувався від залишків на "
            f"{_start_date_text(prev)}: {summary(prev, prev_payoff)}; "
            f"чинний рахується від залишків на {_start_date_text(current)}: {summary(current, current_payoff)}; "
            f"{_version_delta_text(prev, prev_payoff, current, current_payoff)}.")


async def _outcome_sentences(session: AsyncSession, user_id: str, today: date, forecast: bool = True) -> list[str]:
    """Речення наслідку внесених платежів (debt_outcome) як готовий рядок; порожньо, якщо його немає.

    Args:
        forecast: False — речення прогнозу («За внесеними платежами …») не
            додається; речення про місяці без внесених платежів лишається.
    """
    try:
        outcome = await debt_outcome.plan_outcome(session, user_id, today)
    except versions_service.PlanFactError:
        return []
    if not forecast and outcome["status"] != debt_outcome.STATUS_UNRESOLVED:
        return []
    return [outcome["sentence"]] if outcome.get("sentence") else []
