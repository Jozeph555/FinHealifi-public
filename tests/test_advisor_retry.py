"""
Тести повторної генерації відповіді радника, прикладів тону в SYSTEM_PROMPT і
речення про борг, на який іде найбільше понад обов'язкові платежі.

Відповідь з виразом з RETRY_PATTERNS або зі згадкою заощаджень без сценарію
й без питання про них генерується ще раз з повідомленням-коригуванням;
повертається друга відповідь, навіть якщо вона теж порушує правила (тоді
запис у лозі). Claude API не викликається: клієнт моделі замінено заглушкою.
"""
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.llm_advisor as llm_advisor
from app.services.advisor_context import _focus_debt_sentence
from app.services.llm_advisor import (
    EXAMPLES_BLOCK,
    EXAMPLES_HEADER,
    RETRY_PATTERNS,
    SYSTEM_PROMPT,
    answer_question,
    find_banned_words,
    find_rule_violations,
)

CONTEXT = {"sentences": ["За планом усі борги закриються через 11 міс., загальна переплата 17 427 грн.",
                         "Заощадження: 45 000 грн."],
           "savings_scenario": False}


def _reply(answer: str):
    payload = {"answer": answer, "follow_up": ["Коли я закрию борги?"]}
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))])


def _mock_replies(monkeypatch, *answers):
    mock = AsyncMock(side_effect=[_reply(a) for a in answers])
    monkeypatch.setattr(llm_advisor.client.messages, "create", mock)
    return mock


# --- find_rule_violations ----------------------------------------------

@pytest.mark.parametrize("phrase", [
    "Це добре, що план уже є.", "це гарно", "Найкраще просто продовжувати платити.",
    "Це правильне рішення.", "У тебе гарна подушка.", "Можна змінити стратегію.",
    "Спробуй вибрати стратегію.", "Зміни стратегію на іншу.",
])
def test_retry_phrases_detected_case_insensitive(phrase):
    assert find_rule_violations(phrase.upper(), CONTEXT, "Що мені робити?")


def test_savings_mention_without_scenario_is_violation():
    assert find_rule_violations("Спрямуй заощадження на борги.", CONTEXT, "Що мені робити?") == [
        "згадка заощаджень, про які не питали"
    ]


def test_savings_mention_allowed_for_savings_question_or_scenario():
    answer = "Твої заощадження — 45 000 грн."
    assert find_rule_violations(answer, CONTEXT, "Які в мене заощадження?") == []
    assert find_rule_violations(answer, {**CONTEXT, "savings_scenario": True}, "Що мені робити?") == []


def test_clean_answer_has_no_violations():
    answer = "За планом усі борги закриються через 11 міс., переплата буде 17 427 грн."
    assert find_rule_violations(answer, CONTEXT, "Коли я закрию борги?") == []


# --- повторна генерація -------------------------------------------------

async def test_clean_first_answer_single_call(monkeypatch):
    mock = _mock_replies(monkeypatch, "Усі борги закриються через 11 міс.")

    answer, _ = await answer_question("Коли я закрию борги?", CONTEXT)

    assert answer == "Усі борги закриються через 11 міс."
    assert mock.await_count == 1


async def test_violating_first_answer_replaced_by_clean_second(monkeypatch, caplog):
    first = "Це добре, що план є. Найкраще спрямувати заощадження на борги."
    second = "Усі борги закриються через 11 міс., переплата буде 17 427 грн."
    mock = _mock_replies(monkeypatch, first, second)

    with caplog.at_level(logging.WARNING, logger="app.services.llm_advisor"):
        answer, follow_up = await answer_question("Що мені робити?", CONTEXT)

    assert answer == second and follow_up == ["Коли я закрию борги?"]
    assert mock.await_count == 2
    retry_messages = mock.await_args_list[1].kwargs["messages"]
    assert retry_messages[1]["role"] == "assistant" and "Це добре" in retry_messages[1]["content"]
    correction = retry_messages[2]["content"]
    assert correction.startswith("Попередня відповідь порушила правила: ")
    assert "вираз «це добре»" in correction and "вираз «найкраще»" in correction
    assert "згадка заощаджень, про які не питали" in correction
    assert correction.endswith("Відповідай знову за правилами, без цих елементів.")
    assert "повторна відповідь теж порушила правила" not in caplog.text


