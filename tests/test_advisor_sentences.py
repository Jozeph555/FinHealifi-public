"""
Тести контексту AI-радника як готових речень і перевірки відповіді моделі.

(а) У тексті контексту немає заборонених слів.
(б) Усі числа контексту — цілі гривні й збігаються зі значеннями плану в БД.
(в) Перевірка чисел знаходить число, якого немає в контексті, і мовчить на
    коректній відповіді.
(г) Перевірка заборонених слів їх знаходить.
(д) Без плану, із застарілим планом або з планом без відбитка контекст — одне
    речення без чисел плану.
(е) Порівняння стратегій: несуттєва різниця — одне речення; борг, що не
    погашається, не порівнюється і переплата за ним не називається.
Також: розбір питання «ще Z» / «замість», речення сценарію з базою й різницею,
доступність «у k з n останніх місяців» для конкретної суми (k не зростає з
сумою), сценарій заощаджень лише для питань про заощадження, заборони
SYSTEM_PROMPT, лог контексту за ADVISOR_LOG_CONTEXT. Claude API не
викликається: клієнт моделі замінено заглушкою.
"""
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.services.advisor_context as advisor_context
import app.services.llm_advisor as llm_advisor
from app.api.assets import AssetCreateRequest, create_asset
from app.api.debt import DebtCreateRequest, ExtraPaymentRequest, create_debt, optimize_with_extra
from app.models.milp_plan import MilpPlan
from app.services.advisor_context import (
    NEGLIGIBLE_SAVINGS_SHARE,
    PLAN_UNAVAILABLE_SENTENCE,
    RESERVE_SENTENCE,
    _comparison_sentences,
    _plan_sentences,
    build_advisor_context,
    format_uah,
    is_savings_question,
    parse_payment_question,
)
from app.services.affordability import historical_free_cash_flow
from tests.test_affordability import _seed_months
from app.services.llm_advisor import (
    EXAMPLES_HEADER,
    SYSTEM_PROMPT,
    _NUMBER_RE,
    _format_context,
    answer_question,
    find_banned_words,
    find_unknown_numbers,
)

SPEC_BANNED = ("MILP", "avalanche", "snowball", "P25", "перцентиль", "квантиль", "медіана", "ліквідні активи")


async def _debt(db_session, real_user, **overrides):
    defaults = dict(name="Картка", balance=10000.0, monthly_rate=0.04, debt_type="revolving")
    defaults.update(overrides)
    return await create_debt(DebtCreateRequest(**defaults), current_user=real_user, db=db_session)


async def _two_debts(db_session, real_user):
    card = await _debt(db_session, real_user)
    await _debt(db_session, real_user, name="Розстрочка", balance=6000.0, monthly_rate=0.0,
                debt_type="installment", min_payment=500.0)
    return card


async def _plan(db_session, real_user, extra):
    await optimize_with_extra(ExtraPaymentRequest(extra_monthly_uah=extra), current_user=real_user,
                              db=db_session, months=36)
    return (await db_session.execute(
        select(MilpPlan).where(MilpPlan.user_id == real_user.id).order_by(MilpPlan.generated_at.desc()).limit(1)
    )).scalar_one()


def _text(context: dict) -> str:
    return _format_context(context)


# --- (а) заборонені слова в контексті ------------------------------------

async def test_context_has_no_banned_words_paid_plan(db_session, real_user):
    await _two_debts(db_session, real_user)
    await create_asset(AssetCreateRequest(name="Депозит", asset_type="Депозит", current_amount=3000,
                                          liquidity_level="high"), current_user=real_user, db=db_session)
    await _plan(db_session, real_user, extra=1000.0)

    text = _text(await build_advisor_context(
        real_user.id, session=db_session,
        question="А якби я платив ще 2000? І чи варто використати заощадження?",
    ))

    assert "Якби одноразово спрямувати" in text
    assert find_banned_words(text) == []
    for word in SPEC_BANNED:
        assert word.lower() not in text.lower(), word


