"""
Тести бектесту прогнозу (scripts/analysis/forecast_backtest.py).

- Немає витоку майбутнього: ряд на точці t, взятий зрізом повної історії,
  дорівнює ряду, який функції прогнозу будують на даних, обрізаних по кінцю t
  (майбутні рядки видаляються в межах SAVEPOINT).
- Прості моделі детерміновані.
"""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import delete

from app.models.account import Account
from app.models.transaction import Transaction
from scripts.analysis.forecast_backtest import (
    load_series,
    origins,
    run_backtest,
    simple_forecasts,
    window_at,
)


def _ts(year, month, day=10):
    return int(datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp())


async def _seed(db_session, user):
    account = Account(id=f"test-acc-{uuid.uuid4()}", user_id=user.id, source_type="monobank_api")
    db_session.add(account)
    await db_session.flush()
    rows = []
    for i, (y, m) in enumerate([(2025, 10), (2025, 11), (2025, 12), (2026, 1), (2026, 2), (2026, 3),
                                (2026, 4), (2026, 5)]):
        k = i + 1
        rows += [
            (y, m, 3_000_000 + k * 10_000, "Заробітна плата", "keyword", None),   # дохід
            (y, m, -1_000_000 - k * 5_000, "Продукти", "mcc", 5411),               # витрата
            (y, m, -200_000, "pending", "pending", 5812),                          # pending мерчант
            (y, m, -400_000, "pending", "pending", 4829),                          # P2P відтік
            (y, m, 150_000, "pending", "pending", 4829),                           # P2P надходження (неттінг)
        ]
    for y, m, amount, category, method, mcc in rows:
        db_session.add(Transaction(
            id=str(uuid.uuid4()), user_id=user.id, account_id=account.id,
            time=_ts(y, m), time_precision="second", description="Опис", mcc=mcc,
            amount=amount, amount_uah=amount // 100, category=category, category_method=method,
        ))
    await db_session.flush()


async def test_series_at_origin_has_no_future_leakage(db_session, real_user):
    await _seed(db_session, real_user)
    origin = date(2026, 3, 1)

    full = await load_series(db_session, real_user.id)
    sliced = window_at(full, origin)

    # Ті самі функції прогнозу на даних, обрізаних по кінцю origin.
    savepoint = await db_session.begin_nested()
    await db_session.execute(delete(Transaction).where(
        Transaction.user_id == real_user.id, Transaction.time >= _ts(2026, 4, 1) - 12 * 3600,
    ))
    truncated = await load_series(db_session, real_user.id)
    cut = window_at(truncated, origin)
    await savepoint.rollback()

    assert "2026-04" in full["income"] and "2026-04" not in truncated["income"]  # обрізання справжнє
    assert sliced == cut
    assert sliced["months"] and max(sliced["months"]) <= "2026-03"
    assert sliced["fallback_reason"] is None


def test_simple_models_are_deterministic():
    values = [100.0, 120.0, 90.0, 400.0, 110.0, 105.0, 95.0]

    first = simple_forecasts(values)
    assert first == simple_forecasts(values)
    assert first["naive"] == 95.0
    assert first["mean3"] == (110.0 + 105.0 + 95.0) / 3
    assert first["median6"] == 107.5
    assert first["window_mean"] == sum(values) / len(values)
    # 400 — викид: вінзоризований варіант тягне середнє вниз, сирий — ні
    assert first["window_mean_w"] < first["window_mean"]


def test_backtest_targets_are_only_completed_months():
    months = [date(2025, m, 1) for m in range(1, 13)] + [date(2026, m, 1) for m in range(1, 4)]
    series = {
        "coverage": months,
        "income": {f"{d:%Y-%m}": 1000.0 + i for i, d in enumerate(months)},
        "expenses": {f"{d:%Y-%m}": 800.0 + 2 * i for i, d in enumerate(months)},
    }
    today = date(2026, 3, 20)  # 2026-03 — поточний, неповний

    for horizon in (1, 2):
        rows = run_backtest(series, today, horizon, prophet=False)
        assert rows == run_backtest(series, today, horizon, prophet=False)
        assert max(r["target"] for r in rows) == "2026-02"
        assert all(r["origin"] < r["target"] for r in rows)
    # перша точка — коли вікно дає Prophet (>= 3 повних місяців після першого неповного)
    assert origins(series, today, 1)[0] == date(2025, 4, 1)
