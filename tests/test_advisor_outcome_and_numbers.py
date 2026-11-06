"""
Речення наслідку, порівняння версій і блокуюча перевірка чисел у відповіді радника.

Речення «За внесеними платежами …» відсутнє, коли чинна версія побудована від
фактичних залишків (month_start_actual) і її закритих місяців ще немає, і
повертається, коли такий місяць є. Речення порівняння з попередньою версією
містить дати стартових залишків обох розрахунків і різниці закриття й
переплати, обчислені бекендом. Число з відповіді, якого немає в контексті чи
питанні, — порушення: одна повторна генерація, а за повторного порушення —
детерміновані речення контексту для категорії питання.
«Сьогодні» — підміна app.services.debt_ledger.today_kyiv; Claude API підмінено.
"""
import json
from datetime import date
from types import SimpleNamespace

import pytest

import app.services.debt_ledger as ledger
from app.api.debt import DebtCreateRequest, ExtraPaymentRequest, create_debt, optimize_with_extra
from app.models.debt_plan import DebtPlanMonth
from app.services import debt_plan_versions as versions_service
from app.services import llm_advisor
from app.services.advisor_context import (
    MONTH_STATUS_HEADER,
    build_advisor_context,
    format_uah,
    question_category,
)
from app.services.debt_outcome import whole_uah
from app.services.llm_advisor import NO_DATA_ANSWER, answer_question, find_rule_violations

_REAL_TODAY = ledger.today_kyiv
OUTCOME_PREFIX = "За внесеними платежами"


@pytest.fixture
def clock(monkeypatch):
    """Керований «сьогодні» для журналу, план-факту й радника."""
    state = {"today": date(2026, 11, 14)}
    monkeypatch.setattr(ledger, "today_kyiv",
                        lambda today=None: _REAL_TODAY(today) if today is not None else state["today"])
    return state


async def _accept(db_session, user, extra, today):
    response = await optimize_with_extra(ExtraPaymentRequest(extra_monthly_uah=extra), current_user=user,
                                         db=db_session, months=36, today=today)
    version, created = await versions_service.accept_plan(db_session, user.id, response["plan_id"], today)
    assert created
    return version


async def _two_versions(db_session, user, clock):
    """Версія 1 з 14.11, листопад — «платежів не було», версія 2 від залишків на 01.12, зафіксована 03.12."""
    for request in (
        DebtCreateRequest(name="Картка", balance=10000.0, monthly_rate=0.04, debt_type="revolving"),
        DebtCreateRequest(name="Розстрочка", balance=6000.0, monthly_rate=0.02, debt_type="installment",
                          min_payment=500.0),
    ):
        await create_debt(request, current_user=user, db=db_session, today=clock["today"])
    v1 = await _accept(db_session, user, 1000.0, clock["today"])
    clock["today"] = date(2026, 11, 20)
    v2 = await _accept(db_session, user, 1500.0, clock["today"])
    clock["today"] = date(2026, 12, 3)
    db_session.add(DebtPlanMonth(user_id=user.id, month=date(2026, 11, 1), no_payments_confirmed=True))
    await db_session.commit()
    v2, created = await versions_service.materialize(db_session, user.id, force=True, today=clock["today"])
    assert created
    return v1, v2


def _payoff(version):
    return ledger.add_months(version.effective_from, version.months_to_payoff - 1)


# --- 1. речення наслідку ---------------------------------------------------------------------

async def test_outcome_sentence_absent_for_actual_start_version_without_closed_months(db_session, real_user, clock):
    await _two_versions(db_session, real_user, clock)
    sentences = (await build_advisor_context(real_user.id, session=db_session))["sentences"]
    assert not any(s.startswith(OUTCOME_PREFIX) for s in sentences), sentences
    assert MONTH_STATUS_HEADER in sentences


async def test_outcome_sentence_returns_when_version_has_closed_month(db_session, real_user, clock):
    await _two_versions(db_session, real_user, clock)
    clock["today"] = date(2027, 1, 10)
    db_session.add(DebtPlanMonth(user_id=real_user.id, month=date(2026, 12, 1), no_payments_confirmed=True))
    await db_session.commit()
    sentences = (await build_advisor_context(real_user.id, session=db_session))["sentences"]
    assert any(s.startswith(OUTCOME_PREFIX) for s in sentences), sentences


