"""
Аудит набору Kaggle computingvictor/transactions-fraud-datasets як кандидата для
бектесту прогнозу місячних витрат.

Основна БД FinHealifi не використовується. Дані — файли з data/external/caixabank_tx/
(див. README.md там же). cards_data.csv не читається: картки рахуються за card_id
транзакцій. З users_data.csv читаються лише id і yearly_income.

Режим (з кореня репозиторію):
    python -m scripts.analysis.caixabank_audit audit [--data-dir DIR] [--out-dir DIR]

Витрати — транзакції з amount > 0, сума за календарний місяць клієнта (client_id
виступає рахунком). Агрегація, відкидання першого й останнього місяця і підсумок
історії — ті самі функції, що в berka_audit.py. Результат друкується в консоль і
пишеться в --out-dir/summary.txt.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

from app.services.mcc_mapping import categorize_by_mcc
from scripts.analysis.berka_audit import (
    _sha256,
    account_summary,
    aggregate_monthly_expenses,
    drop_edge_months,
    history_report,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT / "data" / "external" / "caixabank_tx"
DEFAULT_OUT_DIR = ROOT / "scripts" / "analysis" / "out" / "caixabank_audit"

TRANS_COLUMNS = ["date", "client_id", "card_id", "amount", "mcc"]
EXPENSE_TYPE = "VYDAJ"
AUTOCORR_LAGS = (1, 2, 12)
HISTORY_LONG_THRESHOLD = 96
TOP_UNCOVERED_MCC = 5


def _parse_money(values: pd.Series) -> pd.Series:
    """Перетворює рядки виду «$-77.00» / «$1,234» на float."""
    return values.str.replace("$", "", regex=False).str.replace(",", "", regex=False).astype(float)


def load_trans(path: Path) -> pd.DataFrame:
    """Читає потрібні колонки transactions_data.csv.

    Returns:
        Фрейм account_id (= client_id), card_id, date (datetime), amount (float, $),
        mcc (int) і type (`VYDAJ` для amount > 0, інакше порожній рядок) — у форматі,
        який приймає aggregate_monthly_expenses.
    """
    df = pd.read_csv(path, usecols=TRANS_COLUMNS,
                     dtype={"client_id": "int32", "card_id": "int32", "mcc": "int32"})
    df = df.rename(columns={"client_id": "account_id"})
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d %H:%M:%S")
    df["amount"] = _parse_money(df["amount"])
    df["type"] = EXPENSE_TYPE
    df.loc[df["amount"] <= 0, "type"] = ""
    return df


def load_yearly_income(path: Path) -> pd.DataFrame:
    """Читає з users_data.csv лише id і yearly_income ($, float)."""
    df = pd.read_csv(path, usecols=["id", "yearly_income"])
    df["yearly_income"] = _parse_money(df["yearly_income"])
    return df


def overview(trans: pd.DataFrame) -> list[tuple[str, object]]:
    """Повертає кількість транзакцій, період, клієнтів, карток і MCC."""
    return [
        ("transactions", len(trans)),
        ("period_min", str(trans["date"].min())),
        ("period_max", str(trans["date"].max())),
        ("clients", int(trans["account_id"].nunique())),
        ("cards", int(trans["card_id"].nunique())),
        ("mcc", int(trans["mcc"].nunique())),
    ]


def negative_amounts(trans: pd.DataFrame) -> list[tuple[str, object]]:
    """Повертає частку й медіану від'ємних сум, мінімум і MCC, де їх найбільше."""
    neg = trans.loc[trans["amount"] < 0]
    top = neg["mcc"].value_counts().head(TOP_UNCOVERED_MCC)
    return [
        ("negative_share", round(len(neg) / len(trans), 4)),
        ("negative_median", float(neg["amount"].median())),
        ("negative_min", float(neg["amount"].min())),
        ("negative_top_mcc", ", ".join(f"{m}: {n}" for m, n in top.items())),
    ]


def mcc_coverage(trans: pd.DataFrame) -> list[tuple[str, object]]:
    """Повертає частку рядків і суми витрат без категорії від categorize_by_mcc і топ непокритих MCC.

    Частка рядків — від усіх транзакцій; частка суми — від суми amount > 0.
    """
    mapped = {m: categorize_by_mcc(int(m)) is not None for m in trans["mcc"].unique()}
    uncovered = ~trans["mcc"].map(mapped)
    positive = trans["amount"].clip(lower=0)
    top = trans.loc[uncovered, "mcc"].value_counts().head(TOP_UNCOVERED_MCC)
    return [
        ("mcc_uncovered_row_share", round(float(uncovered.mean()), 4)),
        ("mcc_uncovered_amount_share", round(float(positive[uncovered].sum() / positive.sum()), 4)),
        ("mcc_uncovered_top", ", ".join(f"{m}: {n}" for m, n in top.items())),
    ]


