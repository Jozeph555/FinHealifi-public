"""
Тести бектесту правил планування додаткового платежу
(scripts/analysis/planning_rules_backtest.py).

- Немає витоку майбутнього: план на точці t, порахований з повної історії,
  дорівнює плану з даних, обрізаних по кінцю t (майбутні рядки видаляються в
  межах SAVEPOINT).
- Детермінованість і значення правил.
- Коротка й порожня історія: fallback з поясненням.
- Інтервал Вілсона й критерій вибору правила.
"""
import math
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete

from app.models.account import Account
from app.models.transaction import Transaction
from scripts.analysis.forecast_backtest import load_series, window_at
from scripts.analysis.planning_rules_backtest import RULES, choose, plan_at, run, wilson


def _ts(year, month, day=10):
    return int(datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp())


async def _seed(db_session, user):
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=user.id, source_type="monobank_api")
    db_session.add(account)
    await db_session.flush()
    months = [(2025, m) for m in range(9, 13)] + [(2026, m) for m in range(1, 6)]
    for k, (y, m) in enumerate(months, start=1):
        for amount, category, method, mcc in [
            (3_000_000 + (k % 3) * 400_000, "Заробітна плата", "keyword", None),
            (-2_500_000 - (k % 4) * 300_000, "Продукти", "mcc", 5411),
            (-200_000, "pending", "pending", 4829),
        ]:
            db_session.add(Transaction(
                id=str(uuid.uuid4()), user_id=user.id, account_id=account.id,
                time=_ts(y, m), time_precision="second", description="Опис", mcc=mcc,
                amount=amount, amount_uah=amount // 100, category=category, category_method=method,
            ))
    await db_session.flush()


async def test_plan_at_origin_has_no_future_leakage(db_session, real_user):
    await _seed(db_session, real_user)
    origin, target = date(2026, 2, 1), date(2026, 4, 1)

    full = await load_series(db_session, real_user.id)
    plans_full = plan_at(window_at(full, origin), target)

    savepoint = await db_session.begin_nested()
    await db_session.execute(delete(Transaction).where(
        Transaction.user_id == real_user.id, Transaction.time >= _ts(2026, 3, 1) - 12 * 3600,
    ))
    truncated = await load_series(db_session, real_user.id)
    plans_cut = plan_at(window_at(truncated, origin), target)
    await savepoint.rollback()

    assert "2026-03" in full["income"] and "2026-03" not in truncated["income"]
    assert plans_full == plans_cut
    assert max(window_at(full, origin)["months"]) <= "2026-02"


def _window(income, expenses):
    months = [f"2025-{m:02d}" if m <= 12 else f"2026-{m - 12:02d}" for m in range(1, len(income) + 1)]
    return {"months": months, "fallback_reason": None, "income": income, "expenses": expenses}


def test_rules_values_and_determinism():
    income = [100.0, 110.0, 90.0, 120.0, 100.0, 105.0, 95.0, 300.0, 100.0, 110.0, 100.0, 90.0, 105.0]
    expenses = [90.0, 100.0, 100.0, 80.0, 95.0, 100.0, 120.0, 100.0, 90.0, 100.0, 95.0, 85.0, 100.0]
    fcf = [i - e for i, e in zip(income, expenses)]
    window = _window(income, expenses)

    plans = plan_at(window, date(2026, 3, 1), prophet=False)

    assert plans == plan_at(window, date(2026, 3, 1), prophet=False)
    assert plans["R0"]["P"] == 0.0
    assert plans["R1"]["P"] == sorted(fcf[-6:])[2] / 2 + sorted(fcf[-6:])[3] / 2
    assert plans["R4"]["P"] == min(fcf[-6:])
    assert plans["R2"]["P"] == pytest.approx(sorted(fcf[-12:])[5] / 2 + sorted(fcf[-12:])[6] / 2)
    s12 = sorted(fcf[-12:])  # лінійна інтерполяція: позиція 0,25 * 11 = 2,75
    assert plans["R3"]["P"] == pytest.approx(s12[2] + 0.75 * (s12[3] - s12[2]))
    assert plans["R3"]["P"] <= plans["R2"]["P"]
    assert all(not plans[r]["note"] for r in ("R1", "R2", "R3", "R4", "R6"))
    p10, p90, _ = plans["_interval"]
    assert p10 < plans["R2"]["P"] < p90


def test_short_history_is_honest_fallback():
    window = _window([100.0, 120.0, 90.0], [80.0, 100.0, 95.0])

    plans = plan_at(window, date(2025, 5, 1), prophet=False)

    assert plans["R2"]["P"] == 20.0 and "3 < 12" in plans["R2"]["note"]  # медіана [20, 20, -5]
    assert plans["R1"]["P"] == 20.0 and "3 < 6" in plans["R1"]["note"]
    assert plans["R4"]["P"] == -5.0
    assert "3 < 12" in plans["R6"]["note"] and "3 < 6" in plans["R6"]["note"]


def test_empty_history_gives_zero_with_note():
    plans = plan_at(_window([], []), date(2025, 3, 1), prophet=False)

    assert all(plans[r]["P"] == 0.0 and "немає історії" in plans[r]["note"] for r in RULES)


def test_run_targets_only_completed_months_and_deterministic():
    months = [date(2025, m, 1) for m in range(1, 13)] + [date(2026, m, 1) for m in range(1, 4)]
    series = {
        "coverage": months,
        "income": {f"{d:%Y-%m}": 1000.0 + (i % 4) * 50 for i, d in enumerate(months)},
        "expenses": {f"{d:%Y-%m}": 900.0 + (i % 3) * 60 for i, d in enumerate(months)},
    }

    rows = run(series, date(2026, 3, 20), 2, prophet=False)

    assert rows == run(series, date(2026, 3, 20), 2, prophet=False)
    assert max(r["target"] for r in rows) == "2026-02"
    assert all(r["E"] == max(r["P"], 0.0) for r in rows if not math.isnan(r["P"]))


def test_wilson_interval():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert all(math.isnan(x) for x in wilson(0, 0))


def _m(share, mean_e):
    return {"share": share, "mean_e": mean_e}


def test_choose_follows_fixed_criterion():
    base = {r: _m(0.5, 1000.0) for r in RULES}

    assert choose(base)["rule"] == "R0"  # жодне не <= 25 %
    assert "лише мінімальні платежі" in choose(base)["verdict"]

    picked = {**base, "R3": _m(0.20, 5000.0), "R5": _m(0.25, 4000.0)}
    assert choose(picked)["rule"] == "R3"  # найбільший E серед допустимих

    tie = {**base, "R6": _m(0.20, 5000.0), "R2": _m(0.10, 4800.0)}  # різниця 4 % <= 5 %
    assert choose(tie)["rule"] == "R2"  # простіше (1 параметр проти 3)