async def test_outcome_sentence_kept_for_acceptance_balances_version(db_session, real_user, clock):
    """Перша версія (від залишків на дату прийняття) у місяці прийняття речення наслідку не втрачає."""
    await create_debt(DebtCreateRequest(name="Картка", balance=10000.0, monthly_rate=0.04, debt_type="revolving"),
                      current_user=real_user, db=db_session, today=clock["today"])
    await _accept(db_session, real_user, 1000.0, clock["today"])
    sentences = (await build_advisor_context(real_user.id, session=db_session))["sentences"]
    assert any(s.startswith(OUTCOME_PREFIX) for s in sentences), sentences


# --- 2. порівняння версій ----------------------------------------------------------------------

async def test_previous_plan_sentence_has_start_dates_and_deltas(db_session, real_user, clock):
    v1, v2 = await _two_versions(db_session, real_user, clock)
    sentences = (await build_advisor_context(real_user.id, session=db_session))["sentences"]
    sentence = next(s for s in sentences if s.startswith("Попередній план"))

    assert "рахувався від залишків на 14 листопада 2026 р." in sentence
    assert "рахується від залишків на 1 грудня 2026 р." in sentence
    assert f"закриття — {ledger.month_name(_payoff(v1))}" in sentence
    assert f"переплата {format_uah(ledger.kop_to_uah(v1.total_interest_kop))} грн" in sentence
    assert f"переплата {format_uah(ledger.kop_to_uah(v2.total_interest_kop))} грн" in sentence

    shift = (_payoff(v2).year - _payoff(v1).year) * 12 + _payoff(v2).month - _payoff(v1).month
    if shift == 0:
        assert "закриття в тому самому місяці" in sentence
    else:
        word = "пізніше" if shift > 0 else "раніше"
        assert f"закриття на {abs(shift)} міс. {word}" in sentence
    diff = whole_uah(v2.total_interest_kop) - whole_uah(v1.total_interest_kop)
    if diff == 0:
        assert "переплата та сама" in sentence
    else:
        word = "більша" if diff > 0 else "менша"
        assert f"переплата на {format_uah(abs(diff))} грн {word}" in sentence
    assert "через" not in sentence and "пропуск" not in sentence
    assert sentence.count(".") >= 1 and sentence.endswith(".")


def test_previous_plan_sentence_unit_deltas():
    from app.services.advisor_context import _previous_version_sentence

    def version(no, start_kind, accepted_on, effective_from, months, interest_kop):
        return SimpleNamespace(id=f"v{no}", version_no=no, start_kind=start_kind, accepted_on=accepted_on,
                               effective_from=effective_from, effective_to=None, materialized_at=True,
                               months_to_payoff=months, total_interest_kop=interest_kop, status="accepted")

    prev = version(1, "acceptance_balances", date(2026, 11, 5), date(2026, 11, 1), 23, 5_000_000)
    current = version(2, "month_start_actual", date(2026, 11, 20), date(2026, 12, 1), 22, 4_870_000)
    prev.effective_to = date(2026, 12, 1)
    sentence = _previous_version_sentence([prev, current], current)
    assert sentence == (
        "Попередній план (діяв з 1 листопада 2026) рахувався від залишків на 5 листопада 2026 р.: "
        "закриття — вересень 2028 р., переплата 50 000 грн; "
        "чинний рахується від залишків на 1 грудня 2026 р.: закриття — вересень 2028 р., переплата 48 700 грн; "
        "у чинному закриття в тому самому місяці, переплата на 1 300 грн менша."
    )
    later = version(2, "month_start_actual", date(2026, 11, 20), date(2026, 12, 1), 24, 5_084_049)
    sentence = _previous_version_sentence([prev, later], later)
    assert sentence.endswith("у чинному закриття на 2 міс. пізніше, переплата на 840 грн більша.")


# --- 3. блокуюча перевірка чисел ---------------------------------------------------------------

CONTEXT = {"sentences": [
    MONTH_STATUS_HEADER,
    "Листопад 2026 р. (план №1): за планом 9 000 грн, внесено 0 грн, різниця −9 000 грн.",
    "Попередній план (діяв з 1 листопада 2026): переплата 50 000 грн; чинний: переплата 48 700 грн.",
]}


