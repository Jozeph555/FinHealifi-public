"""
Вивантаження датасету Berka (PKDD'99 Financial) у CSV і аудит придатності для
бектесту прогнозу місячних витрат.

Основна БД FinHealifi не використовується. Джерело — публічний сервер MariaDB
(лише читання), доступ через клієнт `mysql` у пакетному режимі.

Формат CSV: роздільник «,», заголовок — назви стовпців, NULL записується як
`\\N`, порожній рядок — як порожнє поле. Рядки вивантажуються з ORDER BY за
первинним ключем, тож повторне вивантаження дає ті самі файли.

Режими (з кореня репозиторію):
    python -m scripts.analysis.berka_audit export [--data-dir DIR]
    python -m scripts.analysis.berka_audit audit [--data-dir DIR] [--out-dir DIR]
        [--expense-types=VYDAJ,VYBER]

`export` пише по CSV на таблицю бази `financial` і data/external/berka/README.md.
`audit` читає trans.csv, друкує опис полів і агрегати та пише CSV у --out-dir;
імена файлів мають суфікс з типів витрат (напр. `_VYDAJ+VYBER`).

Витрати — рядки з `type` у --expense-types (за замовчуванням VYDAJ і VYBER),
сума |amount| за календарний місяць. Місяць усередині історії без списань має
витрати 0. Перший і останній календарний місяць кожного рахунку відкидаються
як неповні; довжини історії «до відкидання» друкуються окремо.
"""
import argparse
import csv
import hashlib
import io
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT / "data" / "external" / "berka"
DEFAULT_OUT_DIR = ROOT / "scripts" / "analysis" / "out"

SOURCE = {
    "host": "relational.fel.cvut.cz",
    "port": "3306",
    "user": "guest",
    "password": "ctu-relational",
    "database": "financial",
}
SOURCE_PAGE = "https://relational.fel.cvut.cz/dataset/Financial"
NULL_TOKEN = "\\N"
DEFAULT_EXPENSE_TYPES = ("VYDAJ", "VYBER")
CV_THRESHOLD = 0.1
HISTORY_THRESHOLDS = (24, 36, 48)


def _mysql(sql: str) -> str:
    """Виконує SQL на джерелі через клієнт `mysql` і повертає сирий TSV (з заголовком)."""
    env = dict(os.environ, MYSQL_PWD=SOURCE["password"])
    cmd = [
        "mysql", "-h", SOURCE["host"], "-P", SOURCE["port"], "-u", SOURCE["user"],
        "--batch", "--raw", "--default-character-set=utf8mb4", SOURCE["database"], "-e", sql,
    ]
    return subprocess.run(cmd, env=env, check=True, capture_output=True, text=True).stdout


def _list_tables() -> list[str]:
    """Повертає назви таблиць бази джерела в алфавітному порядку."""
    out = _mysql(
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema = '{SOURCE['database']}' ORDER BY table_name"
    )
    return [line for line in out.splitlines()[1:] if line]


def _primary_key(table: str) -> list[str]:
    """Повертає стовпці первинного ключа таблиці; якщо ключа немає — усі стовпці."""
    out = _mysql(f"SHOW KEYS FROM `{table}` WHERE Key_name = 'PRIMARY'")
    rows = list(csv.DictReader(io.StringIO(out), delimiter="\t"))
    if rows:
        return [r["Column_name"] for r in sorted(rows, key=lambda r: int(r["Seq_in_index"]))]
    cols = _mysql(f"SHOW COLUMNS FROM `{table}`")
    return [r["Field"] for r in csv.DictReader(io.StringIO(cols), delimiter="\t")]


def export_table(table: str, path: Path) -> tuple[int, int]:
    """Вивантажує таблицю джерела в CSV.

    Args:
        table: Назва таблиці в базі `financial`.
        path: Куди записати CSV.

    Returns:
        (кількість рядків, кількість стовпців).
    """
    order = ", ".join(f"`{c}`" for c in _primary_key(table))
    raw = _mysql(f"SELECT * FROM `{table}` ORDER BY {order}")
    lines = raw.rstrip("\n").split("\n")
    header = lines[0].split("\t")
    n_rows = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        for line in lines[1:]:
            values = line.split("\t")
            if len(values) != len(header):
                raise ValueError(f"{table}: {len(values)} полів замість {len(header)}: {line!r}")
            writer.writerow([NULL_TOKEN if v == "NULL" else v for v in values])
            n_rows += 1
    return n_rows, len(header)


