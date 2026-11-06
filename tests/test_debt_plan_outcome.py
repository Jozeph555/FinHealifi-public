"""
Наслідок план-факту (GET /debt/plan-fact/outcome) і його речення.

HTTP — справжні маршрути app.main з JWT тимчасового користувача (дані
комітяться в тестову БД і видаляються після тесту); «сьогодні» — підміна
app.api.deps.today_kyiv.

Сценарії: платежі точно за планом (збіг до гривні), менші за план (пізніше,
переплата більша), більші (раніше, менша), місяць без платежів
(unresolved_months і зняття через позначку), усі борги закрито (paid_off),
без плану (404), відсутність заборонених слів у реченнях.
"""
import re
import uuid
from datetime import date

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import app.api.deps as deps
from app.main import app as fastapi_app
from app.models.database import AsyncSessionLocal
from app.models.debt import Debt
from app.models.debt_plan import (
    DebtBalanceCheck, DebtPayment, DebtPlanMonth, DebtPlanSchedule, DebtPlanVersion,
)
from app.models.milp_plan import MilpPlan
from app.models.user import User
from app.services import debt_outcome
from app.services.llm_advisor import BANNED_PATTERNS, RETRY_PATTERNS, find_banned_words

INSTALLMENT = {"name": "Розстрочка", "balance": 10_000, "monthly_rate": 0.02, "min_payment": 1_000,
               "debt_type": "installment"}
CARD = {"name": "Картка", "balance": 7_345.67, "monthly_rate": 0.0347, "min_payment": None, "debt_type": "revolving"}
# Слова про штрафи, прострочення й оцінки, яких у реченні наслідку не має бути.
EXTRA_FORBIDDEN_RE = re.compile(r"штраф|пен[яіюе]\b|простроч|недоплат|добре|погано|чудово|на жаль|молодець", re.I)


@pytest_asyncio.fixture
async def clock(monkeypatch):
    """Керований «сьогодні»: підміняє app.api.deps.today_kyiv."""
    state = {"today": date(2026, 11, 14)}
    monkeypatch.setattr(deps, "today_kyiv", lambda today=None: state["today"])
    return state


