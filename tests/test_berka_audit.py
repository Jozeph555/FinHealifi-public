"""
Тести агрегації місячних витрат для аудиту Berka (scripts/analysis/berka_audit.py).

На невеликих синтетичних фреймах, без БД і без вивантаженого датасету:
місяць без транзакцій усередині історії, кілька транзакцій в одному місяці,
рахунок з однією транзакцією, місяць лише з надходженнями, відкидання першого й
останнього місяця, пороги історії після відкидання, підсумок по рахунку.
"""
import pandas as pd
import pytest

from scripts.analysis.berka_audit import (
    account_summary,
    aggregate_monthly_expenses,
    drop_edge_months,
    history_report,
)


def _trans(rows):
    df = pd.DataFrame(rows, columns=["account_id", "date", "type", "amount"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _by_month(monthly, account_id):
    sub = monthly[monthly["account_id"] == account_id]
    return {str(m): row for m, row in zip(sub["month"], sub.to_dict("records"))}


def test_month_without_transactions_inside_history_is_zero():
    trans = _trans([
        (1, "1995-01-10", "VYDAJ", 100),
        (1, "1995-03-05", "VYDAJ", 300),
    ])
    months = _by_month(aggregate_monthly_expenses(trans, ["VYDAJ"]), 1)

    assert list(months) == ["1995-01", "1995-02", "1995-03"]
    feb = months["1995-02"]
    assert feb["expense"] == 0
    assert feb["n_tx"] == 0
    assert not feb["has_expense"]


def test_several_transactions_in_one_month_are_summed():
    trans = _trans([
        (1, "1995-01-01", "VYDAJ", 100),
        (1, "1995-01-15", "VYDAJ", 250),
        (1, "1995-01-31", "VYBER", 50),
        (1, "1995-01-20", "PRIJEM", 1000),
    ])
    jan = _by_month(aggregate_monthly_expenses(trans, ["VYDAJ", "VYBER"]), 1)["1995-01"]

    assert jan["expense"] == 400
    assert jan["n_expense_tx"] == 3
    assert jan["n_tx"] == 4


def test_account_with_single_transaction_has_one_month():
    trans = _trans([
        (1, "1995-01-01", "VYDAJ", 100),
        (1, "1995-04-01", "VYDAJ", 100),
        (2, "1996-07-19", "VYDAJ", 70),
    ])
    monthly = aggregate_monthly_expenses(trans, ["VYDAJ"])
    months = _by_month(monthly, 2)

    assert list(months) == ["1996-07"]
    assert months["1996-07"]["expense"] == 70
    assert len(monthly[monthly["account_id"] == 1]) == 4


def test_income_only_month_counts_as_history_without_expense():
    trans = _trans([
        (1, "1995-01-01", "PRIJEM", 1000),
        (1, "1995-02-03", "VYDAJ", 200),
    ])
    months = _by_month(aggregate_monthly_expenses(trans, ["VYDAJ"]), 1)

    assert months["1995-01"]["expense"] == 0
    assert months["1995-01"]["n_tx"] == 1
    assert not months["1995-01"]["has_expense"]


def test_expense_types_select_rows():
    trans = _trans([
        (1, "1995-01-01", "VYDAJ", 100),
        (1, "1995-01-02", "VYBER", 900),
    ])

    only_vydaj = aggregate_monthly_expenses(trans, ["VYDAJ"])
    assert only_vydaj["expense"].tolist() == [100]


def test_drop_edge_months_keeps_only_inner_months():
    trans = _trans([
        (1, "1995-01-20", "VYDAJ", 10),
        (1, "1995-02-05", "VYDAJ", 20),
        (1, "1995-04-30", "VYDAJ", 40),
        (2, "1996-01-01", "VYDAJ", 5),
        (2, "1996-02-01", "VYDAJ", 5),
        (3, "1997-03-01", "VYDAJ", 5),
    ])
    trimmed = drop_edge_months(aggregate_monthly_expenses(trans, ["VYDAJ"]))

    assert trimmed["account_id"].unique().tolist() == [1]
    assert trimmed["month"].astype(str).tolist() == ["1995-02", "1995-03"]
    assert trimmed["expense"].tolist() == [20, 0]


def test_history_report_thresholds_after_trimming():
    rows = [(1, f"1995-{m:02d}-01", "VYDAJ", 100) for m in range(1, 13)]
    rows += [(1, f"1996-{m:02d}-01", "VYDAJ", 100) for m in range(1, 13) if m != 6]
    trans = _trans(rows)
    monthly = aggregate_monthly_expenses(trans, ["VYDAJ"])
    report = history_report(
        account_summary(monthly), account_summary(drop_edge_months(monthly))
    ).set_index("metric")["value"]

    assert report["accounts_ge_24_months_full"] == 1
    assert report["accounts_ge_24_months_trimmed"] == 0
    assert report["zero_expense_months_trimmed"] == 1
    assert pd.isna(report["zero_month_share_ge_24"])


def test_account_summary_counts_zero_months_and_cv():
    trans = _trans([
        (1, "1995-01-01", "VYDAJ", 100),
        (1, "1995-03-01", "VYDAJ", 100),
    ])
    summary = account_summary(aggregate_monthly_expenses(trans, ["VYDAJ"])).iloc[0]

    assert summary["n_months"] == 3
    assert summary["n_zero_expense_months"] == 1
    assert summary["cv_nonzero_only"] == pytest.approx(0.0)
    assert summary["cv_with_zeros"] > 0.1
