"""
Тести ліміту часу MILP-розв'язувача і запасного плану.

Розв'язувач замінюється зависаючим, падаючим або повільним (monkeypatch), тож
тести не залежать від швидкості CBC. Перевіряється: зависання й помилка дають
план «спочатку найдорожчий борг» з fallback=true і статусом розв'язувача;
POST /debt/optimize і GET /debt/plan/latest повертають позначку й пояснення;
повільне розв'язання не блокує event loop; без зависання результат той самий,
що й у синхронного optimize_debt_repayment; якщо avalanche не вкладається в
бюджет, запасного плану немає. Без запасного плану «не встиг» (Time Limit,
Not Solved, Timeout) дає 503 з окремим текстом, а неможливий план — 422 з
порадою про додатковий платіж; порожня причина винятку в текст не потрапляє.
"""
import asyncio
import threading
import time

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import app.services.debt_optimizer as debt_optimizer
import app.services.milp_optimizer as milp_optimizer
from app.api.debt import (
    FALLBACK_PLAN_NOTE,
    SOLVER_NOT_FINISHED_DETAIL,
    DebtCreateRequest,
    DebtOptimizeRequest,
    ExtraPaymentRequest,
    create_debt,
    latest_plan,
    optimize,
    optimize_with_extra,
    router as debt_router,
)
from app.api.deps import get_current_user, get_db
from app.services.debt_optimizer import optimize_debt_repayment, optimize_debt_repayment_async
from app.services.milp_optimizer import solve_in_thread

DEBTS = [
    {"name": "Картка", "balance": 20000.0, "monthly_rate": 0.04, "debt_type": "revolving", "min_payment": None},
    {"name": "Кредит", "balance": 30000.0, "monthly_rate": 0.02, "debt_type": "installment", "min_payment": 1500.0},
]
BUDGET = 6000.0
MONTHS = 36


@pytest.fixture
def short_limit(monkeypatch):
    """Загальний ліміт очікування розв'язувача ~0.15 с."""
    monkeypatch.setattr(milp_optimizer, "MILP_TIME_LIMIT_SEC", 0.05)
    monkeypatch.setattr(milp_optimizer, "MILP_TIMEOUT_GRACE_SEC", 0.1)


@pytest.fixture
def hanging_solver(monkeypatch):
    """Розв'язувач, що не повертається, доки тест не завершиться."""
    release = threading.Event()

    def _hang(*args):
        release.wait(10)
        raise RuntimeError("зависаючий розв'язувач відпущено")

    monkeypatch.setattr(debt_optimizer, "optimize_milp", _hang)
    yield _hang
    release.set()


def _assert_is_avalanche(strategies: dict, status: str):
    milp, avalanche = strategies["milp"], strategies["avalanche"]
    assert milp["fallback"] is True
    assert milp["status"] == status
    assert milp["strategy"] == "milp"
    for key in ("months_to_payoff", "total_interest_paid", "monthly_log", "final_balances"):
        assert milp[key] == avalanche[key]


async def test_solve_in_thread_raises_timeout(short_limit, hanging_solver):
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await solve_in_thread(hanging_solver, DEBTS, BUDGET, MONTHS)
    assert time.monotonic() - started < 2


async def test_hanging_solver_falls_back_to_avalanche(short_limit, hanging_solver):
    started = time.monotonic()
    strategies = await optimize_debt_repayment_async(DEBTS, BUDGET, MONTHS)
    assert time.monotonic() - started < 2
    _assert_is_avalanche(strategies, debt_optimizer.STATUS_TIMEOUT)


async def test_solver_error_falls_back_to_avalanche(monkeypatch):
    def _fail(*args):
        raise RuntimeError("CBC не запустився")

    monkeypatch.setattr(debt_optimizer, "optimize_milp", _fail)
    strategies = await optimize_debt_repayment_async(DEBTS, BUDGET, MONTHS)
    _assert_is_avalanche(strategies, debt_optimizer.STATUS_ERROR)


async def test_non_optimal_status_falls_back_to_avalanche(monkeypatch):
    real = milp_optimizer.optimize_milp

    def _time_limited(*args):
        return {**real(*args), "status": milp_optimizer.STATUS_TIME_LIMIT}

    monkeypatch.setattr(debt_optimizer, "optimize_milp", _time_limited)
    strategies = await optimize_debt_repayment_async(DEBTS, BUDGET, MONTHS)
    _assert_is_avalanche(strategies, milp_optimizer.STATUS_TIME_LIMIT)


async def test_no_fallback_when_avalanche_exceeds_budget():
    """Бюджет менший за обов'язкові платежі: MILP неможливий, avalanche бюджет перевищує."""
    debts = [{"name": "Кредит", "balance": 30000.0, "monthly_rate": 0.02,
              "debt_type": "installment", "min_payment": 1500.0}]
    strategies = await optimize_debt_repayment_async(debts, 1000.0, 12)
    assert strategies["milp"]["status"] != "Optimal"
    assert "fallback" not in strategies["milp"]


async def test_normal_path_matches_sync_result():
    sync = optimize_debt_repayment(DEBTS, BUDGET, MONTHS)
    async_ = await optimize_debt_repayment_async(DEBTS, BUDGET, MONTHS)
    assert async_ == sync
    assert async_["milp"]["status"] == "Optimal"
    assert "fallback" not in async_["milp"]


async def _seed_debts(db_session, user):
    for d in DEBTS:
        await create_debt(DebtCreateRequest(**{k: v for k, v in d.items() if v is not None}),
                          current_user=user, db=db_session)


