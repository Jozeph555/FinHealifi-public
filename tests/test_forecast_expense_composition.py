"""
Тести складу місячних витрат для прогнозу.

Витрати рахуються з усіх відтоків; віднімаються лише розпізнані переміщення
грошей (Заощадження/Інвестиції) і платежі за боргами з таблиці debts у межах їхніх
мінімальних платежів. Категорії — лише для розбивки.

- Готівка (зняття в банкоматі) — витрата.
- pending з MCC != 4829 або без MCC — витрата повністю, без неттінгу.
- category IS NULL — те саме, що pending.
- pending з MCC 4829 (P2P) — місячний неттінг max(0, відтік - надходження);
  P2P-надходження доходом не є.
- «Борги та кредити» — віднімається min(відтік категорії, сума мінімальних
  платежів активних боргів); мінімум revolving = max(4 % балансу, 200),
  installment — Debt.min_payment.

Функції викликаються з переданою db_session, щоб бачити незакомічені дані.
"""
import uuid
from datetime import date, datetime, timezone

import pytest

from app.models.account import Account
from app.models.debt import Debt
from app.models.transaction import Transaction
from app.services.forecasting import _load_monthly_series, generate_forecast
from app.services.advisor_context import build_advisor_context
from app.services.llm_advisor import _format_context
from app.api.forecast import latest


def _ts(year, month, day):
    return int(datetime(year, month, day, 12, tzinfo=timezone.utc).timestamp())


async def _make_account(db_session, user_id):
    account = Account(id=f"monobank_api-{uuid.uuid4()}", user_id=user_id, source_type="monobank_api")
    db_session.add(account)
    await db_session.flush()
    return account


async def _txn(db_session, user_id, account_id, time, amount_uah, category, mcc=None, method="mcc"):
    txn = Transaction(
        id=str(uuid.uuid4()),
        user_id=user_id,
        account_id=account_id,
        time=time,
        time_precision="second",  # акаунт тесту — monobank_api
        amount=amount_uah * 100,
        amount_uah=amount_uah,
        mcc=mcc,
        category=category,
        category_method="pending" if category in ("pending", None) else method,
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


async def _debt(db_session, user_id, debt_type, balance, min_payment=None, is_active=True, rate=0.0):
    debt = Debt(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name=f"Борг {uuid.uuid4().hex[:6]}",
        balance=balance,
        monthly_rate=rate,
        min_payment=min_payment,
        debt_type=debt_type,
        is_active=is_active,
    )
    db_session.add(debt)
    await db_session.flush()
    return debt


async def _expenses_by_month(db_session, user_id):
    df = await _load_monthly_series(user_id, income=False, session=db_session)
    if df.empty:
        return {}
    return dict(zip(df["ds"].dt.strftime("%Y-%m"), df["y"]))


@pytest.fixture
async def account(db_session, real_user):
    return await _make_account(db_session, real_user.id)


# --- Готівка -------------------------------------------------------------

async def test_cash_withdrawal_counts_as_expense(db_session, real_user, account):
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 5), -1000, "Готівка", mcc=6011)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 6), -500, "Продукти", mcc=5411)

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 1500.0}


# --- pending: мерчанти ---------------------------------------------------

async def test_pending_merchant_outflow_counts_fully(db_session, real_user, account):
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 5), -800, "pending", mcc=5699)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 6), -200, "Продукти", mcc=5411)

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 1000.0}


async def test_pending_without_mcc_counts_fully_without_netting(db_session, real_user, account):
    # Рядок без MCC — витрата повністю; надходження без MCC цей відтік не
    # зменшує.
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 5), -500, "pending", mcc=None)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 7), 500, "pending", mcc=None)

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 500.0}


async def test_null_category_counts_as_pending(db_session, real_user, account):
    # Рядок без категорії (category IS NULL) — як pending: без MCC — мерчант
    # (повністю), MCC 4829 — P2P-неттінг.
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 5), -700, None, mcc=None)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 6), -400, None, mcc=4829)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 7), 100, None, mcc=4829)

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 1000.0}


# --- pending: P2P, місячний неттінг ---------------------------------------

