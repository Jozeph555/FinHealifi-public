"""
Статус закритих місяців і порівняння з попереднім планом у контексті радника.

Сценарій: версія 1 прийнята 14.11, листопад позначено «платежів не було»,
версія 2 (від фактичних залишків) діє з 1 грудня. Контекст містить рядок
листопада з «внесено 0 грн» і різницею мінус план листопада, підсумок за
закриті місяці, речення порівняння версій; без попередньої версії речення
порівняння немає; «р.» перед дужкою не втрачає крапку; додатковий платіж у
контексті збігається з GET /debt/plan/active. Інструкція моделі: на питання
про виконання плану — лише ці речення, вердикти без них — порушення.
«Сьогодні» — підміна app.services.debt_ledger.today_kyiv; Claude API не викликається.
"""
from datetime import date

import pytest
from sqlalchemy import select

import app.services.debt_ledger as ledger
from app.api.debt import DebtCreateRequest, ExtraPaymentRequest, create_debt, optimize_with_extra
from app.api.debt_plan_fact import active_plan
from app.models.debt_plan import DebtPlanMonth, DebtPlanSchedule
from app.services import debt_plan_versions as versions_service
from app.services.advisor_context import (
    MONTH_STATUS_HEADER,
    NO_CLOSED_MONTHS_SENTENCE,
    build_advisor_context,
    format_uah,
)
from app.services.llm_advisor import (
    NO_DATA_ANSWER,
    SYSTEM_PROMPT,
    _format_context,
    find_banned_words,
    find_rule_violations,
)

_REAL_TODAY = ledger.today_kyiv
MINUS = "−"


@pytest.fixture
def clock(monkeypatch):
    """Керований «сьогодні» для журналу, план-факту й радника."""
    state = {"today": date(2026, 11, 14)}
    monkeypatch.setattr(ledger, "today_kyiv",
                        lambda today=None: _REAL_TODAY(today) if today is not None else state["today"])
    return state


async def _debts(db_session, user, today):
    await create_debt(DebtCreateRequest(name="Картка", balance=10000.0, monthly_rate=0.04, debt_type="revolving"),
                      current_user=user, db=db_session, today=today)
    await create_debt(DebtCreateRequest(name="Розстрочка", balance=6000.0, monthly_rate=0.02,
                                        debt_type="installment", min_payment=500.0),
                      current_user=user, db=db_session, today=today)


async def _accept(db_session, user, extra, today):
    response = await optimize_with_extra(ExtraPaymentRequest(extra_monthly_uah=extra), current_user=user,
                                         db=db_session, months=36, today=today)
    version, created = await versions_service.accept_plan(db_session, user.id, response["plan_id"], today)
    assert created
    return version


async def _planned_total(db_session, version, month) -> int:
    rows = (await db_session.execute(
        select(DebtPlanSchedule).where(DebtPlanSchedule.version_id == version.id, DebtPlanSchedule.month == month)
    )).scalars().all()
    return sum(r.planned_payment_kop for r in rows)


async def _two_versions_november_without_payments(db_session, user, clock):
    """Версія 1 з 14.11, листопад — «платежів не було», версія 2 з 01.12 зафіксована 03.12."""
    await _debts(db_session, user, clock["today"])
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


async def test_closed_month_without_payments_and_version_comparison(db_session, real_user, clock):
    v1, v2 = await _two_versions_november_without_payments(db_session, real_user, clock)
    planned = ledger.kop_to_uah(await _planned_total(db_session, v1, date(2026, 11, 1)))
    context = await build_advisor_context(real_user.id, session=db_session)
    sentences = context["sentences"]
    text = _format_context(context)

    assert MONTH_STATUS_HEADER in sentences
    november = next(s for s in sentences if s.startswith("Листопад 2026 р."))
    assert "внесено 0 грн" in november
    assert f"різниця {MINUS}{format_uah(planned)} грн" in november
    assert f"за планом {format_uah(planned)} грн" in november
    assert f"внесено на {format_uah(planned)} грн менше за план" in november
    assert "позначка «платежів не було»" in november
    assert not any(s.startswith("Грудень 2026 р.") for s in sentences), "грудень ще триває"

    total = next(s for s in sentences if s.startswith("Разом за закриті місяці:"))
    assert f"різниця {MINUS}{format_uah(planned)} грн" in total

    previous = next(s for s in sentences if s.startswith("Попередній план"))
    assert previous.startswith(
        f"Попередній план (діяв з 1 листопада 2026) рахувався від залишків на 14 листопада 2026 р.: "
        f"закриття — {ledger.month_name(_payoff(v1))}, "
        f"переплата {format_uah(ledger.kop_to_uah(v1.total_interest_kop))} грн; "
        f"чинний рахується від залишків на 1 грудня 2026 р.: закриття — {ledger.month_name(_payoff(v2))}, "
        f"переплата {format_uah(ledger.kop_to_uah(v2.total_interest_kop))} грн; у чинному "), previous
    assert find_banned_words(text) == []