async def test_optimize_endpoint_returns_fallback(db_session, real_user, short_limit, hanging_solver):
    await _seed_debts(db_session, real_user)

    response = await optimize_with_extra(
        ExtraPaymentRequest(extra_monthly_uah=2000), current_user=real_user, db=db_session, months=MONTHS,
    )
    assert response["fallback"] is True
    assert response["plan_note"] == FALLBACK_PLAN_NOTE
    assert response["status"] == debt_optimizer.STATUS_TIMEOUT
    assert response["comparison"]["milp"] == response["comparison"]["avalanche"]

    saved = await latest_plan(current_user=real_user, db=db_session)
    assert saved["plan_id"] == response["plan_id"]
    assert saved["fallback"] is True
    assert saved["plan_note"] == FALLBACK_PLAN_NOTE


async def test_optimize_endpoint_normal_path_has_no_fallback(db_session, real_user):
    await _seed_debts(db_session, real_user)
    response = await optimize_with_extra(
        ExtraPaymentRequest(extra_monthly_uah=2000), current_user=real_user, db=db_session, months=MONTHS,
    )
    assert response["status"] == "Optimal"
    assert response["fallback"] is False
    assert response["plan_note"] is None


async def test_slow_solver_does_not_block_event_loop(db_session, real_user, monkeypatch):
    """Поки POST /debt/optimize чекає на повільний розв'язувач, легкий GET /health відповідає."""
    await _seed_debts(db_session, real_user)
    real = milp_optimizer.optimize_milp
    started, finished = threading.Event(), threading.Event()

    def _slow(*args):
        started.set()
        time.sleep(1.0)
        finished.set()
        return real(*args)

    monkeypatch.setattr(debt_optimizer, "optimize_milp", _slow)

    app = FastAPI()
    app.include_router(debt_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    async def _db():
        return db_session

    app.dependency_overrides[get_current_user] = lambda: real_user
    app.dependency_overrides[get_db] = _db

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        optimize_task = asyncio.create_task(
            client.post("/debt/optimize", json={"extra_monthly_uah": 2000})
        )
        deadline = time.monotonic() + 5
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set()

        health = await client.get("/health")
        assert health.status_code == 200
        # Відповідь отримано, поки розв'язувач ще працює.
        assert not finished.is_set()

        optimize = await optimize_task
    assert optimize.status_code == 201
    assert optimize.json()["status"] == "Optimal"
    assert optimize.json()["fallback"] is False


# --- Відповідь без запасного плану: «не встиг» проти неможливого плану ---------

# Картка 10 000 під 6 %/міс., додатковий 0: бюджет = мінімум місяця 1 = 400 грн;
# у місяці 2 мінімум 4 % від 10 200 = 408 > 400, тож avalanche перевищує бюджет,
# а справжній CBC дає Infeasible.
OVER_BUDGET_CARD = dict(name="Картка", balance=10_000.0, monthly_rate=0.06, debt_type="revolving")
INFEASIBLE_PREFIX = "Не вдалося розрахувати план за цих умов"


async def _optimize_error(db_session, user) -> HTTPException:
    await create_debt(DebtCreateRequest(**OVER_BUDGET_CARD), current_user=user, db=db_session)
    with pytest.raises(HTTPException) as exc:
        await optimize_with_extra(ExtraPaymentRequest(extra_monthly_uah=0),
                                  current_user=user, db=db_session, months=MONTHS)
    return exc.value


def _assert_not_finished(err: HTTPException):
    assert err.status_code == 503
    assert err.detail == SOLVER_NOT_FINISHED_DETAIL
    assert not err.detail.startswith(INFEASIBLE_PREFIX)
    assert ": ." not in err.detail
    assert "додатков" not in err.detail.lower()


@pytest.mark.parametrize("status", [milp_optimizer.STATUS_TIME_LIMIT, "Not Solved"])
async def test_not_finished_status_without_fallback_is_503(db_session, real_user, monkeypatch, status):
    real = milp_optimizer.optimize_milp
    monkeypatch.setattr(debt_optimizer, "optimize_milp", lambda *a: {**real(*a), "status": status})
    _assert_not_finished(await _optimize_error(db_session, real_user))


async def test_timeout_without_fallback_is_503(db_session, real_user, short_limit, hanging_solver):
    _assert_not_finished(await _optimize_error(db_session, real_user))


async def test_real_infeasible_keeps_old_422_text(db_session, real_user):
    err = await _optimize_error(db_session, real_user)
    assert err.status_code == 422
    assert err.detail == (
        "Не вдалося розрахувати план за цих умов: місячний бюджет 400 грн "
        "(обов'язкові платежі 400 + додатковий 0). "
        "Спробуй збільшити додатковий платіж або перевір ставки й мінімальні платежі боргів."
    )
    assert err.detail != SOLVER_NOT_FINISHED_DETAIL


async def test_error_with_empty_reason_has_no_empty_insert(db_session, real_user, monkeypatch):
    def _fail(*args):
        raise RuntimeError("")

    monkeypatch.setattr(debt_optimizer, "optimize_milp", _fail)
    err = await _optimize_error(db_session, real_user)
    assert err.status_code == 422
    assert ": ." not in err.detail
    assert err.detail.startswith("Не вдалося розрахувати план. ")


async def test_optimize_raw_timeout_is_503(short_limit, hanging_solver):
    request = DebtOptimizeRequest(
        debts=[{**OVER_BUDGET_CARD, "min_payment": None}], monthly_budget=400.0, months=MONTHS,
    )
    with pytest.raises(HTTPException) as exc:
        await optimize(request)
    _assert_not_finished(exc.value)
