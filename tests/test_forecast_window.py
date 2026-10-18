"""
Тести вікна прогнозу й обробки викидів.

- Покриття — календарні місяці з будь-якою транзакцією користувача.
- Блок — останній суцільний відрізок місяців покриття.
- Повні місяці — блок без першого місяця і без поточного календарного.
- Місяць вікна без доходу чи витрат дорівнює 0.
- fallback_reason (пріоритет partial_months_only > stale_data >
  insufficient_months): середнє замість Prophet; data_stale — окремий прапорець.
- Prophet прогнозує на period_start.
- Вінзоризація median ± 3·1.4826·MAD лише на вході моделі, для n >= 3;
  MAD = 0 -> без змін; у відповіді й series_actual — фактичні суми.

generate_forecast викликається з db_session і явним today.
"""
import uuid
from datetime import date, datetime, timezone

import pandas as pd
import pytest

import app.services.forecasting as forecasting
from app.api.forecast import latest
from app.models.account import Account
from app.models.transaction import Transaction
from app.services.forecasting import generate_forecast

INCOME = "Заробітна плата"
EXPENSE = "Продукти"


def _ts(year, month, day):
    return int(datetime(year, month, day, 12, tzinfo=timezone.utc).timestamp())


@pytest.fixture
async def account(db_session, real_user):
    acc = Account(id=f"monobank_api-{uuid.uuid4()}", user_id=real_user.id, source_type="monobank_api")
    db_session.add(acc)
    await db_session.flush()
    return acc


async def _txn(db_session, user_id, account_id, day: date, amount_uah, category):
    db_session.add(Transaction(
        id=str(uuid.uuid4()),
        user_id=user_id,
        account_id=account_id,
        time=_ts(day.year, day.month, day.day),
        time_precision="second",  # акаунт тесту — monobank_api
        amount=amount_uah * 100,
        amount_uah=amount_uah,
        mcc=5411 if category == EXPENSE else None,
        category=category,
        category_method="mcc" if category == EXPENSE else "keyword",
    ))


async def _months(db_session, user_id, account_id, months, expense=1000, income=10000, day=10):
    """Створює дохід і витрату в заданий день кожного місяця.

    expense і income — число для всіх місяців або словник {(рік, місяць): сума};
    None — рядок не створюється.
    """
    for year, month in months:
        e = expense.get((year, month)) if isinstance(expense, dict) else expense
        i = income.get((year, month)) if isinstance(income, dict) else income
        if e is not None:
            await _txn(db_session, user_id, account_id, date(year, month, day), -e, EXPENSE)
        if i is not None:
            await _txn(db_session, user_id, account_id, date(year, month, day + 1), i, INCOME)
    await db_session.commit()


def _span(start, end):
    """Повертає [(рік, місяць)] від start до end включно."""
    out, (y, m) = [], start
    while (y, m) <= end:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@pytest.fixture
def prophet_spy(monkeypatch):
    calls = []
    real = forecasting._run_prophet

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(forecasting, "_run_prophet", spy)
    return calls


# --- Вікно: неповні місяці ------------------------------------------------

async def test_current_incomplete_month_is_excluded(db_session, real_user, account):
    await _months(db_session, real_user.id, account.id, _span((2026, 9), (2027, 3)))

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2027, 3, 15))

    assert forecast.raw_output["months_used"] == ["2026-10", "2026-11", "2026-12", "2027-01", "2027-02"]
    assert forecast.raw_output["fallback_reason"] is None
    assert forecast.method == "prophet"


async def test_first_block_month_is_excluded_even_if_data_starts_on_the_1st(db_session, real_user, account):
    # Дані з першого числа місяця не роблять перший місяць блоку повним.
    await _months(db_session, real_user.id, account.id, _span((2026, 12), (2027, 3)), day=1)

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2027, 3, 15))

    assert forecast.raw_output["months_used"] == ["2027-01", "2027-02"]


async def test_past_last_block_month_counts_as_full(db_session, real_user, account, prophet_spy):
    # Поточний місяць ще без транзакцій: минулий останній місяць блоку — повний,
    # прогноз не застарілий.
    await _months(db_session, real_user.id, account.id, _span((2026, 11), (2027, 2)), day=20)

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2027, 3, 2))

    assert forecast.raw_output["months_used"] == ["2026-12", "2027-01", "2027-02"]
    assert forecast.raw_output["data_stale"] is False
    assert forecast.raw_output["fallback_reason"] is None
    assert len(prophet_spy) == 2  # дохід і витрати


