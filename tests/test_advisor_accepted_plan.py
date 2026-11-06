"""
Контекст AI-радника на прийнятому плані.

Основа — версія, чинна в поточному місяці: бюджет версії, «цього місяця
платити» з її розкладу на поточний місяць, речення наслідку з debt_outcome;
платежі версію не застарюють, зміна складу боргів — застарює; лише чернетка —
речення «Цей план ще не прийнято.»; версія без розкладу — речення про
фіксацію. Сценарії «якби» рахуються від поточних залишків і порівнюються з
прогоном від тих самих залишків, а не з переплатою версії. «Сьогодні» —
підміна app.services.debt_ledger.today_kyiv; Claude API не викликається.
"""
import logging
from datetime import date

import pytest
from sqlalchemy import select

import app.services.advisor_context as advisor_context
import app.services.debt_ledger as ledger
from app.api.assets import AssetCreateRequest, create_asset
from app.api.debt import DebtCreateRequest, ExtraPaymentRequest, create_debt, optimize_with_extra
from app.models.debt_plan import DebtPayment, DebtPlanSchedule
from app.models.milp_plan import MilpPlan
from app.services import debt_outcome
from app.services import debt_plan_versions as versions_service
from app.services.advisor_context import (
    ACCEPTED_PLAN_NOT_FIXED_SENTENCE,
    ACCEPTED_PLAN_STALE_SENTENCE,
    DRAFT_NOT_ACCEPTED_SENTENCE,
    build_advisor_context,
    format_uah,
)
from app.services.llm_advisor import _NUMBER_RE, _format_context, find_banned_words
from app.services.milp_optimizer import optimize_milp

_REAL_TODAY = ledger.today_kyiv


@pytest.fixture
def clock(monkeypatch):
    """Керований «сьогодні» для журналу, план-факту й радника."""
    state = {"today": date(2026, 11, 14)}
    monkeypatch.setattr(ledger, "today_kyiv",
                        lambda today=None: _REAL_TODAY(today) if today is not None else state["today"])
    return state


async def _debts(db_session, user, today):
    card = await create_debt(DebtCreateRequest(name="Картка", balance=10000.0, monthly_rate=0.04,
                                               debt_type="revolving"), current_user=user, db=db_session, today=today)
    loan = await create_debt(DebtCreateRequest(name="Розстрочка", balance=6000.0, monthly_rate=0.02,
                                               debt_type="installment", min_payment=500.0),
                             current_user=user, db=db_session, today=today)
    return card, loan


async def _draft(db_session, user, extra, today):
    response = await optimize_with_extra(ExtraPaymentRequest(extra_monthly_uah=extra), current_user=user,
                                         db=db_session, months=36, today=today)
    return await db_session.get(MilpPlan, response["plan_id"])


async def _accepted(db_session, user, clock, extra=1000.0):
    card, loan = await _debts(db_session, user, clock["today"])
    plan = await _draft(db_session, user, extra, clock["today"])
    version, created = await versions_service.accept_plan(db_session, user.id, plan.id, clock["today"])
    assert created
    return (card, loan), version


async def _schedule(db_session, version, month):
    rows = (await db_session.execute(
        select(DebtPlanSchedule).where(DebtPlanSchedule.version_id == version.id, DebtPlanSchedule.month == month)
    )).scalars().all()
    return {r.debt_id: r for r in rows}


async def _pay(db_session, user, debt_id, day, uah):
    db_session.add(DebtPayment(id=f"p-{debt_id}-{day}-{uah}", user_id=user.id, debt_id=debt_id, paid_on=day,
                               amount_kop=ledger.uah_to_kop(uah)))
    await db_session.commit()


def _assert_whole_and_clean(text):
    assert find_banned_words(text) == [], text
    for token in _NUMBER_RE.findall(text):
        assert "," not in token, token