def test_unknown_number_is_violation():
    violations = find_rule_violations("Переплата стала меншою на 1 300 грн.", CONTEXT, "Чи я виконую план?")
    assert any("1 300" in v for v in violations), violations


@pytest.mark.parametrize("answer", [
    "За планом було 9 000 грн, внесено 0 грн.",
    "За планом було 9000 грн.",
    "Переплата близько 50 тис. грн.",
    "Переплата 48,7 тис. грн.",
    "Листопад 2026 р., план №1.",
])
def test_known_numbers_with_rounding_and_thousands_pass(answer):
    assert find_rule_violations(answer, CONTEXT, "Чи я виконую план?") == []


def test_number_from_question_is_allowed():
    assert find_rule_violations("Якщо платити ще 2 500 грн, …", CONTEXT, "А якби я платив ще 2500?") == []


def test_wrong_thousands_rounding_is_violation():
    assert find_rule_violations("Переплата близько 64 тис. грн.", CONTEXT, "Скільки переплата?")


def _fake_client(monkeypatch, answers):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        text = json.dumps({"answer": answers[len(calls) - 1], "follow_up": ["Коли я закрию борги?"]},
                          ensure_ascii=False)
        return SimpleNamespace(content=[SimpleNamespace(text=text)])

    monkeypatch.setattr(llm_advisor, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    return calls


def _context_with_fallback():
    status = CONTEXT["sentences"][:2]
    return {**CONTEXT, "fallback_sentences": {"plan_status": status, "previous_plan": [CONTEXT["sentences"][2]],
                                              "general": ["Щомісяця на борги йде 9 500 грн."]}}


async def test_unknown_number_triggers_one_retry(monkeypatch):
    calls = _fake_client(monkeypatch, ["Ти заплатив 1 300 грн.", "За планом було 9 000 грн, внесено 0 грн."])
    answer, follow_up = await answer_question("Чи я виконую план?", _context_with_fallback())
    assert len(calls) == 2
    assert answer == "За планом було 9 000 грн, внесено 0 грн."
    assert follow_up == ["Коли я закрию борги?"]
    assert "1 300" in calls[1]["messages"][-1]["content"]


async def test_repeated_violation_returns_backend_sentences(monkeypatch):
    calls = _fake_client(monkeypatch, ["Ти заплатив 1 300 грн.", "Ти заплатив 1 500 грн."])
    context = _context_with_fallback()
    answer, follow_up = await answer_question("Чи я виконую план?", context)
    assert len(calls) == 2
    assert answer == " ".join(context["fallback_sentences"]["plan_status"])
    assert follow_up == []


async def test_repeated_violation_without_category_sentences_answers_no_data(monkeypatch):
    _fake_client(monkeypatch, ["Різниця 1 300 грн.", "Різниця 1 500 грн."])
    context = {**CONTEXT, "fallback_sentences": {"general": []}}
    answer, _ = await answer_question("Чим відрізняється попередній план?", context)
    assert answer == NO_DATA_ANSWER


@pytest.mark.parametrize("question, category", [
    ("Чи я виконую план?", "plan_status"),
    ("Скільки я вніс за листопад?", "plan_status"),
    ("Чим відрізняється попередній план від чинного?", "previous_plan"),
    ("А якби я платив ще 1 500?", "what_if"),
    ("Чи варто використати заощадження?", "savings"),
    ("Коли я закрию всі борги?", "general"),
])
def test_question_category(question, category):
    assert question_category(question) == category


async def test_context_has_fallback_sentences_per_category(db_session, real_user, clock):
    await _two_versions(db_session, real_user, clock)
    context = await build_advisor_context(real_user.id, session=db_session, question="Чи я виконую план?")
    fallback = context["fallback_sentences"]
    assert fallback["plan_status"][0] == MONTH_STATUS_HEADER
    assert fallback["previous_plan"] and fallback["previous_plan"][0].startswith("Попередній план")
    assert fallback["general"][0].startswith("Щомісяця на борги йде")
    for sentences in fallback.values():
        assert all(s in context["sentences"] for s in sentences)