async def _cleanup(user_id: str) -> None:
    async with AsyncSessionLocal() as s:
        version_ids = select(DebtPlanVersion.id).where(DebtPlanVersion.user_id == user_id)
        await s.execute(delete(DebtPlanSchedule).where(DebtPlanSchedule.version_id.in_(version_ids)))
        for model in (DebtPlanVersion, DebtPayment, DebtBalanceCheck, DebtPlanMonth, MilpPlan, Debt):
            await s.execute(delete(model).where(model.user_id == user_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


@pytest_asyncio.fixture
async def api(clock):
    """Клієнт із JWT тимчасового користувача."""
    email = f"outcome-{uuid.uuid4().hex[:12]}@example.com"
    password = "test-password-123"
    user_id = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as c:
        try:
            assert (await c.post("/auth/register", json={"email": email, "password": password})).status_code == 201
            resp = await c.post("/auth/login", json={"email": email, "password": password})
            c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
            user_id = (await c.get("/auth/me")).json()["id"]
            yield c
        finally:
            if user_id is None:
                async with AsyncSessionLocal() as s:
                    user_id = (await s.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
            if user_id is not None:
                await _cleanup(user_id)


async def _accepted_plan(api, extra=1_000):
    """Два борги, чернетка й прийняття першої версії; повертає (борги, версія)."""
    a = (await api.post("/debt", json=INSTALLMENT)).json()
    b = (await api.post("/debt", json=CARD)).json()
    plan_id = (await api.post("/debt/optimize", json={"extra_monthly_uah": extra})).json()["plan_id"]
    resp = await api.post(f"/debt/plans/{plan_id}/accept")
    assert resp.status_code == 201, resp.text
    return (a, b), resp.json()


async def _november_payments(api, clock, payments):
    """Вносить платежі 20.11.2026 (пари борг, сума) і переводить «сьогодні» на 10.12.2026."""
    clock["today"] = date(2026, 11, 25)
    for debt, amount in payments:
        await _pay(api, debt["id"], date(2026, 11, 20), amount)
    clock["today"] = date(2026, 12, 10)


def _planned(version, month, debt_id):
    m = next(m for m in version["schedule"] if m["month"] == month)
    return next(d for d in m["debts"] if d["debt_id"] == debt_id)["payment"]


async def _pay(api, debt_id, day, amount):
    resp = await api.post("/debt/payments", json={"debt_id": debt_id, "paid_on": day.isoformat(), "amount": amount})
    assert resp.status_code == 201, resp.text


async def _outcome(api):
    resp = await api.get("/debt/plan-fact/outcome")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _assert_neutral(sentence: str) -> None:
    assert find_banned_words(sentence) == [], sentence
    for pattern in RETRY_PATTERNS.values():
        assert not re.search(pattern, sentence, re.I), sentence
    assert not EXTRA_FORBIDDEN_RE.search(sentence), sentence
    assert not re.search(r"\d[.,]\d", sentence.replace("р.", "")), f"копійки в реченні: {sentence}"


async def test_outcome_without_plan_is_404(api):
    resp = await api.get("/debt/plan-fact/outcome")
    assert resp.status_code == 404, resp.text


async def test_payments_exactly_as_planned_match_version(api, clock):
    (a, b), v1 = await _accepted_plan(api)

    # Поточний місяць першого плану: прогноз від стартових залишків = план.
    now = await _outcome(api)
    assert now["status"] == "ok"
    assert now["payoff_month"] == v1["payoff_month"]
    assert now["payoff_shift_months"] == 0
    assert now["interest_diff"] == pytest.approx(0, abs=0.005)

    await _november_payments(api, clock, [(d, _planned(v1, "2026-11", d["id"])) for d in (a, b)])
    out = await _outcome(api)
    assert out["status"] == "ok"
    assert out["unresolved_months"] == []
    assert out["version_payoff_month"] == v1["payoff_month"]
    assert out["payoff_month"] == v1["payoff_month"]
    assert out["payoff_shift_months"] == 0
    assert out["version_total_interest"] == pytest.approx(v1["total_interest"])
    assert out["projected_total_interest"] == pytest.approx(v1["total_interest"], abs=0.005)
    assert out["interest_diff"] == pytest.approx(0, abs=0.005)
    assert out["sentence"].endswith("як і в чинному плані; переплата така сама.")
    _assert_neutral(out["sentence"])


async def test_underpayment_shifts_payoff_later(api, clock):
    (a, b), v1 = await _accepted_plan(api)
    await _november_payments(api, clock, [(a, 100)])
    out = await _outcome(api)
    assert out["status"] == "ok"
    assert out["payoff_shift_months"] > 0
    assert out["interest_diff"] > 0
    assert out["projected_total_interest"] == pytest.approx(out["version_total_interest"] + out["interest_diff"])
    assert f"на {out['payoff_shift_months']} міс. пізніше, ніж у чинному плані" in out["sentence"]
    assert "переплата більша на " in out["sentence"]
    _assert_neutral(out["sentence"])


async def test_overpayment_shifts_payoff_earlier(api, clock):
    (a, b), v1 = await _accepted_plan(api)
    await _november_payments(api, clock, [(d, _planned(v1, "2026-11", d["id"]) + 2_500) for d in (a, b)])
    out = await _outcome(api)
    assert out["status"] == "ok"
    assert out["payoff_shift_months"] < 0
    assert out["interest_diff"] < 0
    assert f"на {-out['payoff_shift_months']} міс. раніше, ніж у чинному плані" in out["sentence"]
    assert "переплата менша на " in out["sentence"]
    _assert_neutral(out["sentence"])


async def test_month_without_payments_is_unresolved_until_marked(api, clock):
    await _accepted_plan(api)
    clock["today"] = date(2026, 12, 10)
    out = await _outcome(api)
    assert out["status"] == "unresolved_months"
    assert out["unresolved_months"] == ["2026-11"]
    assert out["payoff_month"] is None and out["interest_diff"] is None
    assert out["sentence"] == "Щоб порахувати наслідок, внеси платежі за листопад 2026 р. або познач, що їх не було."
    _assert_neutral(out["sentence"])

    mark = await api.put("/debt/plan-fact/months/2026-11/no-payments", json={"confirmed": True})
    assert mark.status_code == 200, mark.text
    out = await _outcome(api)
    assert out["status"] == "ok"
    assert out["payoff_shift_months"] > 0 and out["interest_diff"] > 0


async def test_statement_check_resolves_month_without_payments(api, clock):
    (a, b), _ = await _accepted_plan(api)
    clock["today"] = date(2026, 12, 10)
    for debt, balance in ((a, 9_000), (b, 7_000)):
        resp = await api.post(f"/debt/{debt['id']}/balance-checks", json={"as_of": "2026-12-02", "balance": balance})
        assert resp.status_code == 201, resp.text
    out = await _outcome(api)
    assert out["status"] == "ok"
    assert out["unresolved_months"] == []


async def test_all_debts_paid_off(api, clock):
    (a, b), _ = await _accepted_plan(api)
    await _november_payments(api, clock, [(d, 20_000) for d in (a, b)])
    out = await _outcome(api)
    assert out["status"] == "paid_off"
    assert out["payoff_month"] == "2026-11"
    assert out["payoff_shift_months"] < 0
    assert out["interest_diff"] < 0
    assert out["sentence"].startswith("За внесеними платежами всі борги закрито в листопаді 2026 р., на ")
    _assert_neutral(out["sentence"])


def test_sentences_are_neutral_and_whole_hryvnias():
    """Усі варіанти речення: без заборонених слів, суми в цілих гривнях."""
    sentences = [
        debt_outcome.outcome_sentence(status, month, shift, diff)
        for status in (debt_outcome.STATUS_OK, debt_outcome.STATUS_PAID_OFF)
        for month in (date(2028, 3, 1), date(2027, 9, 1), None)
        for shift in (-2, 0, 3, None)
        for diff in (-1240, 0, 1_234_567, None)
    ] + [debt_outcome.unresolved_sentence([date(2026, 11, 1)]),
         debt_outcome.unresolved_sentence([date(2026, 11, 1), date(2027, 1, 1)])]
    assert BANNED_PATTERNS
    for sentence in sentences:
        _assert_neutral(sentence)
    assert debt_outcome.outcome_sentence("ok", date(2028, 3, 1), 2, 1240) == (
        "За внесеними платежами борги закриються в березні 2028 р., на 2 міс. пізніше, ніж у чинному плані; "
        "переплата більша на 1 240 грн."
    )
    assert "у вересні 2027 р." in debt_outcome.outcome_sentence("ok", date(2027, 9, 1), None, None)
    assert debt_outcome.whole_uah(124_050) == 1_241 and debt_outcome.whole_uah(-124_050) == -1_241