async def test_context_has_no_banned_words_unpaid_plan_with_arbitrage(db_session, real_user):
    await _debt(db_session, real_user, balance=20000.0)
    await create_asset(AssetCreateRequest(name="Депозит", asset_type="Депозит", current_amount=5000,
                                          liquidity_level="high", annual_rate=2.0),
                       current_user=real_user, db=db_session)
    await _plan(db_session, real_user, extra=0.0)

    context = await build_advisor_context(real_user.id, session=db_session,
                                          question="Чи варто закрити борг заощадженнями?")
    text = _text(context)

    assert context["asset_arbitrage"] is True
    assert "Заощадження «Депозит» приносять менше" in text
    assert find_banned_words(text) == []
    for word in SPEC_BANNED:
        assert word.lower() not in text.lower(), word


# --- (б) числа контексту = цілі гривні з плану ------------------------------

async def test_context_numbers_are_whole_uah_from_plan(db_session, real_user):
    await _two_debts(db_session, real_user)
    plan = await _plan(db_session, real_user, extra=1000.0)
    milp = plan.plan_data["milp"]

    text = _text(await build_advisor_context(real_user.id, session=db_session))
    tokens = _NUMBER_RE.findall(text)

    assert tokens, text
    for token in tokens:
        assert "," not in token and "." not in token, token

    expected = {format_uah(v) for v in (
        1900, 400 + 500, 1000, 10000, 6000, 16000, plan.total_interest_paid,
        *milp["monthly_log"][0]["payments"].values(),
    )}
    expected.add(str(milp["months_to_payoff"]))
    for key in ("avalanche", "snowball", "minimum"):
        strategy = plan.plan_data[key]
        expected.add(format_uah(strategy["total_interest_paid"] - plan.total_interest_paid))
    assert set(tokens) <= expected, (set(tokens) - expected, text)

    assert "Щомісяця на борги йде 1 900 грн: 900 грн обов'язкових платежів і 1 000 грн понад них." in text
    assert "Картка: залишилось 10 000 грн, цього місяця платити " in text
    assert (f"За планом усі борги закриються через {milp['months_to_payoff']} міс., загальна переплата "
            f"{format_uah(plan.total_interest_paid)} грн.") in text


def test_format_uah_whole_hryvnias_with_space_separator():
    assert format_uah(17427.22) == "17 427"
    assert format_uah(1234567.5) == "1 234 568"
    assert format_uah(999.49) == "999"
    assert format_uah(0.5) == "1"


# --- (в) перевірка чисел відповіді -------------------------------------

CONTEXT_TEXT = (
    "- Щомісяця на борги йде 1 900 грн: 900 грн обов'язкових платежів і 1 000 грн понад них.\n"
    "- За планом усі борги закриються через 11 міс., загальна переплата 17 427 грн."
)


def test_number_check_finds_unknown_number():
    assert find_unknown_numbers("Тобі лишилось сплатити 7 777 грн за 11 міс.", CONTEXT_TEXT) == [7777.0]


def test_number_check_silent_on_correct_answer():
    answer = ("Ти щомісяця віддаєш на борги 1 900 грн, з них 1 000 грн понад обов'язкові. "
              "Так усі борги закриються через 11 міс., а переплата буде 17 427 грн.")
    assert find_unknown_numbers(answer, CONTEXT_TEXT) == []


def _mock_reply(monkeypatch, payload: dict):
    reply = SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))])
    monkeypatch.setattr(llm_advisor.client.messages, "create", AsyncMock(return_value=reply))


async def test_answer_logs_unknown_number_but_returns_answer(monkeypatch, caplog):
    _mock_reply(monkeypatch, {"answer": "Борги закриються через 9 міс.", "follow_up": []})
    context = {"sentences": ["За планом усі борги закриються через 11 міс., загальна переплата 17 427 грн."]}

    with caplog.at_level(logging.WARNING, logger="app.services.llm_advisor"):
        answer, _ = await answer_question("Коли я закрию борги?", context)

    assert answer == "Борги закриються через 9 міс."
    assert "числа, яких немає в контексті: [9.0]" in caplog.text


async def test_answer_no_warning_on_correct_answer(monkeypatch, caplog):
    _mock_reply(monkeypatch, {"answer": "Усі борги закриються через 11 міс. Так тримати!",
                              "follow_up": ["Що буде, якщо платити ще 2000?"]})
    context = {"sentences": ["За планом усі борги закриються через 11 міс., загальна переплата 17 427 грн."]}

    with caplog.at_level(logging.WARNING, logger="app.services.llm_advisor"):
        await answer_question("Коли я закрию борги?", context)

    assert caplog.text == ""