async def test_pending_p2p_outflow_netted_against_inflow_same_month(db_session, real_user, account):
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 5), -200, "Продукти", mcc=5411)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 10), -1000, "pending", mcc=4829)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 12), 300, "pending", mcc=4829)

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 900.0}


async def test_pending_p2p_net_is_floored_at_zero(db_session, real_user, account):
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 5), -200, "Продукти", mcc=5411)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 10), -300, "pending", mcc=4829)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 12), 1000, "pending", mcc=4829)

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 200.0}


async def test_pending_p2p_pair_across_month_boundary_is_not_netted(db_session, real_user, account):
    # Неттінг діє лише в межах календарного місяця.
    await _txn(db_session, real_user.id, account.id, _ts(2026, 7, 31), -1000, "pending", mcc=4829)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 7, 20), -100, "Продукти", mcc=5411)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 1), 1000, "pending", mcc=4829)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 20), -100, "Продукти", mcc=5411)

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-07": 1100.0, "2026-08": 100.0}


async def test_pending_p2p_inflow_is_not_income(db_session, real_user, account):
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 12), 1000, "pending", mcc=4829)

    df = await _load_monthly_series(real_user.id, income=True, session=db_session)
    assert len(df) == 0


# --- Борги та кредити ----------------------------------------------------

async def test_debt_payment_deducted_up_to_installment_min_payment(db_session, real_user, account):
    await _debt(db_session, real_user.id, "installment", 18032, min_payment=2254)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 4), -2254, "Борги та кредити", method="keyword")
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 6), -1000, "Продукти", mcc=5411)

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 1000.0}


async def test_debt_deduction_capped_by_sum_of_min_payments(db_session, real_user, account):
    await _debt(db_session, real_user.id, "installment", 50000, min_payment=2000)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 4), -2254, "Борги та кредити", method="keyword")
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 6), -1000, "Продукти", mcc=5411)

    # 254 грн платежів понад мінімуми лишаються витратою
    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 1254.0}


@pytest.mark.parametrize(
    "balance, expected_min",
    [
        (10000, 400.0),  # 4% · 10000
        (3000, 200.0),   # max(120, 200)
    ],
)
async def test_revolving_min_payment_uses_milp_formula(db_session, real_user, account, balance, expected_min):
    await _debt(db_session, real_user.id, "revolving", balance, min_payment=None, rate=0.03)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 4), -1000, "Борги та кредити", method="keyword")
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 6), -100, "Продукти", mcc=5411)

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 1100.0 - expected_min}


async def test_inactive_debts_are_not_deducted(db_session, real_user, account):
    await _debt(db_session, real_user.id, "installment", 18032, min_payment=2254, is_active=False)
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 4), -2254, "Борги та кредити", method="keyword")

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 2254.0}


async def test_debt_category_without_debts_stays_expense(db_session, real_user, account):
    await _txn(db_session, real_user.id, account.id, _ts(2026, 8, 4), -2254, "Борги та кредити", method="keyword")

    assert await _expenses_by_month(db_session, real_user.id) == {"2026-08": 2254.0}


# --- Розбивка у Forecast.raw_output і у відповіді API --------------------

