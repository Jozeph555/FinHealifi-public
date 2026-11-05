"""
Тести план-факту боргів: прийняття версій, знімок, платежі, звірки, блокування місяців.

- чисті функції: half-up у копійках, roll_month, межа місяця за Europe/Kyiv,
  effective_to версії, що не діяла;
- HTTP-сценарії: перший план посеред місяця, платіж до дати прийняття,
  збіг плану й факту до копійки, другий план 20.11 і його матеріалізація,
  третій план до 1.12, місяць без платежів, звірка, редагування закритого
  місяця, PATCH залишку як звірка; cumulative лише по закритих місяцях і
  remaining_to_plan (max(0, план − факт)) для відкритого місяця.

«Сьогодні» задається підміною залежності get_today. HTTP — через httpx
ASGITransport на мінімальному FastAPI з роутерами боргів.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

import app.api.debt as debt_module
import app.api.debt_plan_fact as plan_fact_module
from app.api.deps import get_current_user, get_db, get_today
from app.models.debt_plan import DebtBalanceCheck, DebtPlanVersion
from app.services import debt_ledger as ledger
from app.services.debt_plan_versions import version_acted, version_covers


# --- чисті функції --------------------------------------------------------------------

def test_uah_to_kop_half_up():
    assert ledger.uah_to_kop(1234.56) == 123456
    assert ledger.uah_to_kop(1.005) == 101  # float 1.005 < 1.005, але Decimal(str(x)) дає рівно 1.005
    assert ledger.uah_to_kop(0.125) == 13
    assert ledger.uah_to_kop(Decimal("2.345")) == 235
    assert ledger.uah_to_kop(10_000) == 1_000_000
    assert ledger.kop_to_uah(123456) == 1234.56


def test_interest_half_up():
    assert ledger.interest_kop(3, 0.5) == 2  # 1.5 -> 2
    assert ledger.interest_kop(1, 0.5) == 1
    assert ledger.interest_kop(10_000, 0.03333) == 333  # 333.3
    assert ledger.interest_kop(15, 0.1) == 2  # 1.5 -> 2


def test_roll_month_caps_payment_and_reports_overpay():
    roll = ledger.roll_month(100_000, 0.02, 150_000)
    assert (roll.interest, roll.paid, roll.closing, roll.overpaid) == (2_000, 102_000, 0, 48_000)
    assert ledger.roll_month(0, 0.02, 0).closing == 0


def test_plan_and_fact_series_use_the_same_formula():
    """Ряд від стартової звірки плану з тими самими платежами дає ті самі залишки, що й roll_month поспіль."""
    rate = 0.0347
    opening = 1_234_567
    payments = [104_321, 99_999, 250_000, 1]
    expected, balance = [], opening
    for pay in payments:
        roll = ledger.roll_month(balance, rate, pay)
        expected.append(roll.closing)
        balance = roll.closing

    anchor = ledger.Anchor(date(2026, 11, 14), opening, ledger.PLAN_ANCHOR_SOURCE)
    pays = [(date(2026, 11, 14), payments[0]), (date(2026, 12, 3), payments[1]),
            (date(2027, 1, 31), payments[2]), (date(2027, 2, 1), payments[3])]
    series = ledger.debt_series([anchor], pays, rate, date(2026, 11, 1), date(2027, 2, 28))

    assert [f.closing for f in series] == expected


def test_month_boundary_is_kyiv():
    late_kyiv = datetime(2026, 11, 30, 21, 30, tzinfo=timezone.utc)  # 30.11 23:30 за Києвом
    after_midnight = datetime(2026, 11, 30, 22, 30, tzinfo=timezone.utc)  # 01.12 00:30 за Києвом

    assert ledger.today_kyiv(late_kyiv) == date(2026, 11, 30)
    assert ledger.month_start(ledger.today_kyiv(late_kyiv)) == date(2026, 11, 1)
    assert ledger.today_kyiv(after_midnight) == date(2026, 12, 1)


def test_version_that_never_acted_is_not_in_force():
    never = DebtPlanVersion(effective_from=date(2026, 12, 1), effective_to=date(2026, 12, 1), materialized_at=None)
    acted = DebtPlanVersion(effective_from=date(2026, 11, 1), effective_to=date(2026, 12, 1), materialized_at=None)
    current = DebtPlanVersion(effective_from=date(2026, 12, 1), effective_to=None, materialized_at=None)

    assert not version_acted(never)
    assert not version_covers(never, date(2026, 12, 1))
    assert version_covers(acted, date(2026, 11, 1)) and not version_covers(acted, date(2026, 12, 1))
    assert version_covers(current, date(2027, 3, 1))


# --- HTTP ----------------------------------------------------------------------------------

@pytest_asyncio.fixture
async def clock():
    return {"today": date(2026, 11, 14)}


@pytest_asyncio.fixture
async def client(db_session, real_user, clock):
    app = FastAPI()
    app.include_router(debt_module.router)
    app.include_router(plan_fact_module.router)

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: real_user
    app.dependency_overrides[get_today] = lambda: clock["today"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


INSTALLMENT = {"name": "Розстрочка", "balance": 10_000, "monthly_rate": 0.02, "min_payment": 1_000,
               "debt_type": "installment"}
CARD = {"name": "Картка", "balance": 7_345.67, "monthly_rate": 0.0347, "min_payment": None, "debt_type": "revolving"}


async def _debt(client, **body):
    resp = await client.post("/debt", json={**INSTALLMENT, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _draft(client, extra=1_000):
    resp = await client.post("/debt/optimize", json={"extra_monthly_uah": extra})
    assert resp.status_code == 201, resp.text
    assert resp.json()["plan_state"] == "draft"
    return resp.json()["plan_id"]


async def _accept(client, plan_id, expected=201):
    resp = await client.post(f"/debt/plans/{plan_id}/accept")
    assert resp.status_code == expected, resp.text
    return resp.json()


async def _pay(client, debt_id, day, amount, expected=201):
    resp = await client.post("/debt/payments", json={"debt_id": debt_id, "paid_on": day.isoformat(),
                                                     "amount": amount})
    assert resp.status_code == expected, resp.text
    return resp.json()


async def _plan_fact(client, **params):
    resp = await client.get("/debt/plan-fact", params=params)
    assert resp.status_code == 200, resp.text
    return {m["month"]: m for m in resp.json()["months"]}


def _row(month, debt_id):
    return next(r for r in month["debts"] if r["debt_id"] == debt_id)


def _planned(version, month, debt_id):
    m = next(m for m in version["schedule"] if m["month"] == month)
    return next(d for d in m["debts"] if d["debt_id"] == debt_id)


async def test_first_plan_mid_month_and_payment_before_acceptance(client, clock):
    debt = await _debt(client)
    version = await _accept(client, await _draft(client))

    assert version["effective_from"] == "2026-11-01" and version["accepted_on"] == "2026-11-14"
    assert version["materialized"] is True and version["status"] == "accepted"
    november = _planned(version, "2026-11", debt["id"])
    assert november["opening"] == 10_000 and november["interest"] == 200

    resp = await client.post("/debt/payments", json={"debt_id": debt["id"], "paid_on": "2026-11-10", "amount": 500})
    assert resp.status_code == 409
    assert "14.11.2026" in resp.json()["detail"]

    await _pay(client, debt["id"], date(2026, 11, 14), november["payment"])
    nov = (await _plan_fact(client))["2026-11"]
    assert nov["counted_from"] == "2026-11-14" and nov["entry_state"] == "entered"
    row = _row(nov, debt["id"])
    assert row["payment_diff"] == 0 and row["balance_diff"] == 0
    assert row["actual_closing"] == november["closing"]


async def test_payment_without_plan_is_rejected(client):
    debt = await _debt(client)
    resp = await client.post("/debt/payments", json={"debt_id": debt["id"], "paid_on": "2026-11-14", "amount": 100})
    assert resp.status_code == 409


async def test_plan_and_fact_match_to_the_kopeck(client, clock):
    """Платежі рівно за планом щомісяця дають нульові розбіжності й нульовий залишок у кінці."""
    a = await _debt(client)
    b = await _debt(client, **CARD)
    version = await _accept(client, await _draft(client, extra=1_234.56))
    months = [m["month"] for m in version["schedule"]]
    assert len(months) >= 3

    for month in months:
        day = ledger.parse_month(month).replace(day=15)
        clock["today"] = max(day, date(2026, 11, 14))
        m = next(x for x in version["schedule"] if x["month"] == month)
        for d in m["debts"]:
            if d["payment"] > 0:
                await _pay(client, d["debt_id"], clock["today"], d["payment"])

    fact = await _plan_fact(client)
    for month in months:
        for d in fact[month]["debts"]:
            if d["planned_payment"] is not None:
                assert d["payment_diff"] == 0, (month, d)
                assert d["balance_diff"] == 0, (month, d)
    last = fact[months[-1]]
    assert last["totals"]["actual_closing"] == 0
    debts = {d["id"]: d for d in (await client.get("/debt")).json()}
    assert debts[a["id"]]["balance"] == 0 and debts[b["id"]]["balance"] == 0


async def test_second_plan_on_20th_starts_next_month_from_actual_balance(client, clock):
    debt = await _debt(client)
    v1 = await _accept(client, await _draft(client))
    planned_nov = _planned(v1, "2026-11", debt["id"])
    await _pay(client, debt["id"], date(2026, 11, 14), 1_500)

    clock["today"] = date(2026, 11, 20)
    v2 = await _accept(client, await _draft(client, extra=2_000))
    assert v2["effective_from"] == "2026-12-01" and v2["materialized"] is False and v2["schedule"] == []
    assert v2["in_force_version_id"] == v1["version_id"]
    nov = (await _plan_fact(client))["2026-11"]
    assert nov["version_id"] == v1["version_id"]
    assert _row(nov, debt["id"])["planned_payment"] == planned_nov["payment"]

    clock["today"] = date(2026, 12, 3)
    assert (await client.post("/debt/plan/materialize")).status_code == 409
    await _pay(client, debt["id"], date(2026, 11, 28), 100)  # попередній місяць ще редагується
    resp = await client.post("/debt/plan/materialize", params={"force": True})
    assert resp.status_code == 201, resp.text
    v2 = resp.json()
    assert v2["materialized"] is True

    expected_nov_closing = ledger.roll_month(1_000_000, 0.02, 160_000).closing
    assert _planned(v2, "2026-12", debt["id"])["opening"] == ledger.kop_to_uah(expected_nov_closing)
    fact = await _plan_fact(client)
    assert _row(fact["2026-11"], debt["id"])["actual_closing"] == ledger.kop_to_uah(expected_nov_closing)
    assert fact["2026-12"]["version_id"] == v2["version_id"]
    assert fact["2026-11"]["editable"] is False

    assert (await client.post("/debt/plan/materialize")).status_code == 200  # ідемпотентно
    resp = await client.post("/debt/payments", json={"debt_id": debt["id"], "paid_on": "2026-11-29", "amount": 1})
    assert resp.status_code == 409


async def test_materialize_requires_previous_month_resolved(client, clock):
    await _debt(client)
    await _accept(client, await _draft(client))
    clock["today"] = date(2026, 11, 20)
    await _accept(client, await _draft(client, extra=1_500))

    clock["today"] = date(2026, 12, 7)
    resp = await client.post("/debt/plan/materialize")
    assert resp.status_code == 409
    assert "платежів не було" in resp.json()["detail"]

    assert (await client.put("/debt/plan-fact/months/2026-11/no-payments", json={"confirmed": True})).status_code == 200
    assert (await client.post("/debt/plan/materialize")).status_code == 201


async def test_materialize_before_effective_month_is_rejected(client, clock):
    await _debt(client)
    await _accept(client, await _draft(client))
    clock["today"] = date(2026, 11, 20)
    await _accept(client, await _draft(client, extra=1_500))
    assert (await client.post("/debt/plan/materialize", params={"force": True})).status_code == 409


async def test_third_plan_before_first_of_month_replaces_second(client, db_session, real_user, clock):
    debt = await _debt(client)
    v1 = await _accept(client, await _draft(client))
    await _pay(client, debt["id"], date(2026, 11, 14), 2_000)
    clock["today"] = date(2026, 11, 20)
    v2 = await _accept(client, await _draft(client, extra=1_500))
    clock["today"] = date(2026, 11, 25)
    v3 = await _accept(client, await _draft(client, extra=3_000))

    versions = {v.id: v for v in (await db_session.execute(
        select(DebtPlanVersion).where(DebtPlanVersion.user_id == real_user.id)
    )).scalars().all()}
    assert versions[v1["version_id"]].effective_to == date(2026, 12, 1)
    assert versions[v2["version_id"]].status == "superseded"
    assert versions[v2["version_id"]].effective_to == versions[v2["version_id"]].effective_from
    assert versions[v3["version_id"]].status == "accepted"

    clock["today"] = date(2026, 12, 6)
    assert (await client.post("/debt/plan/materialize")).status_code == 201
    fact = await _plan_fact(client)
    assert fact["2026-12"]["version_id"] == v3["version_id"]
    assert fact["2026-11"]["version_id"] == v1["version_id"]


async def test_same_draft_accepted_twice_is_idempotent(client):
    await _debt(client)
    plan_id = await _draft(client)
    first = await _accept(client, plan_id)
    again = await _accept(client, plan_id, expected=200)
    assert again["version_id"] == first["version_id"]


async def test_stale_draft_cannot_be_accepted(client):
    debt = await _debt(client)
    plan_id = await _draft(client)
    assert (await client.patch(f"/debt/{debt['id']}", json={"balance": 9_000})).status_code == 200
    resp = await client.post(f"/debt/plans/{plan_id}/accept")
    assert resp.status_code == 409


async def test_month_without_payments_is_not_a_divergence(client, clock):
    debt = await _debt(client)
    await _accept(client, await _draft(client))
    clock["today"] = date(2026, 12, 10)

    fact = await _plan_fact(client)
    nov = _row(fact["2026-11"], debt["id"])
    assert fact["2026-11"]["entry_state"] == "not_entered" and fact["2026-11"]["closed"] is True
    assert nov["actual_payment"] is None and nov["payment_diff"] is None and nov["balance_diff"] is None
    assert nov["actual_closing"] is None
    assert _row(fact["2026-12"], debt["id"])["actual_closing"] is None

    assert (await client.put("/debt/plan-fact/months/2026-11/no-payments", json={"confirmed": True})).status_code == 200
    fact = await _plan_fact(client)
    nov = _row(fact["2026-11"], debt["id"])
    assert fact["2026-11"]["entry_state"] == "no_payments_confirmed"
    assert nov["actual_payment"] == 0 and nov["payment_diff"] == -nov["planned_payment"]
    assert nov["actual_closing"] == 10_200


async def test_no_payments_mark_rejected_when_month_has_payments(client):
    debt = await _debt(client)
    await _accept(client, await _draft(client))
    await _pay(client, debt["id"], date(2026, 11, 14), 100)
    resp = await client.put("/debt/plan-fact/months/2026-11/no-payments", json={"confirmed": True})
    assert resp.status_code == 409


async def test_balance_check_resets_computed_balance(client, clock):
    debt = await _debt(client)
    await _accept(client, await _draft(client))
    await _pay(client, debt["id"], date(2026, 11, 14), 2_000)

    clock["today"] = date(2026, 12, 20)
    resp = await client.post(f"/debt/{debt['id']}/balance-checks", json={"as_of": "2026-12-15", "balance": 7_000})
    assert resp.status_code == 201, resp.text
    await _pay(client, debt["id"], date(2026, 12, 15), 300)  # того ж дня — до звірки, не віднімається
    await _pay(client, debt["id"], date(2026, 12, 20), 500)

    fact = await _plan_fact(client)
    dec = _row(fact["2026-12"], debt["id"])
    assert dec["actual_closing"] == 6_500
    assert dec["check"]["as_of"] == "2026-12-15" and dec["check"]["balance"] == 7_000
    debts = {d["id"]: d for d in (await client.get("/debt")).json()}
    assert debts[debt["id"]]["balance"] == 6_500

    clock["today"] = date(2027, 1, 10)
    await _pay(client, debt["id"], date(2027, 1, 5), 1_000)
    jan = _row((await _plan_fact(client))["2027-01"], debt["id"])
    assert jan["actual_closing"] == 6_500 + 130 - 1_000

    resp = await client.post(f"/debt/{debt['id']}/balance-checks", json={"as_of": "2026-12-15", "balance": 6_900})
    assert resp.status_code == 200  # та сама дата — заміна


async def test_editing_closed_month_within_current_version_recalculates(client, clock):
    debt = await _debt(client)
    await _accept(client, await _draft(client))
    payment = await _pay(client, debt["id"], date(2026, 11, 14), 1_000)

    clock["today"] = date(2026, 12, 10)
    resp = await client.patch(f"/debt/payments/{payment['id']}", json={"amount": 1_500})
    assert resp.status_code == 200, resp.text
    nov = _row((await _plan_fact(client))["2026-11"], debt["id"])
    assert nov["actual_payment"] == 1_500
    assert nov["actual_closing"] == 10_200 - 1_500

    assert (await client.delete(f"/debt/payments/{payment['id']}")).status_code == 204


async def test_future_payment_and_bad_amount_rejected(client):
    debt = await _debt(client)
    await _accept(client, await _draft(client))
    assert (await client.post("/debt/payments", json={"debt_id": debt["id"], "paid_on": "2026-11-15",
                                                      "amount": 100})).status_code == 422
    assert (await client.post("/debt/payments", json={"debt_id": debt["id"], "paid_on": "2026-11-14",
                                                      "amount": 0})).status_code == 422


async def test_patch_balance_creates_manual_edit_check(client, db_session):
    debt = await _debt(client)
    resp = await client.patch(f"/debt/{debt['id']}", json={"balance": 8_765.43})
    assert resp.status_code == 200 and resp.json()["balance"] == 8_765.43

    checks = (await db_session.execute(
        select(DebtBalanceCheck).where(DebtBalanceCheck.debt_id == debt["id"])
    )).scalars().all()
    assert [(c.source, c.balance_kop) for c in checks] == [("manual_edit", 876_543)]
    from app.models.debt import Debt
    assert (await db_session.get(Debt, debt["id"])).balance == 10_000


async def test_active_plan_reports_structure_change(client):
    await _debt(client)
    await _accept(client, await _draft(client))
    assert (await client.get("/debt/plan/active")).json()["structure_changed"] is False

    await _debt(client, name="Новий кредит")
    body = (await client.get("/debt/plan/active")).json()
    assert body["structure_changed"] is True
    nov = (await _plan_fact(client))["2026-11"]
    new_row = next(r for r in nov["debts"] if r["name"] == "Новий кредит")
    assert new_row["planned_payment"] is None


async def test_active_plan_404_without_acceptance(client):
    assert (await client.get("/debt/plan/active")).status_code == 404
    assert (await client.get("/debt/plan-fact")).status_code == 404


# --- накопичена розбіжність і залишок до плану в поточному місяці ---------------------------

async def _plan_fact_body(client):
    resp = await client.get("/debt/plan-fact")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_open_month_is_not_in_cumulative(client, clock):
    clock["today"] = date(2026, 11, 4)
    debt = await _debt(client)
    version = await _accept(client, await _draft(client))
    planned = _planned(version, "2026-11", debt["id"])["payment"]
    await _pay(client, debt["id"], date(2026, 11, 4), 300)

    body = await _plan_fact_body(client)
    nov = body["months"][0]
    assert nov["month"] == "2026-11" and nov["closed"] is False
    assert _row(nov, debt["id"])["payment_diff"] == round(300 - planned, 2)
    assert body["cumulative"]["payment_diff"] == 0
    assert body["cumulative"]["balance_diff"] is None
    assert body["cumulative"]["debts"] == []
    assert _row(nov, debt["id"])["remaining_to_plan"] == round(planned - 300, 2)
    assert nov["totals"]["remaining_to_plan"] == round(planned - 300, 2)


async def test_closed_month_is_in_cumulative(client, clock):
    clock["today"] = date(2026, 11, 4)
    debt = await _debt(client)
    version = await _accept(client, await _draft(client))
    planned_nov = _planned(version, "2026-11", debt["id"])
    await _pay(client, debt["id"], date(2026, 11, 4), 300)

    clock["today"] = date(2026, 12, 10)
    body = await _plan_fact_body(client)
    nov, dec = body["months"]
    assert nov["closed"] is True and dec["closed"] is False
    assert body["cumulative"]["payment_diff"] == round(300 - planned_nov["payment"], 2)
    assert body["cumulative"]["balance_diff"] == _row(nov, debt["id"])["balance_diff"]
    assert _row(nov, debt["id"])["remaining_to_plan"] is None
    assert nov["totals"]["remaining_to_plan"] is None
    # Грудень без платежів: лишилося внести весь плановий платіж, у cumulative його немає.
    dec_row = _row(dec, debt["id"])
    assert dec_row["remaining_to_plan"] == dec_row["planned_payment"]


async def test_remaining_to_plan_is_never_negative(client, clock):
    clock["today"] = date(2026, 11, 4)
    debt = await _debt(client)
    version = await _accept(client, await _draft(client))
    planned = _planned(version, "2026-11", debt["id"])["payment"]
    await _pay(client, debt["id"], date(2026, 11, 4), planned + 500)

    nov = (await _plan_fact_body(client))["months"][0]
    assert _row(nov, debt["id"])["payment_diff"] == 500
    assert _row(nov, debt["id"])["remaining_to_plan"] == 0
    assert nov["totals"]["remaining_to_plan"] == 0


# --- «Разом на місяць» у збереженій чернетці й прийнятій версії ------------------------------

async def test_saved_draft_and_active_plan_report_monthly_total(client):
    debt = await _debt(client)
    await _debt(client, **CARD)
    draft = await client.post("/debt/optimize", json={"extra_monthly_uah": 1_000})
    built = draft.json()

    latest = (await client.get("/debt/plan/latest")).json()
    assert latest["min_payments_total_uah"] == built["min_payments_total_uah"]
    assert latest["total_monthly_payment_uah"] == built["total_monthly_payment_uah"]

    version = await _accept(client, built["plan_id"])
    assert version["extra_monthly"] == 1_000
    assert version["min_payments_total"] == round(built["min_payments_total_uah"], 2)
    assert version["total_monthly_payment"] == round(built["total_monthly_payment_uah"], 2)

    assert (await client.patch(f"/debt/{debt['id']}", json={"balance": 9_000})).status_code == 200
    latest = (await client.get("/debt/plan/latest")).json()
    assert latest["balances_changed"] is True and latest["structure_changed"] is False
    assert latest["min_payments_total_uah"] is None and latest["total_monthly_payment_uah"] is None


async def test_unmaterialized_version_has_no_monthly_total(client, clock):
    await _debt(client)
    await _accept(client, await _draft(client))
    clock["today"] = date(2026, 11, 20)
    v2 = await _accept(client, await _draft(client, extra=2_000))
    assert v2["materialized"] is False
    assert v2["extra_monthly"] == 2_000
    assert v2["min_payments_total"] is None and v2["total_monthly_payment"] is None


# --- зміни після побудови чернетки й після прийняття ------------------------------------------

async def test_draft_flags_follow_the_accept_rule(client):
    """Перший план — лише без змін; наступні — якщо не змінився склад, навіть зі зміненими залишками."""
    debt = await _debt(client)
    first = await _draft(client)
    assert (await client.patch(f"/debt/{debt['id']}", json={"balance": 9_500})).status_code == 200
    latest = (await client.get("/debt/plan/latest")).json()
    assert (latest["balances_changed"], latest["structure_changed"]) == (True, False)
    assert (await client.post(f"/debt/plans/{first}/accept")).status_code == 409

    await _accept(client, await _draft(client))
    second = await _draft(client, extra=2_000)
    assert (await client.patch(f"/debt/{debt['id']}", json={"balance": 9_000})).status_code == 200
    latest = (await client.get("/debt/plan/latest")).json()
    assert (latest["balances_changed"], latest["structure_changed"]) == (True, False)
    await _accept(client, second)

    third = await _draft(client, extra=3_000)
    await _debt(client, name="Новий борг")
    latest = (await client.get("/debt/plan/latest")).json()
    assert (latest["balances_changed"], latest["structure_changed"]) == (False, True)
    assert (await client.post(f"/debt/plans/{third}/accept")).status_code == 409


async def test_active_returns_effective_and_pending_versions(client, clock):
    debt = await _debt(client)
    v1 = await _accept(client, await _draft(client))
    body = (await client.get("/debt/plan/active")).json()
    assert body["effective"]["version_id"] == v1["version_id"] and body["pending"] is None

    clock["today"] = date(2026, 11, 20)
    v2 = await _accept(client, await _draft(client, extra=2_000))
    body = (await client.get("/debt/plan/active")).json()
    assert body["version_id"] == v2["version_id"]
    assert body["pending"]["version_id"] == v2["version_id"] and body["pending"]["extra_monthly"] == 2_000
    assert body["pending"]["structure_changed"] is False
    effective = body["effective"]
    assert effective["version_id"] == v1["version_id"] and effective["status"] == "superseded"
    assert effective["total_monthly_payment"] == v1["total_monthly_payment"]
    assert [m["month"] for m in effective["schedule"]][0] == "2026-11"
    assert _planned(effective, "2026-11", debt["id"])["payment"] > 0


async def test_pending_version_reports_structure_change_after_new_debt(client, clock):
    await _debt(client)
    await _accept(client, await _draft(client))
    clock["today"] = date(2026, 11, 20)
    await _accept(client, await _draft(client, extra=2_000))
    await _debt(client, name="Новий борг", balance=3_000, min_payment=300)

    body = (await client.get("/debt/plan/active")).json()
    assert body["materialized"] is False
    assert body["structure_changed"] is True
    assert body["pending"]["structure_changed"] is True
    assert body["effective"]["structure_changed"] is True


async def test_materialize_takes_debts_and_rates_as_they_are_at_materialization(client, clock):
    """Поточна поведінка: борг, доданий між прийняттям і вступом у дію, і нова ставка потрапляють у знімок без окремого підтвердження."""
    debt = await _debt(client)
    await _accept(client, await _draft(client))
    await _pay(client, debt["id"], date(2026, 11, 14), 1_500)
    clock["today"] = date(2026, 11, 20)
    await _accept(client, await _draft(client, extra=2_000))

    clock["today"] = date(2026, 11, 25)
    new = await _debt(client, name="Новий борг", balance=3_000, min_payment=300)
    assert (await client.patch(f"/debt/{debt['id']}", json={"monthly_rate": 0.03})).status_code == 200
    assert (await client.get("/debt/plan/active")).json()["pending"]["structure_changed"] is True

    clock["today"] = date(2026, 12, 7)
    resp = await client.post("/debt/plan/materialize")
    assert resp.status_code == 201, resp.text
    v2 = resp.json()
    dec = {d["debt_id"]: d for d in next(m for m in v2["schedule"] if m["month"] == "2026-12")["debts"]}
    assert set(dec) == {debt["id"], new["id"]}
    assert dec[debt["id"]]["interest"] == round(dec[debt["id"]]["opening"] * 0.03, 2)
    assert v2["min_payments_total"] == 1_000 + 300
    assert v2["total_monthly_payment"] == 1_000 + 300 + 2_000
    assert v2["structure_changed"] is False


async def test_active_plan_lists_debt_added_between_acceptance_and_materialization(client, clock):
    debt = await _debt(client)
    v1 = await _accept(client, await _draft(client))
    assert v1["added_debts"] == [] and v1["removed_debts"] == []
    await _pay(client, debt["id"], date(2026, 11, 14), 1_500)
    clock["today"] = date(2026, 11, 20)
    await _accept(client, await _draft(client, extra=2_000))

    clock["today"] = date(2026, 11, 25)
    new = await _debt(client, name="Новий борг", balance=3_000, min_payment=300)
    pending = (await client.get("/debt/plan/active")).json()["pending"]
    assert pending["added_debts"] is None and pending["removed_debts"] is None

    clock["today"] = date(2026, 12, 7)
    assert (await client.post("/debt/plan/materialize")).status_code == 201
    body = (await client.get("/debt/plan/active")).json()
    assert body["added_debts"] == [{"debt_id": new["id"], "name": "Новий борг"}]
    assert body["removed_debts"] == []
    assert body["effective"]["added_debts"] == body["added_debts"]


async def test_active_plan_lists_debt_removed_between_acceptance_and_materialization(client, clock):
    debt = await _debt(client)
    card = await _debt(client, **CARD)
    await _accept(client, await _draft(client))
    await _pay(client, debt["id"], date(2026, 11, 14), 1_500)
    clock["today"] = date(2026, 11, 20)
    await _accept(client, await _draft(client, extra=2_000))

    clock["today"] = date(2026, 11, 25)
    assert (await client.patch(f"/debt/{card['id']}", json={"is_active": False})).status_code == 200

    clock["today"] = date(2026, 12, 7)
    assert (await client.post("/debt/plan/materialize")).status_code == 201
    body = (await client.get("/debt/plan/active")).json()
    assert body["added_debts"] == []
    assert body["removed_debts"] == [{"debt_id": card["id"], "name": "Картка"}]


async def test_active_plan_debt_diff_unknown_for_draft_without_start_balances(client, db_session):
    from app.models.milp_plan import MilpPlan

    await _debt(client)
    plan_id = await _draft(client)
    plan = await db_session.get(MilpPlan, plan_id)
    plan.plan_data = {**plan.plan_data, "milp": {k: v for k, v in plan.plan_data["milp"].items()
                                                 if k != "start_balances"}}
    await db_session.flush()
    await _accept(client, plan_id)
    body = (await client.get("/debt/plan/active")).json()
    assert body["added_debts"] is None and body["removed_debts"] is None


async def test_no_payments_mark_can_be_removed(client, clock):
    await _debt(client)
    await _accept(client, await _draft(client))
    url = "/debt/plan-fact/months/2026-11/no-payments"

    marked = await client.put(url, json={"confirmed": True})
    assert marked.status_code == 200, marked.text
    assert marked.json() == {"month": "2026-11", "entry_state": "no_payments_confirmed"}

    unmarked = await client.put(url, json={"confirmed": False})
    assert unmarked.status_code == 200, unmarked.text
    assert unmarked.json() == {"month": "2026-11", "entry_state": "not_entered"}
    assert (await _plan_fact(client))["2026-11"]["entry_state"] == "not_entered"


# --- розбіжність залишку в місяці зі звіркою ---------------------------------------------------

async def test_balance_diff_is_null_in_month_with_statement_check(client, clock):
    """Після звірки залишок місяця без відсотків решти місяця, тож із плановим не порівнюється."""
    a = await _debt(client)
    b = await _debt(client, **CARD)
    await _accept(client, await _draft(client))
    await _pay(client, a["id"], date(2026, 11, 14), 1_000)
    clock["today"] = date(2026, 11, 20)
    resp = await client.post(f"/debt/{b['id']}/balance-checks", json={"as_of": "2026-11-20", "balance": 7_000})
    assert resp.status_code == 201, resp.text

    clock["today"] = date(2026, 12, 10)
    body = await _plan_fact_body(client)
    nov = body["months"][0]
    row_a, row_b = _row(nov, a["id"]), _row(nov, b["id"])
    assert row_b["check"] is not None and row_b["actual_closing"] == 7_000
    assert row_b["balance_diff"] is None
    assert row_b["payment_diff"] == -row_b["planned_payment"]
    assert row_a["balance_diff"] is not None
    assert nov["totals"]["balance_diff"] is None

    cum = {d["debt_id"]: d for d in body["cumulative"]["debts"]}
    assert cum[b["id"]]["balance_diff"] is None
    assert cum[a["id"]]["balance_diff"] == row_a["balance_diff"]
    assert body["cumulative"]["balance_diff"] == row_a["balance_diff"]


async def test_cumulative_balance_diff_after_check_month_is_unknown(client, clock):
    """Звірка в останньому закритому місяці не лишає в cumulative розбіжність із місяця до неї."""
    debt = await _debt(client)
    await _accept(client, await _draft(client))
    await _pay(client, debt["id"], date(2026, 11, 14), 1_000)
    clock["today"] = date(2026, 12, 20)
    await _pay(client, debt["id"], date(2026, 12, 5), 1_000)
    resp = await client.post(f"/debt/{debt['id']}/balance-checks", json={"as_of": "2026-12-15", "balance": 9_000})
    assert resp.status_code == 201, resp.text

    clock["today"] = date(2027, 1, 10)
    body = await _plan_fact_body(client)
    nov, dec = body["months"][0], body["months"][1]
    assert _row(nov, debt["id"])["balance_diff"] is not None
    assert _row(dec, debt["id"])["balance_diff"] is None
    assert body["cumulative"]["debts"][0]["balance_diff"] is None
    assert body["cumulative"]["balance_diff"] is None