# --- (г) перевірка заборонених слів -------------------------------------

def test_banned_words_check_finds_words():
    found = find_banned_words("За стратегією Avalanche і медіаною виходить меньший платіж, ось лайфхак.")
    assert {"avalanche", "медіана", "меньший", "лайфхак"} <= set(found)
    assert find_banned_words("Спочатку найдорожчий борг дає ту саму переплату.") == []


async def test_answer_logs_banned_word(monkeypatch, caplog):
    _mock_reply(monkeypatch, {"answer": "MILP показує, що все добре.", "follow_up": []})

    with caplog.at_level(logging.WARNING, logger="app.services.llm_advisor"):
        answer, _ = await answer_question("Як справи?", {"sentences": []})

    assert answer == "MILP показує, що все добре."
    assert "заборонені слова: ['MILP']" in caplog.text


# --- (д) план відсутній або застарів -----------------------------------

async def test_no_plan_context_is_single_sentence(db_session, real_user):
    await _two_debts(db_session, real_user)

    context = await build_advisor_context(real_user.id, session=db_session)

    assert context["sentences"] == [PLAN_UNAVAILABLE_SENTENCE]
    assert context["plan_available"] is False
    assert _NUMBER_RE.findall(_text(context)) == []


async def test_stale_plan_context_has_no_plan_numbers(db_session, real_user):
    card = await _two_debts(db_session, real_user)
    await _plan(db_session, real_user, extra=1000.0)
    card.balance = 9000.0
    await db_session.commit()

    context = await build_advisor_context(real_user.id, session=db_session, question="А якби я платив ще 2000?")

    assert context["sentences"] == [PLAN_UNAVAILABLE_SENTENCE]
    assert _NUMBER_RE.findall(_text(context)) == []


async def test_plan_without_fingerprint_is_treated_as_unavailable(db_session, real_user):
    await _two_debts(db_session, real_user)
    plan = await _plan(db_session, real_user, extra=1000.0)
    plan.debts_fingerprint = None
    await db_session.commit()

    context = await build_advisor_context(real_user.id, session=db_session)
    assert context["sentences"] == [PLAN_UNAVAILABLE_SENTENCE]


async def test_no_debts_context(db_session, real_user):
    context = await build_advisor_context(real_user.id, session=db_session)
    assert context["sentences"] == ["Активних боргів зараз немає."]


# --- (е) порівняння стратегій ------------------------------------------

def _plan_data(milp=10000.0, avalanche=10050.0, snowball=None, minimum=None, milp_months=11):
    def entry(interest, months):
        return {"total_interest_paid": interest, "months_to_payoff": months}
    return {
        "milp": entry(milp, milp_months),
        "avalanche": entry(avalanche, milp_months),
        "snowball": entry(snowball, 12) if snowball is not None else entry(20000.0, None),
        "minimum": entry(minimum, 30) if minimum is not None else entry(40000.0, None),
    }


def test_negligible_difference_gives_single_sentence():
    assert NEGLIGIBLE_SAVINGS_SHARE == 0.01
    assert _comparison_sentences(_plan_data(avalanche=10050.0)) == [
        "Спочатку найдорожчий борг дає ту саму переплату, окремо порівнювати не треба."
    ]


def test_difference_exactly_at_threshold_is_shown():
    """Різниця рівно NEGLIGIBLE_SAVINGS_SHARE від переплати стратегії вже показується."""
    assert _comparison_sentences(_plan_data(milp=9900.0, avalanche=10000.0)) == [
        "Якщо гасити спочатку найдорожчий борг, переплата буде на 100 грн більша, ніж за розрахованим планом."
    ]


def test_significant_difference_named_in_uah():
    sentences = _comparison_sentences(_plan_data(avalanche=10200.0, snowball=11500.4, minimum=25000.0))
    assert sentences == [
        "Якщо гасити спочатку найдорожчий борг, переплата буде на 200 грн більша, ніж за розрахованим планом.",
        "Якщо гасити спочатку найменший борг, переплата буде на 1 500 грн більша, ніж за розрахованим планом.",
        "Якщо платити тільки обов'язкові платежі, переплата буде на 15 000 грн більша, ніж за розрахованим планом.",
    ]