async def test_accepted_version_is_the_basis(db_session, real_user, clock):
    (card, loan), version = await _accepted(db_session, real_user, clock)
    rows = await _schedule(db_session, version, date(2026, 11, 1))
    context = await build_advisor_context(real_user.id, session=db_session)
    text = _format_context(context)

    assert context["plan_kind"] == "accepted" and context["plan_available"] is True
    assert DRAFT_NOT_ACCEPTED_SENTENCE not in context["sentences"]
    for debt in (card, loan):
        payment = ledger.kop_to_uah(rows[debt.id].planned_payment_kop)
        assert f"{debt.name}: залишилось {format_uah(debt.balance)} грн, цього місяця платити {format_uah(payment)} грн" \
            in text
    payoff = ledger.add_months(version.effective_from, version.months_to_payoff - 1)
    assert (f"За прийнятим планом усі борги закриються {debt_outcome.month_in_text(payoff)} "
            f"(через {version.months_to_payoff} міс. від цього місяця), загальна переплата за планом "
            f"{format_uah(ledger.kop_to_uah(version.total_interest_kop))} грн.") in context["sentences"]
    outcome = await debt_outcome.plan_outcome(db_session, real_user.id, clock["today"])
    assert outcome["sentence"] in context["sentences"]
    assert outcome["sentence"].endswith("як і в чинному плані; переплата така сама.")
    _assert_whole_and_clean(text)


async def test_payments_do_not_make_accepted_plan_stale_and_newer_draft_is_ignored(db_session, real_user, clock):
    (card, loan), version = await _accepted(db_session, real_user, clock)
    await _pay(db_session, real_user, card.id, date(2026, 11, 14), 3000.0)
    await _draft(db_session, real_user, 4000.0, clock["today"])  # нова чернетка, не прийнята

    context = await build_advisor_context(real_user.id, session=db_session)
    assert context["plan_kind"] == "accepted"
    assert ACCEPTED_PLAN_STALE_SENTENCE not in context["sentences"]
    text = _format_context(context)
    assert "Картка: залишилось 7 000 грн" in text
    assert "1 000 грн понад них" in text and "4 000 грн понад них" not in text


async def test_paid_off_debt_from_schedule_keeps_plan_fresh(db_session, real_user, clock):
    (card, loan), _ = await _accepted(db_session, real_user, clock)
    await _pay(db_session, real_user, loan.id, date(2026, 11, 14), 6000.0)
    context = await build_advisor_context(real_user.id, session=db_session)
    assert context["plan_kind"] == "accepted"
    assert "Розстрочка: залишилось 0 грн" in _format_context(context)


async def test_structure_change_makes_accepted_plan_stale(db_session, real_user, clock):
    await _accepted(db_session, real_user, clock)
    await create_debt(DebtCreateRequest(name="Позика", balance=2000.0, monthly_rate=0.05, debt_type="revolving"),
                      current_user=real_user, db=db_session, today=clock["today"])
    context = await build_advisor_context(real_user.id, session=db_session)
    assert context["sentences"] == [ACCEPTED_PLAN_STALE_SENTENCE]
    assert context["plan_available"] is False


async def test_only_draft_says_not_accepted(db_session, real_user, clock):
    await _debts(db_session, real_user, clock["today"])
    await _draft(db_session, real_user, 1000.0, clock["today"])
    context = await build_advisor_context(real_user.id, session=db_session)
    assert context["plan_kind"] == "draft"
    assert context["sentences"][0] == DRAFT_NOT_ACCEPTED_SENTENCE == "Цей план ще не прийнято."


async def test_this_month_payment_comes_from_current_month_of_schedule(db_session, real_user, clock):
    (card, loan), version = await _accepted(db_session, real_user, clock)
    nov = await _schedule(db_session, version, date(2026, 11, 1))
    for debt in (card, loan):
        await _pay(db_session, real_user, debt.id, date(2026, 11, 14), ledger.kop_to_uah(nov[debt.id].planned_payment_kop))
    clock["today"] = date(2026, 12, 10)
    dec = await _schedule(db_session, version, date(2026, 12, 1))
    context = await build_advisor_context(real_user.id, session=db_session)
    text = _format_context(context)
    for debt in (card, loan):
        closing = ledger.kop_to_uah(nov[debt.id].closing_kop)
        payment = ledger.kop_to_uah(dec[debt.id].planned_payment_kop)
        assert f"{debt.name}: залишилось {format_uah(closing)} грн, цього місяця платити {format_uah(payment)} грн" \
            in text
    assert f"(через {version.months_to_payoff - 1} міс. від цього місяця)" in text