def autocorr_medians(monthly: pd.DataFrame, lags=AUTOCORR_LAGS) -> list[tuple[str, object]]:
    """Повертає медіану по клієнтах автокореляції місячних витрат для кожного лагу.

    Args:
        monthly: Місячні витрати (account_id, month, expense), впорядковані за місяцем.
        lags: Лаги в місяцях.
    """
    g = monthly.sort_values(["account_id", "month"]).groupby("account_id")["expense"]
    return [
        (f"autocorr_lag{k}_median",
         round(float(g.apply(lambda s, k=k: s.reset_index(drop=True).autocorr(k)).median()), 4))
        for k in lags
    ]


def variance_split(monthly: pd.DataFrame) -> list[tuple[str, object]]:
    """Ділить дисперсію місячних витрат на частку між клієнтами й усередині клієнта."""
    total = monthly["expense"].var(ddof=0)
    within = (monthly["expense"]
              - monthly.groupby("account_id")["expense"].transform("mean")).var(ddof=0)
    return [
        ("variance_between_clients_share", round(float(1 - within / total), 4)),
        ("variance_within_client_share", round(float(within / total), 4)),
    ]


def income_check(trans_header: list[str], trans: pd.DataFrame,
                 users: pd.DataFrame) -> list[tuple[str, object]]:
    """Повертає факти про дохід: повний заголовок транзакцій і кількість значень yearly_income на клієнта.

    Args:
        trans_header: Усі колонки transactions_data.csv.
        trans: Результат load_trans.
        users: Результат load_yearly_income.
    """
    clients = trans["account_id"].unique()
    per_client = users.groupby("id")["yearly_income"].nunique()
    return [
        ("trans_file_columns", ", ".join(trans_header)),
        ("users_rows", len(users)),
        ("users_max_yearly_income_values_per_client", int(per_client.max())),
        ("clients_with_yearly_income", int(users["id"].isin(clients).sum())),
    ]


def run_audit(data_dir: Path, out_dir: Path) -> None:
    """Виконує аудит, друкує показники і пише їх у out_dir/summary.txt."""
    trans_path = data_dir / "transactions_data.csv"
    trans = load_trans(trans_path)
    trans_header = list(pd.read_csv(trans_path, nrows=0).columns)
    users = load_yearly_income(data_dir / "users_data.csv")
    monthly = aggregate_monthly_expenses(trans, [EXPENSE_TYPE])
    trimmed = drop_edge_months(monthly)
    trimmed_summary = account_summary(trimmed)
    history = history_report(account_summary(monthly), trimmed_summary)
    history = history[history["metric"].isin(
        ["n_months_trimmed_q50", "accounts_ge_36_months_trimmed", "zero_expense_months_trimmed"])]
    long_history = int((trimmed_summary["n_months"] >= HISTORY_LONG_THRESHOLD).sum())

    sections = {
        "files_sha256": [(p.name, _sha256(p)) for p in sorted(data_dir.iterdir())
                         if p.is_file() and p.name != "README.md"],
        "overview": overview(trans),
        "history": list(history.itertuples(index=False, name=None))
                   + [(f"accounts_ge_{HISTORY_LONG_THRESHOLD}_months_trimmed", long_history)],
        "negative_amounts": negative_amounts(trans),
        "mcc_coverage": mcc_coverage(trans),
        "autocorrelation": autocorr_medians(trimmed),
        "variance": variance_split(trimmed),
        "income": income_check(trans_header, trans, users),
    }
    lines = []
    for name, rows in sections.items():
        lines.append(f"=== {name} ===")
        lines += [f"{metric}: {value}" for metric, value in rows]
        lines.append("")
    text = "\n".join(lines)
    print(text)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.txt").write_text(text, encoding="utf-8")
    print(f"Записано: {out_dir / 'summary.txt'}")


def main() -> None:
    """Розбирає аргументи командного рядка і запускає audit."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    a.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = p.parse_args()
    run_audit(args.data_dir, args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