def _sha256(path: Path) -> str:
    """Повертає sha256 файлу у hex."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_readme(data_dir: Path, stats: list[tuple[str, int, int, str]], exported_on: date) -> None:
    """Записує README.md з джерелом, датою вивантаження і розміром та sha256 кожного CSV."""
    rows = "\n".join(f"| `{t}.csv` | {r} | {c} | `{h}` |" for t, r, c, h in stats)
    text = f"""# Датасет Berka (PKDD'99 Financial)

Дані в git не зберігаються (див. `.gitignore`), тут лише опис вивантаження.

- Джерело: публічний сервер MariaDB `{SOURCE['host']}:{SOURCE['port']}`, база `{SOURCE['database']}`,
  користувач `{SOURCE['user']}` (лише читання). Опис: {SOURCE_PAGE}
- Ліцензія: на сторінці джерела не вказана.
- Дата вивантаження: {exported_on.isoformat()}
- Скрипт: `python -m scripts.analysis.berka_audit export`
- Формат: CSV, UTF-8, заголовок у першому рядку; NULL — `\\N`, порожній рядок — порожнє поле;
  рядки впорядковані за первинним ключем.

## Розбіжність опису і даних: `VYBER` у `trans.type`

У гайді Berka (Guide to the Financial Data Set) `VYBER` описано лише як значення `operation`
(«зняття готівки»); для `type` описано тільки `PRIJEM` (надходження) і `VYDAJ` (списання).
У даних `VYBER` є й у `type`: 16 666 рядків, усі з `operation = VYBER`; баланс після них
лише зменшується (у 0 % рядків попередній баланс + amount = баланс, у 72,5 % попередній
баланс − amount = баланс; решта не перевіряється через невідомий порядок транзакцій
усередині дня).

