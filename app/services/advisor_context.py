"""
Контекст для AI-радника, зібраний з БД на боці сервера.

Контекст — список готових речень українською: модель лише пояснює їх і нічого
не рахує. Усі числа рахуються тут (план з БД, мінімальні платежі, MILP-солвер
для сценаріїв «якби», історія вільних грошей за повні місяці) і подаються в
цілих гривнях. У контекст не потрапляють категорії витрат, зняття готівки,
перцентилі чи медіани, прогноз, описи транзакцій, імена отримувачів і номери
карток. Без актуального плану контекст — одне речення про те, що план треба
перебудувати.
"""
import logging
import re
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AsyncSessionLocal
from app.models.milp_plan import MilpPlan
from app.models.debt import Debt
from app.models.asset import Asset
from app.services.affordability import historical_free_cash_flow
from app.services.debt_fingerprint import debts_fingerprint
from app.services.debt_rules import debt_min_payment
from app.services.milp_optimizer import optimize_milp

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

    debts_result = await session.execute(
        select(Debt).where(Debt.user_id == user_id, Debt.is_active == True)
    )
    debts = debts_result.scalars().all()

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


def _usable_plan(plan: MilpPlan | None, debts: list[Debt]) -> tuple[MilpPlan | None, str | None]:
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


def _min_payments_total(debts: list[Debt]) -> float:
    """Повертає суму обов'язкових платежів активних боргів за правилом debt_rules."""
    return sum(debt_min_payment(d.debt_type, d.balance, d.monthly_rate, d.min_payment) for d in debts)


def _plan_budget(plan: MilpPlan | None, debts: list[Debt]) -> tuple[float | None, str | None]:
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