async def test_forecast_exposes_breakdown_and_uncategorized_fields(db_session, real_user, account):
    # Прогноз береться з повних місяців останнього суцільного блоку (перший
    # місяць блоку й поточний — неповні). Дані за червень–серпень і today у
    # вересні: повні липень і серпень, 2 місяці -> середнє без Prophet.
    await _debt(db_session, real_user.id, "installment", 50000, min_payment=2000)
    uid, acc = real_user.id, account.id
    for month in (6, 7, 8):
        await _txn(db_session, uid, acc, _ts(2026, month, 3), -1000, "Продукти", mcc=5411)
        await _txn(db_session, uid, acc, _ts(2026, month, 4), -500, "Готівка", mcc=6011)
        await _txn(db_session, uid, acc, _ts(2026, month, 5), -300, "pending", mcc=5699)
        await _txn(db_session, uid, acc, _ts(2026, month, 6), -400, "pending", mcc=4829)
        await _txn(db_session, uid, acc, _ts(2026, month, 7), 100, "pending", mcc=4829)
        await _txn(db_session, uid, acc, _ts(2026, month, 8), -2254, "Борги та кредити", method="keyword")
    await db_session.commit()

    forecast = await generate_forecast(uid, session=db_session, today=date(2026, 9, 15))

    # 1000 + 500 + 300 + max(0, 400-100) + (2254 - min(2254, 2000)) = 2354
    assert forecast.predicted_expenses == 2354.0

    month = {m["month"]: m for m in forecast.raw_output["expense_breakdown"]["months"]}["2026-08"]
    assert month["month"] == "2026-08"
    assert month["cash"] == 500.0
    assert month["pending_merchant"] == 300.0
    assert month["pending_p2p_out"] == 400.0
    assert month["pending_p2p_in"] == 100.0
    assert month["pending_p2p_net"] == 300.0
    assert month["debt_category_out"] == 2254.0
    assert month["debt_deduction"] == 2000.0
    assert month["total"] == 2354.0
    # expense_breakdown — діагностика по всій історії: 3 місяці по 1000
    assert forecast.raw_output["expense_breakdown"]["by_category"]["Продукти"] == 3000.0

    response = await latest(current_user=real_user, db=db_session)
    assert response["months_used"] == ["2026-07", "2026-08"]
    assert "months_basis" not in response
    assert response["uncategorized_outflow_avg_per_month"] == 700.0
    assert response["uncategorized_inflow_avg_per_month"] == 100.0
    assert response["debt_payments_outside_list_avg_per_month"] == 254.0


async def test_breakdown_components_sum_to_monthly_total(db_session, real_user, account):
    """Для кожного місяця categorized + pending_merchant + pending_p2p_net - debt_deduction == total == y ряду."""
    await _debt(db_session, real_user.id, "revolving", 10000, rate=0.03)
    await _debt(db_session, real_user.id, "installment", 18032, min_payment=2254)
    uid, acc = real_user.id, account.id
    for month in (6, 7, 8):
        await _txn(db_session, uid, acc, _ts(2026, month, 2), -1000 - month, "Продукти", mcc=5411)
        await _txn(db_session, uid, acc, _ts(2026, month, 3), -300 * month, "Готівка", mcc=6011)
        await _txn(db_session, uid, acc, _ts(2026, month, 4), -2254, "Борги та кредити", method="keyword")
        await _txn(db_session, uid, acc, _ts(2026, month, 5), -250, "pending", mcc=5699)
        await _txn(db_session, uid, acc, _ts(2026, month, 6), -90, None, mcc=None)
        await _txn(db_session, uid, acc, _ts(2026, month, 7), -100 * month, "pending", mcc=4829)
        await _txn(db_session, uid, acc, _ts(2026, month, 8), 650, "pending", mcc=4829)
        await _txn(db_session, uid, acc, _ts(2026, month, 9), -500, "Заощадження", method="keyword")
    await db_session.commit()

    forecast = await generate_forecast(uid, session=db_session)
    series = await _expenses_by_month(db_session, uid)
    months = forecast.raw_output["expense_breakdown"]["months"]

    assert [m["month"] for m in months] == sorted(series)
    for m in months:
        components = m["categorized"] + m["pending_merchant"] + m["pending_p2p_net"] - m["debt_deduction"]
        assert components == pytest.approx(m["total"])
        assert m["total"] == pytest.approx(series[m["month"]])


# --- Готівка в раднику: окремий рядок, а не найбільша категорія ------------

async def test_advisor_context_has_no_categories_or_cash(
    db_session, real_user, account,
):
    from datetime import timedelta

    recent = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp())
    await _txn(db_session, real_user.id, account.id, recent, -20000, "Готівка", mcc=6011)
    await _txn(db_session, real_user.id, account.id, recent + 60, -900, "Продукти", mcc=5411)
    await _txn(db_session, real_user.id, account.id, recent + 120, -5000, "pending", mcc=5699)
    await db_session.commit()

    text = _format_context(await build_advisor_context(real_user.id, session=db_session))

    assert "Продукти" not in text
    assert "готівк" not in text.lower()
    assert "20 000" not in text and "20000" not in text