def test_strategies_that_do_not_pay_off_are_not_compared():
    sentences = _comparison_sentences(_plan_data(avalanche=10200.0))
    assert len(sentences) == 1
    assert "найменший" not in sentences[0] and "обов'язкові" not in sentences[0]


def test_unpaid_plan_has_no_comparison_and_no_overpay():
    data = _plan_data(milp_months=None)
    assert _comparison_sentences(data) == []

    data["milp"].update(monthly_log=[{"payments": {"Картка": 800.0}}], final_balances={"Картка": 20000.0})
    plan = MilpPlan(plan_data=data, months_to_payoff=36, total_interest_paid=28800.0, extra_monthly_uah=0.0)
    debt = SimpleNamespace(name="Картка", balance=20000.0, monthly_rate=0.04, debt_type="revolving",
                           min_payment=None)
    sentences = _plan_sentences(plan, [debt], 800.0)

    assert sentences[1] == ("Картка: залишилось 20 000 грн, цього місяця платити 800 грн; "
                            "за 36 міс. цей борг не погашається, переплату за ним не називай.")
    assert sentences[2] == "За планом не всі борги закриються за 36 міс., тому загальну переплату не називай."
    assert "28 800" not in " ".join(sentences)


# --- Питання «а якби я платив ще Z» -----------------------------------

@pytest.mark.parametrize("question, expected", [
    ("А якби я платив ще 2000?", (2000.0, "add")),
    ("Що буде, якщо платити додатково 1 500 грн?", (1500.0, "add")),
    ("А якщо платити на 3000 грн більше?", (3000.0, "add")),
    ("А якби я платив 3000 замість 1000?", (3000.0, "total")),
    ("Якщо платити усього 2 500 грн на місяць?", (2500.0, "total")),
    ("Якби я платив рівно 4000?", (4000.0, "total")),
    ("Якщо платити загалом 1000, коли закрию?", (1000.0, "total")),
    ("Скільки мені ще платити?", None),
    ("Коли я закрию борги?", None),
    ("Якщо платити ще 6 місяців, що буде?", None),
])
def test_parse_payment_question(question, expected):
    assert parse_payment_question(question) == expected


async def test_extra_question_add_has_base_and_difference(db_session, real_user):
    await _two_debts(db_session, real_user)
    plan = await _plan(db_session, real_user, extra=1000.0)
    milp = plan.plan_data["milp"]
    scenario = advisor_context.optimize_milp(
        advisor_context._debt_dicts(await _active_debts(db_session, real_user)), 900 + 3000, 36,
    )

    sentences = (await build_advisor_context(real_user.id, session=db_session,
                                             question="А якби я платив ще 2 000 грн?"))["sentences"]

    assert (f"Зараз додатковий платіж 1 000 грн: борги закриються через {milp['months_to_payoff']} міс., "
            f"переплата {format_uah(plan.total_interest_paid)} грн.") in sentences
    diff = int(format_uah(plan.total_interest_paid).replace(" ", "")) - int(
        format_uah(scenario["total_interest_paid"]).replace(" ", ""))
    assert (f"Якби до цього додати ще 2 000 грн (разом 3 000 грн понад обов'язкові), борги закрилися б через "
            f"{scenario['months_to_payoff']} міс., переплата {format_uah(scenario['total_interest_paid'])} грн, "
            f"тобто на {format_uah(diff)} грн менше.") in sentences
    assert sentences.count(RESERVE_SENTENCE) == 1


async def test_extra_question_total_replaces_payment(db_session, real_user):
    await _two_debts(db_session, real_user)
    await _plan(db_session, real_user, extra=1000.0)

    text = _text(await build_advisor_context(real_user.id, session=db_session,
                                             question="А якби я платив 3000 замість 1000?"))

    assert "Якби додатковий платіж був 3 000 грн замість 1 000 грн, борги закрилися б через" in text
    assert "разом" not in text