async def test_both_answers_violate_second_returned_and_logged(monkeypatch, caplog):
    first = "Найкраще продовжувати платити."
    second = "Можна змінити стратегію, і це добре."
    mock = _mock_replies(monkeypatch, first, second)

    with caplog.at_level(logging.WARNING, logger="app.services.llm_advisor"):
        answer, _ = await answer_question("Що мені робити?", CONTEXT)

    assert answer == second
    assert mock.await_count == 2
    assert "повторна відповідь теж порушила правила" in caplog.text
    assert "вираз «змінити стратегію»" in caplog.text


# --- приклади тону в SYSTEM_PROMPT -------------------------------------

def test_prompt_has_examples_block_with_four_examples():
    assert EXAMPLES_HEADER == (
        "Приклади тону. Числа в прикладах вигадані, у відповіді використовуй ЛИШЕ числа з контексту."
    )
    assert SYSTEM_PROMPT.endswith(EXAMPLES_BLOCK)
    for question in ("«Що мені робити?»", "«А якби я платив ще 1 500?»", "«Які в мене заощадження?»",
                     "«Як вигідно інвестувати в акції?»"):
        assert f"Запитання: {question}" in EXAMPLES_BLOCK
    assert EXAMPLES_BLOCK.count("Відповідь:") == 4


def test_examples_contain_no_banned_words_verdicts_or_strategies():
    assert find_banned_words(EXAMPLES_BLOCK) == []
    for phrase, pattern in RETRY_PATTERNS.items():
        assert not llm_advisor._RETRY_RES[phrase].search(EXAMPLES_BLOCK), phrase
    assert "стратегі" not in EXAMPLES_BLOCK


def test_examples_follow_scenario_shape():
    examples = EXAMPLES_BLOCK.split("Запитання: ")[1:]
    what_to_do, extra, savings, off_topic = examples
    assert "заощадж" not in what_to_do
    assert "Зараз" in extra and "Якби додати ще" in extra and "менше" in extra
    assert "у 5 з 12 останніх місяців" in extra and extra.count("резерв") == 1
    assert "Якби" not in savings
    assert "Я відповідаю лише про твої борги і план" in off_topic and off_topic.count("?") == 2


def test_prompt_rules_forbid_savings_and_strategy_mentions():
    rules = SYSTEM_PROMPT.split(EXAMPLES_HEADER)[0]
    assert "Не згадуй заощадження, якщо про них не питали і в даних немає сценарію з ними." in rules
    assert "вибору стратегії немає" in rules
    assert "«це добре», «це гарно», «найкраще»" in rules


# --- речення про борг з найбільшою частиною понад обов'язкові ------------

def _debt(name, balance, rate, debt_type="installment", min_payment=500.0):
    return SimpleNamespace(name=name, balance=balance, monthly_rate=rate, debt_type=debt_type,
                           min_payment=min_payment)


def test_focus_sentence_names_highest_rate_debt():
    debts = [_debt("Картка", 10000.0, 0.04, "revolving", None), _debt("Розстрочка", 6000.0, 0.0)]
    sentence = _focus_debt_sentence(debts, {"Картка": 1400.0, "Розстрочка": 500.0})
    assert sentence == ("Цього місяця понад обов'язкові платежі найбільше йде на «Картка»: 1 000 грн понад "
                        "його обов'язковий платіж, бо це борг з найвищою ставкою серед твоїх боргів.")


def test_focus_sentence_without_rate_reason_for_other_debt():
    debts = [_debt("Картка", 10000.0, 0.04, "revolving", None), _debt("Розстрочка", 6000.0, 0.0)]
    sentence = _focus_debt_sentence(debts, {"Картка": 400.0, "Розстрочка": 900.0})
    assert sentence == ("Цього місяця понад обов'язкові платежі найбільше йде на «Розстрочка»: 400 грн понад "
                        "його обов'язковий платіж.")


def test_focus_sentence_absent_when_only_minimums_paid():
    debts = [_debt("Картка", 10000.0, 0.04, "revolving", None), _debt("Розстрочка", 6000.0, 0.0)]
    assert _focus_debt_sentence(debts, {"Картка": 400.0, "Розстрочка": 500.0}) is None