Опис полів звірено за вторинним джерелом
(https://webpages.charlotte.edu/mirsad/itcs6265/group1/transaction_domain.html):
сторінка гайду на is.vsfs.cz на момент вивантаження відкривалась лише через анти-бот JS-перевірку.

Аудит (`python -m scripts.analysis.berka_audit audit`) за замовчуванням вважає витратами
`type` ∈ {{`VYDAJ`, `VYBER`}}; для перевірки чутливості — `--expense-types=VYDAJ`.

| Файл | Рядків (без заголовка) | Стовпців | sha256 |
|---|---:|---:|---|
{rows}
"""
    (data_dir / "README.md").write_text(text, encoding="utf-8")


def run_export(data_dir: Path) -> None:
    """Вивантажує всі таблиці бази в data_dir і записує README.md."""
    data_dir.mkdir(parents=True, exist_ok=True)
    stats = []
    for table in _list_tables():
        path = data_dir / f"{table}.csv"
        n_rows, n_cols = export_table(table, path)
        stats.append((table, n_rows, n_cols, _sha256(path)))
        print(f"{table}: {n_rows} рядків, {n_cols} стовпців")
    write_readme(data_dir, stats, date.today())


def load_trans(path: Path) -> pd.DataFrame:
    """Читає trans.csv: `\\N` -> NaN, порожні рядки лишаються порожніми рядками, date -> datetime."""
    df = pd.read_csv(path, keep_default_na=False, na_values=[NULL_TOKEN], low_memory=False)
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d")
    return df


def aggregate_monthly_expenses(trans: pd.DataFrame, expense_types) -> pd.DataFrame:
    """Агрегує витрати по рахунку за календарний місяць.

    Історія рахунку — усі місяці від першої до останньої транзакції рахунку будь-якого
    типу включно. Місяць історії без жодної витратної транзакції отримує expense = 0 і
    has_expense = False, щоб його можна було трактувати і як нуль, і як пропуск.

    Args:
        trans: Фрейм зі стовпцями account_id, date (datetime), type, amount.
        expense_types: Значення `type`, які вважаються витратами.

    Returns:
        Фрейм account_id, month (Period[M]), expense (сума |amount|), n_expense_tx,
        has_expense, n_tx (усі транзакції місяця).
    """
    df = trans[["account_id", "date", "type", "amount"]].copy()
    df["month"] = df["date"].dt.to_period("M")
    is_exp = df["type"].isin(list(expense_types))
    df["expense"] = df["amount"].abs().where(is_exp, 0)
    df["is_exp"] = is_exp.astype(int)

    observed = (
        df.groupby(["account_id", "month"])
        .agg(expense=("expense", "sum"), n_expense_tx=("is_exp", "sum"), n_tx=("amount", "size"))
    )
    bounds = df.groupby("account_id")["month"].agg(["min", "max"])
    full_index = pd.MultiIndex.from_tuples(
        [
            (acc, m)
            for acc, (lo, hi) in bounds.iterrows()
            for m in pd.period_range(lo, hi, freq="M")
        ],
        names=["account_id", "month"],
    )
    out = observed.reindex(full_index, fill_value=0).reset_index()
    out["has_expense"] = out["n_expense_tx"] > 0
    return out[["account_id", "month", "expense", "n_expense_tx", "has_expense", "n_tx"]]


def drop_edge_months(monthly: pd.DataFrame) -> pd.DataFrame:
    """Відкидає перший і останній календарний місяць історії кожного рахунку.

    Рахунок з однією чи двома місяцями історії зникає повністю.

    Args:
        monthly: Результат aggregate_monthly_expenses.

    Returns:
        Той самий формат без крайніх місяців.
    """
    df = monthly.sort_values(["account_id", "month"])
    g = df.groupby("account_id")["month"]
    first, last = g.transform("min"), g.transform("max")
    return df[(df["month"] != first) & (df["month"] != last)].reset_index(drop=True)


def _cv(values: pd.Series) -> float:
    """Коефіцієнт варіації (std з ddof=0 / mean); NaN, якщо середнє 0."""
    mean = values.mean()
    return float(values.std(ddof=0) / mean) if mean else float("nan")


def account_summary(monthly: pd.DataFrame) -> pd.DataFrame:
    """Повертає по рахунку: місяці історії, місяці без витрат, CV витрат (з нулями і без них)."""
    g = monthly.groupby("account_id")
    return pd.DataFrame({
        "n_months": g.size(),
        "n_zero_expense_months": g["has_expense"].apply(lambda s: int((~s).sum())),
        "cv_with_zeros": g["expense"].apply(_cv),
        "cv_nonzero_only": monthly[monthly["has_expense"]].groupby("account_id")["expense"].apply(_cv),
    }).reset_index()


def describe_trans(trans: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Складає таблиці опису trans: стовпці/типи/NULL, діапазон дат, частоти категорій, amount."""
    n = len(trans)
    columns = pd.DataFrame({
        "column": trans.columns,
        "dtype": [str(t) for t in trans.dtypes],
        "null_pct": [round(trans[c].isna().mean() * 100, 4) for c in trans.columns],
        "empty_string_pct": [
            round((trans[c] == "").mean() * 100, 4) if trans[c].dtype == object else 0.0
            for c in trans.columns
        ],
    })
    freqs = []
    for col in ("type", "operation", "k_symbol"):
        vc = trans[col].fillna("<NULL>").replace("", "<порожньо>").value_counts()
        freqs.append(pd.DataFrame({"column": col, "value": vc.index, "rows": vc.values}))
    freq = pd.concat(freqs, ignore_index=True)
    amount = trans.groupby("type")["amount"].agg(["count", "min", "median", "max"]).reset_index()
    amount.loc[len(amount)] = ["<усі>", n, trans["amount"].min(), trans["amount"].median(),
                               trans["amount"].max()]
    for col, pred in (("negative_rows", lambda s: s < 0), ("zero_rows", lambda s: s == 0)):
        amount[col] = [
            int(pred(trans.loc[trans["type"] == t, "amount"]).sum()) if t != "<усі>"
            else int(pred(trans["amount"]).sum())
            for t in amount["type"]
        ]
    type_op = (
        trans.assign(operation=trans["operation"].fillna("<NULL>").replace("", "<порожньо>"))
        .groupby(["type", "operation"]).size().rename("rows").reset_index()
    )
    dates = pd.DataFrame({
        "metric": ["rows", "date_min", "date_max", "accounts"],
        "value": [n, trans["date"].min().date(), trans["date"].max().date(),
                  trans["account_id"].nunique()],
    })
    return {"columns": columns, "freq": freq, "amount": amount, "type_operation": type_op,
            "overview": dates}


def history_report(full: pd.DataFrame, trimmed: pd.DataFrame) -> pd.DataFrame:
    """Складає підсумок по рахунках до і після відкидання крайніх місяців.

    Args:
        full: account_summary до відкидання першого й останнього місяця.
        trimmed: account_summary після відкидання (рахунки без місяців у ньому відсутні).

    Returns:
        Фрейм metric/value: розподіл довжини історії, кількість рахунків на порогах,
        нульові місяці (усього і на кожному порозі) та частка рахунків із CV < порога.
        Метрики нулів і CV рахуються після відкидання.
    """
    quantiles = [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]
    trimmed = (
        trimmed.set_index("account_id")
        .reindex(full["account_id"])
        .fillna({"n_months": 0, "n_zero_expense_months": 0})
        .reset_index()
    )
    rows = [("accounts", len(full))]
    for label, s in (("full", full), ("trimmed", trimmed)):
        rows += [(f"n_months_{label}_q{int(p * 100)}", v)
                 for p, v in s["n_months"].quantile(quantiles).items()]
        rows += [(f"accounts_ge_{t}_months_{label}", int((s["n_months"] >= t).sum()))
                 for t in HISTORY_THRESHOLDS]
    rows += [
        ("months_trimmed_total", int(trimmed["n_months"].sum())),
        ("zero_expense_months_trimmed", int(trimmed["n_zero_expense_months"].sum())),
        ("accounts_with_zero_expense_month_trimmed",
         int((trimmed["n_zero_expense_months"] > 0).sum())),
    ]
    for t in HISTORY_THRESHOLDS:
        sub = trimmed[trimmed["n_months"] >= t]
        months = sub["n_months"].sum()
        rows += [
            (f"zero_month_share_ge_{t}",
             round(float(sub["n_zero_expense_months"].sum() / months), 4) if months else None),
            (f"accounts_with_zero_month_share_ge_{t}",
             round(float((sub["n_zero_expense_months"] > 0).mean()), 4) if len(sub) else None),
        ]
    rows += [
        (f"share_cv_with_zeros_lt_{CV_THRESHOLD}",
         round(float((trimmed["cv_with_zeros"] < CV_THRESHOLD).mean()), 4)),
        (f"share_cv_nonzero_only_lt_{CV_THRESHOLD}",
         round(float((trimmed["cv_nonzero_only"] < CV_THRESHOLD).mean()), 4)),
    ]
    return pd.DataFrame(rows, columns=["metric", "value"])


def run_audit(data_dir: Path, out_dir: Path, expense_types) -> None:
    """Виконує аудит trans.csv, друкує результати і пише CSV у out_dir."""
    trans = load_trans(data_dir / "trans.csv")
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = "+".join(expense_types)
    tables = describe_trans(trans)
    monthly = aggregate_monthly_expenses(trans, expense_types)
    trimmed = drop_edge_months(monthly)
    summary = account_summary(trimmed)
    tables["history"] = history_report(account_summary(monthly), summary)
    for name, df in tables.items():
        print(f"\n=== {name} ===")
        print(df.to_string(index=False))
        df.to_csv(out_dir / f"berka_audit_{name}_{tag}.csv", index=False)
    trimmed.assign(month=trimmed["month"].astype(str)).to_csv(
        out_dir / f"berka_monthly_expenses_{tag}.csv", index=False)
    summary.to_csv(out_dir / f"berka_account_summary_{tag}.csv", index=False)
    print(f"\nВитрати: type in {list(expense_types)}. CSV: {out_dir}")


def main() -> None:
    """Розбирає аргументи командного рядка і запускає export або audit."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    a = sub.add_parser("audit")
    a.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    a.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    a.add_argument("--expense-types", nargs="+", default=[",".join(DEFAULT_EXPENSE_TYPES)],
                   help="значення type через кому або пробіл, напр. --expense-types=VYDAJ")
    args = p.parse_args()
    if args.cmd == "export":
        run_export(args.data_dir)
    else:
        types = [t for v in args.expense_types for t in v.split(",") if t]
        run_audit(args.data_dir, args.out_dir, types)


if __name__ == "__main__":
    sys.exit(main())