def _debt_dicts(debts: list[Debt]) -> list[dict]:
    """Перетворює борги з БД на словники для оптимізаторів."""
    return [
        {"name": d.name, "balance": d.balance, "monthly_rate": d.monthly_rate,
         "min_payment": d.min_payment, "debt_type": d.debt_type}
        for d in debts
    ]


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
    session: AsyncSession, user_id: str, plan: MilpPlan | None
) -> tuple[dict | None, str | None]:
    """Оцінює сценарій одноразового погашення боргів заощадженнями.

    Сума активів з ліквідністю high/medium застосовується до боргів у порядку
    спадання ставки. MILP-солвер запускається двічі — на поточних балансах і на
    зменшених — з бюджетом збереженого плану (_plan_budget); результат —
    різниця. Базовий прогін має збігатися зі збереженим планом за сумарною
    переплатою з допуском WHAT_IF_PLAN_TOLERANCE_UAH; інакше оцінка не
    повертається, а розбіжність логується.

    Returns:
        Пара (оцінка, причина). Оцінка — словник з бюджетом, сумою застосованих
        активів, скороченням строку погашення, новим строком і економією на
        відсотках. Причина — текст, чому оцінки немає; (None, None), якщо немає
        активних боргів або заощаджень.
    """
    total_applicable = await _savings_total(session, user_id)
    if total_applicable <= 0:
        return None, None

    debts_result = await session.execute(
        select(Debt).where(Debt.user_id == user_id, Debt.is_active == True)
    )
    debts = debts_result.scalars().all()
    if not debts:
        return None, None

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
        baseline = optimize_milp(baseline_debts, budget, WHAT_IF_HORIZON_MONTHS)
        adjusted = optimize_milp(adjusted_debts, budget, WHAT_IF_HORIZON_MONTHS)
    except Exception:
        logger.exception("what-if за заощадженнями: MILP-солвер впав (user_id=%s)", user_id)
        return None, None
    if baseline["status"] != "Optimal" or adjusted["status"] != "Optimal":
        return None, None

    mismatch = abs(baseline["total_interest_paid"] - plan.total_interest_paid)
    if mismatch > WHAT_IF_PLAN_TOLERANCE_UAH:
        logger.warning(
            "what-if за заощадженнями не показано: базовий прогін %.2f грн проти плану %.2f грн "
            "(різниця %.2f > %.2f), бюджет %.2f, plan_id=%s",
            baseline["total_interest_paid"], plan.total_interest_paid, mismatch,
            WHAT_IF_PLAN_TOLERANCE_UAH, budget, plan.id,
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

def _plan_sentences(plan: MilpPlan, debts: list[Debt], min_total: float) -> list[str]:
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


def _focus_debt_sentence(debts: list[Debt], first_month: dict) -> str | None:
    """Речення про борг, на який цього місяця йде найбільше понад його обов'язковий платіж.

    Якщо в цього боргу найвища ставка серед активних боргів, речення пояснює
    вибір цим. Без такої частини (план платить лише обов'язкові) речення немає.
    """
    best, best_over = None, 0.0
    for debt in debts:
        if debt.name not in first_month:
            continue
        over = first_month[debt.name] - debt_min_payment(
            debt.debt_type, debt.balance, debt.monthly_rate, debt.min_payment
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
    session: AsyncSession, user_id: str, plan: MilpPlan, debts: list[Debt], min_total: float,
    amount: float, mode: str,
) -> list[str]:
    """Речення сценарію зміни додаткового платежу з базою і різницею.

    mode "add": новий додатковий платіж = поточний + amount; mode "total":
    новий додатковий платіж = amount. Бюджет = обов'язкові платежі + новий
    додатковий платіж; строк і переплату рахує MILP-солвер. Доступність
    рахується для нового додаткового платежу.
    """
    current = plan.extra_monthly_uah
    new_extra = current + amount if mode == "add" else amount
    if mode == "add":
        line = (f"Якби до цього додати ще {format_uah(amount)} грн "
                f"(разом {format_uah(_whole(current) + _whole(amount))} грн понад обов'язкові), ")
    else:
        line = f"Якби додатковий платіж був {format_uah(amount)} грн замість {format_uah(current)} грн, "

    try:
        result = optimize_milp(_debt_dicts(debts), min_total + new_extra, WHAT_IF_HORIZON_MONTHS)
    except Exception:
        logger.exception("сценарій додаткового платежу: MILP-солвер впав")
        result = None
    base_paid = plan.plan_data.get("milp", {}).get("months_to_payoff") is not None
    if not result or result["status"] != "Optimal":
        line += "розрахувати цей варіант не вдалося."
    elif result["months_to_payoff"] is None:
        line += f"за {WHAT_IF_HORIZON_MONTHS} міс. не всі борги закрилися б."
    else:
        line += (f"борги закрилися б через {result['months_to_payoff']} міс., "
                 f"переплата {format_uah(result['total_interest_paid'])} грн")
        if base_paid:
            diff = _whole(plan.total_interest_paid) - _whole(result["total_interest_paid"])
            if diff > 0:
                line += f", тобто на {format_uah(diff)} грн менше"
            elif diff < 0:
                line += f", тобто на {format_uah(-diff)} грн більше"
            else:
                line += ", тобто така сама"
        line += "."

    sentences = [_base_sentence(plan), line]
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
        asset_arbitrage — чи є заощадження зі ставкою, нижчою за ставку боргу
        (речення про них додається лише для питання про заощадження);
        savings_scenario — чи є в реченнях сценарій використання заощаджень.
    """
    if session is not None:
        return await _build_context(session, user_id, question)
    async with AsyncSessionLocal() as own_session:
        return await _build_context(own_session, user_id, question)


async def _build_context(session: AsyncSession, user_id: str, question: str | None) -> dict:
    """Складає речення контексту радника в межах переданої сесії."""
    plan = (await session.execute(
        select(MilpPlan)
        .where(MilpPlan.user_id == user_id)
        .order_by(MilpPlan.generated_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    debts = (await session.execute(
        select(Debt).where(Debt.user_id == user_id, Debt.is_active == True)
    )).scalars().all()

    savings = await _savings_total(session, user_id)
    savings_sentence = f"Заощадження: {format_uah(savings)} грн." if savings > 0 else None

    if not debts:
        sentences = [NO_DEBTS_SENTENCE] + ([savings_sentence] if savings_sentence else [])
        return {"sentences": sentences, "plan_available": False, "asset_arbitrage": False,
                "savings_scenario": False}

    usable_plan, _ = _usable_plan(plan, debts)
    if usable_plan is None or usable_plan.extra_monthly_uah is None or not usable_plan.plan_data:
        return {"sentences": [PLAN_UNAVAILABLE_SENTENCE], "plan_available": False, "asset_arbitrage": False,
                "savings_scenario": False}

    min_total = _min_payments_total(debts)
    sentences = _plan_sentences(usable_plan, debts, min_total)
    sentences.append(f"Разом боргів зараз {format_uah(sum(d.balance for d in debts))} грн.")
    sentences += _comparison_sentences(usable_plan.plan_data)

    availability = await _availability_sentence(session, user_id, usable_plan.extra_monthly_uah)
    if availability:
        sentences.append(availability)

    if savings_sentence:
        sentences.append(savings_sentence)
    arbitrage = await _find_asset_arbitrage(session, user_id, usable_plan.plan_data.get("milp", {}))
    savings_scenario = False
    if is_savings_question(question):
        what_if, _ = await _estimate_debt_reduction_from_assets(session, user_id, usable_plan)
        if what_if:
            sentences += _what_if_sentences(what_if)
            savings_scenario = True
        if arbitrage:
            sentences.append(_arbitrage_sentence(arbitrage))
            savings_scenario = True

    payment_question = parse_payment_question(question)
    if payment_question is not None:
        amount, mode = payment_question
        sentences += await _extra_question_sentences(
            session, user_id, usable_plan, debts, min_total, amount, mode,
        )

    # Однакові речення (нагадування про резерв) лишаються один раз.
    sentences = list(dict.fromkeys(sentences))
    return {"sentences": sentences, "plan_available": True, "asset_arbitrage": arbitrage is not None,
            "savings_scenario": savings_scenario}