# --- Розрив усередині історії vs застарілість після останньої транзакції ---

async def test_gap_inside_history_uses_only_last_block(db_session, real_user, account, prophet_spy):
    await _months(db_session, real_user.id, account.id, _span((2026, 1), (2026, 3)), expense=50000)
    await _months(db_session, real_user.id, account.id, _span((2026, 6), (2026, 10)), day=5)

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2026, 10, 8))

    assert forecast.raw_output["months_used"] == ["2026-07", "2026-08", "2026-09"]
    assert forecast.raw_output["data_stale"] is False
    assert forecast.raw_output["fallback_reason"] is None
    assert len(prophet_spy) == 2
    # старий блок (по 50 000) не впливає на прогноз
    assert forecast.predicted_expenses < 5000


async def test_stale_after_last_transaction_uses_average_without_prophet(
    db_session, real_user, account, prophet_spy,
):
    expenses = {(2026, 5): 900, (2026, 6): 1000, (2026, 7): 2000, (2026, 8): 3000}
    await _months(db_session, real_user.id, account.id, _span((2026, 5), (2026, 8)), expense=expenses)
    await _txn(db_session, real_user.id, account.id, date(2026, 8, 26), -100, EXPENSE)
    await db_session.commit()

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2026, 10, 8))

    raw = forecast.raw_output
    assert raw["months_used"] == ["2026-06", "2026-07", "2026-08"]
    assert raw["data_stale"] is True
    assert raw["fallback_reason"] == "stale_data"
    assert raw["last_transaction_at"].startswith("2026-08-26")
    assert raw["data_stale_days"] == 43
    assert prophet_spy == []
    assert forecast.method == "average_fallback"
    assert forecast.low_confidence is True
    assert forecast.predicted_expenses == pytest.approx((1000 + 2000 + 3100) / 3, abs=0.01)


# --- Інші причини fallback і пріоритет -----------------------------------

async def test_insufficient_months_when_fresh(db_session, real_user, account, prophet_spy):
    expenses = {(2026, 12): 500, (2027, 1): 1000, (2027, 2): 3000, (2027, 3): 700}
    await _months(db_session, real_user.id, account.id, _span((2026, 12), (2027, 3)), expense=expenses)

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2027, 3, 15))

    raw = forecast.raw_output
    assert raw["months_used"] == ["2027-01", "2027-02"]
    assert raw["fallback_reason"] == "insufficient_months"
    assert raw["data_stale"] is False
    assert raw["partial_months_only"] is False
    assert prophet_spy == []
    assert forecast.predicted_expenses == 2000.0


async def test_partial_months_only_uses_average_of_partial_months(db_session, real_user, account, prophet_spy):
    expenses = {(2027, 2): 1000, (2027, 3): 3000}
    await _months(db_session, real_user.id, account.id, _span((2027, 2), (2027, 3)), expense=expenses)

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2027, 3, 15))

    raw = forecast.raw_output
    assert raw["months_used"] == ["2027-02", "2027-03"]
    assert raw["partial_months_only"] is True
    assert raw["fallback_reason"] == "partial_months_only"
    assert raw["data_stale"] is False
    assert prophet_spy == []
    assert forecast.predicted_expenses == 2000.0


async def test_priority_partial_over_stale(db_session, real_user, account):
    # Один місяць даних давно: немає повних місяців І дані застарілі.
    await _months(db_session, real_user.id, account.id, [(2026, 6)])

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2026, 10, 8))

    assert forecast.raw_output["fallback_reason"] == "partial_months_only"
    assert forecast.raw_output["data_stale"] is True


async def test_priority_stale_over_insufficient(db_session, real_user, account):
    # Повний лише липень (червень — перший): замало місяців І дані застарілі.
    await _months(db_session, real_user.id, account.id, _span((2026, 6), (2026, 7)))

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2026, 10, 8))

    assert forecast.raw_output["months_used"] == ["2026-07"]
    assert forecast.raw_output["fallback_reason"] == "stale_data"
    assert forecast.raw_output["data_stale"] is True


# --- Нулі всередині вікна ----------------------------------------------

async def test_month_without_expenses_inside_window_is_zero(db_session, real_user, account):
    expenses = {(2026, 12): 1000, (2027, 1): 1000, (2027, 2): None, (2027, 3): 1000}
    await _months(db_session, real_user.id, account.id, _span((2026, 12), (2027, 3)), expense=expenses)

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2027, 3, 15))

    assert forecast.raw_output["months_used"] == ["2027-01", "2027-02"]
    assert forecast.raw_output["series_actual"]["expenses"] == {"2027-01": 1000.0, "2027-02": 0.0}
    assert forecast.predicted_expenses == 500.0