async def _active_debts(db_session, real_user):
    from app.models.debt import Debt
    return (await db_session.execute(
        select(Debt).where(Debt.user_id == real_user.id, Debt.is_active == True)  # noqa: E712
    )).scalars().all()


# --- Доступність --------------------------------------------------------

async def _seed_history(db_session, real_user):
    # 12 повних місяців (k=1..12; k=13 — перший, неповний місяць блоку).
    # Вільні гроші місяця = 1 000 · (13 - k): від 1 000 до 12 000.
    await _seed_months(db_session, real_user, range(1, 14),
                       income_of=lambda k: 40_000 + 1_000 * (13 - k), expense_of=lambda k: 40_000)


async def test_availability_k_does_not_grow_with_amount(db_session, real_user):
    await _seed_history(db_session, real_user)

    ks = [(await historical_free_cash_flow(db_session, real_user.id, amount, 12))["count_ge_extra"]
          for amount in (0, 500, 1000, 2500, 3000, 6000, 12000, 12001, 50000)]

    assert ks == sorted(ks, reverse=True)
    assert ks[0] == 12 and ks[-1] == 0


async def test_availability_counted_separately_for_scenario_amount(db_session, real_user):
    await _seed_history(db_session, real_user)
    await _two_debts(db_session, real_user)
    await _plan(db_session, real_user, extra=1000.0)

    text = _text(await build_advisor_context(real_user.id, session=db_session,
                                             question="А якби я платив ще 5 000 грн?"))

    note = "це оцінка за твоїми минулими операціями"
    assert f"У 12 з 12 останніх місяців вільних грошей вистачало б на додатковий платіж 1 000 грн; {note}." in text
    assert f"У 7 з 12 останніх місяців вільних грошей вистачало б на додатковий платіж 6 000 грн; {note}." in text
    assert "завеликий" not in text and "половин" not in text


async def test_availability_without_enough_history_is_omitted(db_session, real_user):
    """Один повний місяць історії: підсумків немає, речення про доступність теж."""
    await _seed_months(db_session, real_user, range(1, 3),
                       income_of=lambda k: 50_000, expense_of=lambda k: 40_000)
    await _two_debts(db_session, real_user)
    await _plan(db_session, real_user, extra=1000.0)

    text = _text(await build_advisor_context(real_user.id, session=db_session,
                                             question="А якби я платив ще 2000?"))

    assert "останніх місяців" not in text


async def test_availability_monkeypatched_history_sentence(db_session, real_user, monkeypatch):
    await _two_debts(db_session, real_user)
    await _plan(db_session, real_user, extra=1000.0)

    async def _history(session, user_id, extra, months=12, today=None):
        return {"count_ge_extra": 3, "n_months_used": 12, "median": 4321.0, "p25": 1234.0}

    monkeypatch.setattr(advisor_context, "historical_free_cash_flow", _history)
    text = _text(await build_advisor_context(real_user.id, session=db_session))

    assert ("У 3 з 12 останніх місяців вільних грошей вистачало б на додатковий платіж 1 000 грн; "
            "це оцінка за твоїми минулими операціями.") in text
    assert "4 321" not in text and "1 234" not in text and "4321" not in text


# --- Сценарій заощаджень лише для питань про заощадження ---------------------

@pytest.mark.parametrize("question, expected", [
    ("Чи варто скористатися заощадженнями, щоб швидше закрити борг?", True),
    ("А якщо погасити достроково?", True),
    ("Можна закрити раніше одним платежем?", True),
    ("Я відклав трохи грошей, що з ними робити?", True),
    ("Коли я закрию борги?", False),
    (None, False),
])
def test_is_savings_question(question, expected):
    assert is_savings_question(question) is expected