async def test_in_force_version_without_schedule(db_session, real_user, clock):
    await _accepted(db_session, real_user, clock)
    clock["today"] = date(2026, 11, 20)
    plan = await _draft(db_session, real_user, 2000.0, clock["today"])
    v2, created = await versions_service.accept_plan(db_session, real_user.id, plan.id, clock["today"])
    assert created and v2.effective_from == date(2026, 12, 1)
    clock["today"] = date(2026, 12, 3)
    context = await build_advisor_context(real_user.id, session=db_session)
    assert context["sentences"][:2] == [
        ACCEPTED_PLAN_NOT_FIXED_SENTENCE,
        "Щоб порахувати наслідок, внеси платежі за листопад 2026 р. або познач, що їх не було.",
    ]
    assert context["sentences"][2] == advisor_context.MONTH_STATUS_HEADER
    assert context["sentences"][3].startswith("Листопад 2026 р. (план №1): за планом ")
    assert context["sentences"][3].endswith("платежів не внесено, внесена сума невідома.")
    assert len(context["sentences"]) == 4, "без підсумку: жодного місяця з відомою внесеною сумою"


async def test_extra_payment_what_if_compares_runs_from_same_balances(db_session, real_user, clock):
    (card, loan), version = await _accepted(db_session, real_user, clock)
    await _pay(db_session, real_user, card.id, date(2026, 11, 14), 2500.0)
    context = await build_advisor_context(real_user.id, session=db_session, question="А якби я платив ще 2000?")
    text = _format_context(context)

    rows = await _schedule(db_session, version, date(2026, 11, 1))
    debts = await ledger.active_debt_states(db_session, real_user.id, clock["today"])
    min_total = versions_service.schedule_min_payments_total(list(rows.values()), {card.id: card, loan.id: loan})
    dicts = advisor_context._debt_dicts(debts)
    base = optimize_milp(dicts, min_total + 1000.0, 36)
    scenario = optimize_milp(dicts, min_total + 3000.0, 36)
    diff = advisor_context._whole(base["total_interest_paid"]) - advisor_context._whole(scenario["total_interest_paid"])

    assert (f"Зараз додатковий платіж 1 000 грн: від нинішніх залишків борги закриються через "
            f"{base['months_to_payoff']} міс., переплата від цього місяця {format_uah(base['total_interest_paid'])} грн.") \
        in context["sentences"]
    assert "(разом 3 000 грн понад обов'язкові)" in text
    assert f"переплата {format_uah(scenario['total_interest_paid'])} грн, тобто на {format_uah(diff)} грн менше" in text
    _assert_whole_and_clean(text)


async def test_savings_what_if_tolerance_uses_run_from_same_balances(db_session, real_user, clock, caplog):
    (card, _), version = await _accepted(db_session, real_user, clock)
    await _pay(db_session, real_user, card.id, date(2026, 11, 14), 2500.0)
    await create_asset(AssetCreateRequest(name="Депозит", asset_type="Депозит", current_amount=3000,
                                          liquidity_level="high"), current_user=real_user, db=db_session)
    with caplog.at_level(logging.WARNING, logger="app.services.advisor_context"):
        context = await build_advisor_context(real_user.id, session=db_session,
                                              question="Чи варто закрити борг заощадженнями?")
    assert context["savings_scenario"] is True
    assert any(s.startswith("Якби одноразово спрямувати 3 000 грн заощаджень") for s in context["sentences"])
    assert "what-if за заощадженнями не показано" not in caplog.text

    # Переплата версії вже не збігається з прогоном від поточних залишків: звірка з нею приховала б сценарій.
    debts = await ledger.active_debt_states(db_session, real_user.id, clock["today"])
    rows = await _schedule(db_session, version, date(2026, 11, 1))
    min_total = versions_service.schedule_min_payments_total(list(rows.values()), {d.id: d for d in debts})
    baseline = optimize_milp(advisor_context._debt_dicts(debts), min_total + 1000.0, 36)
    assert abs(baseline["total_interest_paid"] - ledger.kop_to_uah(version.total_interest_kop)) \
        > advisor_context.WHAT_IF_PLAN_TOLERANCE_UAH