# --- Горизонт Prophet = period_start --------------------------------------

async def test_prophet_forecasts_period_start_not_next_point(db_session, real_user, account):
    # Повні жовтень–лютий з лінійним трендом +1000/міс; поточний березень
    # неповний; ціль — квітень (period_start). «Наступна точка» ряду —
    # березень (≈6000), а ціль — квітень (≈7000).
    expenses = {(2026, 9): 500, (2027, 3): 100}
    expenses.update({ym: 1000 * (i + 1) for i, ym in enumerate(_span((2026, 10), (2027, 2)))})
    await _months(db_session, real_user.id, account.id, _span((2026, 9), (2027, 3)), expense=expenses)

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2027, 3, 15))

    assert forecast.period_start == date(2027, 4, 1)
    assert forecast.method == "prophet"
    assert forecast.predicted_expenses > 6500


# --- Викиди: вінзоризація MAD лише на вході моделі ------------------------

async def test_outlier_winsorized_in_model_input_but_actual_kept(db_session, real_user, account):
    full = [1000, 1100, 1050, 9000, 1020]
    expenses = {(2026, 9): 1000, (2027, 3): 1000}
    expenses.update(dict(zip(_span((2026, 10), (2027, 2)), full)))
    await _months(db_session, real_user.id, account.id, _span((2026, 9), (2027, 3)), expense=expenses)

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2027, 3, 15))

    raw = forecast.raw_output
    assert raw["series_actual"]["expenses"]["2027-01"] == 9000.0
    # median 1050, MAD 50 -> верхня межа 1050 + 3·1.4826·50 = 1272.39
    assert raw["series_model_input"]["expenses"]["2027-01"] == pytest.approx(1272.39, abs=0.01)
    for month, value in raw["series_actual"]["expenses"].items():
        if month != "2027-01":
            assert raw["series_model_input"]["expenses"][month] == value


async def test_mad_zero_leaves_model_input_unchanged(db_session, real_user, account):
    full = [1000, 1000, 1000, 5000]
    expenses = {(2026, 10): 1000, (2027, 3): 1000}
    expenses.update(dict(zip(_span((2026, 11), (2027, 2)), full)))
    await _months(db_session, real_user.id, account.id, _span((2026, 10), (2027, 3)), expense=expenses)

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2027, 3, 15))

    raw = forecast.raw_output
    assert raw["series_model_input"]["expenses"] == raw["series_actual"]["expenses"]


async def test_no_winsorization_below_three_points(db_session, real_user, account):
    expenses = {(2026, 12): 1000, (2027, 1): 1000, (2027, 2): 50000, (2027, 3): 1000}
    await _months(db_session, real_user.id, account.id, _span((2026, 12), (2027, 3)), expense=expenses)

    forecast = await generate_forecast(real_user.id, session=db_session, today=date(2027, 3, 15))

    raw = forecast.raw_output
    assert raw["series_model_input"]["expenses"] == raw["series_actual"]["expenses"]
    assert forecast.predicted_expenses == 25500.0


def test_median_three_sigma_rule_is_unreliable_documented():
    """Правило median ± 3σ знаходить викид при n=10 з одним сильним викидом, але не при n=6 і не при двох викидах."""
    def fires(values):
        s = pd.Series(values, dtype=float)
        return bool((s > s.median() + 3 * s.std()).any())

    assert fires([100] * 9 + [1000]) is True
    assert fires([51000, 65000, 57000, 60000, 55000, 150000]) is False
    assert fires([100] * 8 + [1000] * 2) is False


# --- Відповідь API ------------------------------------------------------

async def test_latest_exposes_window_fields(db_session, real_user, account):
    await _months(db_session, real_user.id, account.id, _span((2026, 5), (2026, 8)))

    await generate_forecast(real_user.id, session=db_session, today=date(2026, 10, 8))
    response = await latest(current_user=real_user, db=db_session)

    assert response["months_used"] == ["2026-06", "2026-07", "2026-08"]
    assert response["data_stale"] is True
    assert response["fallback_reason"] == "stale_data"
    assert response["data_stale_days"] == 58  # остання транзакція 11.08
    assert response["last_transaction_at"].startswith("2026-08-11")
    assert response["partial_months_only"] is False
    assert "months_basis" not in response