async def test_savings_scenario_absent_for_neutral_question(db_session, real_user):
    await _two_debts(db_session, real_user)
    await create_asset(AssetCreateRequest(name="Депозит", asset_type="Депозит", current_amount=3000,
                                          liquidity_level="high"), current_user=real_user, db=db_session)
    await _plan(db_session, real_user, extra=1000.0)

    neutral = _text(await build_advisor_context(real_user.id, session=db_session,
                                                question="Коли я закрию борги?"))
    without_question = _text(await build_advisor_context(real_user.id, session=db_session))
    savings = _text(await build_advisor_context(real_user.id, session=db_session,
                                                question="Чи варто закрити борги із заощаджень?"))

    for text in (neutral, without_question):
        assert "Заощадження: 3 000 грн." in text
        assert "Якби одноразово спрямувати" not in text
        assert "резерв" not in text
    assert "Якби одноразово спрямувати 3 000 грн заощаджень" in savings
    assert savings.count(RESERVE_SENTENCE) == 1


# --- План відсутній: справжні назви з інтерфейсу --------------------------

def test_plan_unavailable_sentence_names_real_ui():
    debts_page = (Path(__file__).resolve().parents[1] / "frontend/src/pages/Debts.jsx").read_text()
    assert "«Побудувати план»" in PLAN_UNAVAILABLE_SENTENCE and '"Побудувати план"' in debts_page
    assert "«Борги»" in PLAN_UNAVAILABLE_SENTENCE and ">Борги</h1>" in debts_page
    assert "Додатковий платіж на місяць" in debts_page
    for phrase in ("внес", "стратегі"):
        assert phrase not in PLAN_UNAVAILABLE_SENTENCE


# --- Лог контексту ------------------------------------------------------

async def test_context_log_disabled_by_default(monkeypatch, caplog):
    monkeypatch.delenv("ADVISOR_LOG_CONTEXT", raising=False)
    _mock_reply(monkeypatch, {"answer": "Усі борги закриються через 11 міс.", "follow_up": []})
    context = {"sentences": ["За планом усі борги закриються через 11 міс., загальна переплата 17 427 грн."]}

    with caplog.at_level(logging.DEBUG, logger="app.services.llm_advisor"):
        await answer_question("Коли я закрию борги?", context)

    assert "ADVISOR_LOG_CONTEXT" not in caplog.text
    assert "17 427" not in caplog.text


async def test_context_log_enabled_by_env(monkeypatch, caplog):
    monkeypatch.setenv("ADVISOR_LOG_CONTEXT", "1")
    _mock_reply(monkeypatch, {"answer": "Усі борги закриються через 11 міс.", "follow_up": []})
    context = {"sentences": ["За планом усі борги закриються через 11 міс., загальна переплата 17 427 грн."]}

    with caplog.at_level(logging.WARNING, logger="app.services.llm_advisor"):
        await answer_question("Коли я закрию борги?", context)

    assert "ADVISOR_LOG_CONTEXT (answer):" in caplog.text
    assert "- За планом усі борги закриються через 11 міс., загальна переплата 17 427 грн." in caplog.text


# --- SYSTEM_PROMPT -------------------------------------------------------

def test_system_prompt_rules():
    for fragment in (
        "Звертайся на «ти»",
        "3–5 речень",
        "Нічого не рахуй",
        "не округлюй",
        "порад про витрати, якщо про це не питали",
        "Не згадуй здоров'я",
        "«у k з n останніх місяців»",
        "спершу передай, як його перебудувати",
        "Ніяких оцінок і вердиктів",
        "«гарна подушка», «правильне рішення»",
        "«працює добре», «навантаження велике»",
        "Не радь змінювати\n  чи зменшувати додатковий платіж",
        "Не описуй функцій застосунку, яких немає в даних",
        "«внести борги» чи «вибрати стратегію»",
        "не більше одного разу за відповідь і лише тоді, коли відповідаєш про сценарій «якби»",
        "ти відповідаєш про твої борги і план",
        "запропонуй конкретне питання з цього кола",
        "«утримуватися при», «вистачає лишень», «в кожному кроці»",
    ):
        assert fragment in SYSTEM_PROMPT, fragment
    # Резерв згадується лише в прикладі сценарію «якби», не як загальне правило.
    assert "резерв" not in SYSTEM_PROMPT.split(EXAMPLES_HEADER)[0]


def test_banned_words_include_calques_and_verdicts():
    text = "Варто утримуватися при покупках, грошей вистачає лишень на це, в кожному кроці — правильне рішення."
    assert {"утримуватися при", "вистачає лишень", "в кожному кроці", "правильне рішення"} <= set(
        find_banned_words(text))