async def test_without_previous_version_no_comparison(db_session, real_user, clock):
    await _debts(db_session, real_user, clock["today"])
    await _accept(db_session, real_user, 1000.0, clock["today"])
    context = await build_advisor_context(real_user.id, session=db_session)
    assert not any(s.startswith("Попередній план") for s in context["sentences"])
    assert NO_CLOSED_MONTHS_SENTENCE in context["sentences"]


async def test_entered_payments_month_source(db_session, real_user, clock):
    await _debts(db_session, real_user, clock["today"])
    v1 = await _accept(db_session, real_user, 1000.0, clock["today"])
    planned_kop = await _planned_total(db_session, v1, date(2026, 11, 1))
    debt_id = (await db_session.execute(
        select(DebtPlanSchedule.debt_id).where(DebtPlanSchedule.version_id == v1.id).limit(1))).scalar_one()
    from app.models.debt_plan import DebtPayment
    db_session.add(DebtPayment(id="p-status-1", user_id=real_user.id, debt_id=debt_id, paid_on=date(2026, 11, 20),
                               amount_kop=planned_kop))
    await db_session.commit()
    clock["today"] = date(2026, 12, 10)
    context = await build_advisor_context(real_user.id, session=db_session)
    november = next(s for s in context["sentences"] if s.startswith("Листопад 2026 р."))
    assert f"внесено {format_uah(ledger.kop_to_uah(planned_kop))} грн" in november
    assert "внесено стільки, скільки в плані" in november
    assert "джерело факту — внесені платежі" in november


async def test_period_before_bracket_in_templates(db_session, real_user, clock):
    await _debts(db_session, real_user, clock["today"])
    await _accept(db_session, real_user, 1000.0, clock["today"])
    text = _format_context(await build_advisor_context(real_user.id, session=db_session))
    assert "р (" not in text
    assert "р. (через" in text


async def test_extra_payment_matches_active_plan(db_session, real_user, clock):
    await _two_versions_november_without_payments(db_session, real_user, clock)
    active = await active_plan(current_user=real_user, db=db_session, today=clock["today"])
    effective = active["effective"]
    text = _format_context(await build_advisor_context(real_user.id, session=db_session))
    assert (f"Щомісяця на борги йде {format_uah(effective['total_monthly_payment'])} грн: "
            f"{format_uah(effective['min_payments_total'])} грн обов'язкових платежів і "
            f"{format_uah(effective['extra_monthly'])} грн понад них.") in text
    assert effective["extra_monthly"] == 1500


def test_prompt_restricts_plan_execution_answers():
    assert NO_DATA_ANSWER == "Даних для цієї відповіді немає."
    assert NO_DATA_ANSWER in SYSTEM_PROMPT
    assert MONTH_STATUS_HEADER.rstrip(":") in SYSTEM_PROMPT
    assert "Попередній план" in SYSTEM_PROMPT
    context = {"sentences": ["Щомісяця на борги йде 1 000 грн."]}
    for verdict in ("Так, ти виконуєш план.", "Ти не виконуєш план.", "Усе йде як і планувалось.",
                    "Платежі як і розраховано.", "Змін немає, усе співпадає."):
        assert find_rule_violations(verdict, context, "Чи я виконую план?"), verdict
    assert find_rule_violations("Даних для цієї відповіді немає.", context, "Чи я виконую план?") == []
