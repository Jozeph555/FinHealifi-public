"""
Бектест прогнозу бюджету: Prophet проти простих базових моделей.

Лише читає БД; прогнози, плани й інші записи не створюються.

Побудова рядів без витоку майбутнього:
- один прохід у транзакції лише для читання (SET TRANSACTION READ ONLY,
  наприкінці ROLLBACK): _load_monthly_series(income=True),
  _load_expense_breakdown і _load_coverage з forecasting.py на всій історії;
- на точці t (місяць t завершено) беруться лише місяці <= t: вікно —
  _select_window(coverage[<= t], today = перше число місяця t+1). Кожен
  місячний агрегат залежить лише від рядків свого місяця, тож зріз дорівнює
  запуску тих самих функцій на даних, обрізаних по кінцю t;
- фактичне значення цілі — з тих самих рядів.

Горизонти: A — ціль t+2 (today у місяці t+1, period_start = t+2), B — ціль
t+1. Моделі: naive, mean3, mean6, median6, window_mean — на сирому ряді вікна
і (суфікс _w) після _winsorize_mad; prophet — як _generate_forecast
(_winsorize_mad -> _run_prophet; при fallback_reason — середнє вікна).

debt_deduction використовує поточні мінімальні платежі активних боргів для
всієї історії, як і прогноз.

Запуск (з кореня репозиторію; основна БД — лише читання):
    ALLOW_MAIN_DB=1 python -m scripts.analysis.forecast_backtest \\
        --expect-db-port 5436 --user-id <id> [--today YYYY-MM-DD] [--out-dir DIR]
Коди виходу: 0 — успіх; 2 — відмова перевірки цілі БД.
"""
import argparse
import asyncio
import csv
import logging
import math
import os
import statistics
import sys
import tempfile
import time
from datetime import date

import pandas as pd
from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from app.models.database import AsyncSessionLocal, engine
from app.services.forecasting import (
    _load_coverage,
    _load_expense_breakdown,
    _load_monthly_series,
    _month_key,
    _run_prophet,
    _select_window,
    _winsorize_mad,
)
from scripts.data_fixes._db_guard import DbGuardRefused, check_db_target

REGIME_SHIFT = "2026-01"
MIN_POINTS_PER_SIDE = 4
HORIZONS = {"A": 2, "B": 1}  # місяців від точки t до цілі
SIMPLE_BASES = ["naive", "mean3", "mean6", "median6", "window_mean"]
SIMPLE_MODELS = SIMPLE_BASES + [f"{m}_w" for m in SIMPLE_BASES if m != "naive"]
MODELS = SIMPLE_MODELS + ["prophet"]


# --- ряди: дані з БД -> чисті структури ------------------------------------------

async def load_series(session, user_id: str) -> dict:
    """Завантажує ряди доходу, витрат і покриття на всій історії (лише SELECT).

    Returns:
        {"income": {YYYY-MM: грн}, "expenses": {...}, "coverage": [date]}.
    """
    income_df = await _load_monthly_series(user_id, income=True, session=session)
    breakdown = await _load_expense_breakdown(user_id, session=session)
    coverage, _ = await _load_coverage(session, user_id)
    return {
        "income": {_month_key(ds): float(y) for ds, y in zip(income_df["ds"], income_df["y"])},
        "expenses": {m["month"]: float(m["total"]) for m in breakdown["months"]},
        "coverage": coverage,
    }


def window_at(series: dict, origin: date) -> dict:
    """Повертає вікно прогнозу станом на завершений місяць origin (today — перше число наступного місяця)."""
    origin = origin.replace(day=1)
    coverage = [m for m in series["coverage"] if m <= origin]
    window = _select_window(coverage, origin + relativedelta(months=1))
    months = [_month_key(m) for m in window["months"]]
    return {
        "months": months,
        "fallback_reason": window["fallback_reason"],
        "income": [series["income"].get(m, 0.0) for m in months],
        "expenses": [series["expenses"].get(m, 0.0) for m in months],
    }


# --- моделі ---------------------------------------------------------------------

def simple_forecasts(values: list[float]) -> dict:
    """Рахує прогнози простих моделей на ряді вікна; варіанти з суфіксом _w — після _winsorize_mad."""
    def base(v):
        """Прогнози naive, mean3, mean6, median6 і window_mean для ряду."""
        return {
            "naive": v[-1],
            "mean3": statistics.fmean(v[-3:]),
            "mean6": statistics.fmean(v[-6:]),
            "median6": statistics.median(v[-6:]),
            "window_mean": statistics.fmean(v),
        }

    raw = base(values)
    winsorized = base(_winsorize_mad(values))
    return {**raw, **{f"{k}_w": v for k, v in winsorized.items() if k != "naive"}}


def prophet_forecast(months: list[str], values: list[float], fallback_reason, target: date):
    """Прогнозує як _generate_forecast: при fallback — середнє вікна, інакше _winsorize_mad -> _run_prophet.

    Returns:
        Трійка (прогноз, секунди, подія); при збої Prophet прогноз — NaN.
    """
    if fallback_reason is not None:
        return sum(values) / len(values) if values else 0.0, 0.0, f"fallback:{fallback_reason}"
    df = pd.DataFrame({"ds": pd.to_datetime(months, format="%Y-%m"), "y": _winsorize_mad(values)})
    started = time.perf_counter()
    try:
        prediction = _run_prophet(df, target)
    except Exception as e:  # прогноз тут кидає RuntimeError; бектест фіксує збій
        return math.nan, time.perf_counter() - started, f"error:{type(e).__name__}"
    return prediction, time.perf_counter() - started, ""


# --- бектест --------------------------------------------------------------------

def origins(series: dict, today: date, horizon: int) -> list[date]:
    """Повертає точки відліку бектесту.

    Від першої точки, де вікно дає Prophet (щонайменше MIN_MONTHS_FOR_PROPHET
    повних місяців), до останньої, ціль якої — завершений місяць.
    """
    last_full = today.replace(day=1) - relativedelta(months=1)
    result = []
    if not series["coverage"]:
        return result
    month = series["coverage"][0]
    while month + relativedelta(months=horizon) <= last_full:
        if window_at(series, month)["fallback_reason"] is None:
            result.append(month)
        month += relativedelta(months=1)
    return result


def run_backtest(series: dict, today: date, horizon: int, prophet=True) -> list[dict]:
    """Рахує прогнози всіх моделей у кожній точці відліку; рядок на (точка, модель)."""
    rows = []
    for origin in origins(series, today, horizon):
        window = window_at(series, origin)
        target = origin + relativedelta(months=horizon)
        target_key = _month_key(target)
        actual = {
            "income": series["income"].get(target_key, 0.0),
            "expenses": series["expenses"].get(target_key, 0.0),
        }
        preds = {name: simple_forecasts(window[name]) for name in ("income", "expenses")}
        events, seconds = [], 0.0
        if prophet:
            for name in ("income", "expenses"):
                p, s, event = prophet_forecast(window["months"], window[name], window["fallback_reason"], target)
                preds[name]["prophet"] = p
                seconds += s
                if event:
                    events.append(f"{name}:{event}")
        for model in preds["income"]:
            rows.append({
                "origin": _month_key(origin),
                "target": target_key,
                "window_months": len(window["months"]),
                "model": model,
                "income_pred": preds["income"][model],
                "income_actual": actual["income"],
                "expenses_pred": preds["expenses"][model],
                "expenses_actual": actual["expenses"],
                "fcf_pred": preds["income"][model] - preds["expenses"][model],
                "fcf_actual": actual["income"] - actual["expenses"],
                "prophet_seconds": seconds if model == "prophet" else "",
                "events": ";".join(events) if model == "prophet" else "",
            })
    return rows


def _smape(pred, actual):
    """Симетрична відносна похибка; 0, якщо прогноз і факт нульові."""
    denominator = (abs(pred) + abs(actual)) / 2
    return 0.0 if denominator == 0 else abs(pred - actual) / denominator


def _sign(x):
    """Знак числа: -1, 0 або 1."""
    return (x > 0) - (x < 0)


def summarize(rows: list[dict]) -> dict:
    """Зводить метрики моделей: MAE, bias і sMAPE для доходу, витрат і FCF, точність знаку FCF.

    Точки, де прогнозу моделі немає (NaN — збій Prophet), виключаються й рахуються окремо.
    """
    summary = {}
    for model in dict.fromkeys(r["model"] for r in rows):
        model_rows = [r for r in rows if r["model"] == model and not math.isnan(r["fcf_pred"])]
        stats = {"n": len(model_rows)}
        for target in ("income", "expenses", "fcf"):
            errors = [r[f"{target}_pred"] - r[f"{target}_actual"] for r in model_rows]
            stats[target] = {
                "mae": statistics.fmean(abs(e) for e in errors) if errors else math.nan,
                "bias": statistics.fmean(errors) if errors else math.nan,
                "smape": statistics.fmean(_smape(r[f"{target}_pred"], r[f"{target}_actual"]) for r in model_rows)
                if model_rows else math.nan,
            }
        stats["fcf_sign_acc"] = (
            statistics.fmean(_sign(r["fcf_pred"]) == _sign(r["fcf_actual"]) for r in model_rows)
            if model_rows else math.nan
        )
        summary[model] = stats
    return summary


def criterion(summary: dict) -> dict:
    """Застосовує критерій вибору моделі, заданий до запуску.

    Prophet лишається, лише якщо MAE вільного потоку не більший за 0.9 * MAE
    найкращої простої моделі і |bias| не гірший.
    """
    best = min(SIMPLE_MODELS, key=lambda m: summary[m]["fcf"]["mae"])
    prophet, simple = summary["prophet"]["fcf"], summary[best]["fcf"]
    mae_ok = prophet["mae"] <= 0.9 * simple["mae"]
    bias_ok = abs(prophet["bias"]) <= abs(simple["bias"])
    return {"best_simple": best, "mae_ok": mae_ok, "bias_ok": bias_ok, "prophet_stays": mae_ok and bias_ok,
            "mae_ratio": prophet["mae"] / simple["mae"] if simple["mae"] else math.nan}


# --- вивід ----------------------------------------------------------------------

def _fmt(x, pct=False):
    """Форматує число (або відсоток) для таблиці; «—» для відсутнього значення."""
    if isinstance(x, float) and math.isnan(x):
        return "—"
    return f"{x * 100:.1f}%" if pct else f"{x:,.0f}".replace(",", " ")


def print_summary(title: str, summary: dict):
    """Друкує зведену таблицю метрик моделей."""
    print(f"\n### {title}")
    print("| модель | n | MAE дохід | MAE витрати | MAE FCF | bias FCF | sMAPE FCF | знак FCF |")
    print("|---|---|---|---|---|---|---|---|")
    for model in MODELS:
        if model not in summary:
            continue
        s = summary[model]
        print(f"| {model} | {s['n']} | {_fmt(s['income']['mae'])} | {_fmt(s['expenses']['mae'])} | "
              f"{_fmt(s['fcf']['mae'])} | {_fmt(s['fcf']['bias'])} | {_fmt(s['fcf']['smape'], pct=True)} | "
              f"{_fmt(s['fcf_sign_acc'], pct=True)} |")


def print_points(rows: list[dict]):
    """Друкує таблицю прогнозів по точках: факт, Prophet і найкраща проста модель."""
    print("| точка → ціль | факт FCF | prophet | найкраща за точкою (проста) |")
    print("|---|---|---|---|")
    for origin in dict.fromkeys(r["origin"] for r in rows):
        point = [r for r in rows if r["origin"] == origin]
        prophet = next(r for r in point if r["model"] == "prophet")
        simple = min((r for r in point if r["model"] != "prophet"), key=lambda r: abs(r["fcf_pred"] - r["fcf_actual"]))
        print(f"| {origin} → {prophet['target']} | {_fmt(prophet['fcf_actual'])} | {_fmt(prophet['fcf_pred'])} | "
              f"{simple['model']} {_fmt(simple['fcf_pred'])} |")


async def main(user_id: str, today: date, expect_db_port: int, out_dir: str) -> int:
    """Завантажує ряди, виконує бектест обох горизонтів, друкує підсумки й пише CSV.

    Returns:
        Код виходу: 0 — успіх, 2 — відмова перевірки цілі БД.
    """
    engine.echo = False
    try:
        target_db = check_db_target(engine.url, expect_db_port, os.environ)
    except DbGuardRefused as e:
        print(f"ВІДМОВА (db_guard): {e}. До БД не підключались.")
        return 2
    print(f"БД: {target_db} (лише читання); користувач {user_id[:8]}…; today = {today}")

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            series = await load_series(session, user_id)
        finally:
            await session.rollback()

    # setLevel не досить: prophet/cmdstanpy налаштовують власні обробники.
    for logger in ("cmdstanpy", "prophet"):
        logging.getLogger(logger).disabled = True

    os.makedirs(out_dir, exist_ok=True)
    for horizon_name, horizon in HORIZONS.items():
        rows = run_backtest(series, today, horizon)
        path = os.path.join(out_dir, f"forecast_backtest_{horizon_name}.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

        prophet_rows = [r for r in rows if r["model"] == "prophet"]
        seconds = [r["prophet_seconds"] for r in prophet_rows]
        events = [r["events"] for r in prophet_rows if r["events"]]
        label = f"Горизонт {horizon_name}: t → t+{horizon}"
        print(f"\n## {label}; точок n = {len(prophet_rows)}; CSV: {path}")
        print(f"Prophet: {sum(seconds):.1f} с на {2 * len(prophet_rows)} фітів "
              f"(сер. {statistics.fmean(seconds) / 2:.2f} с/фіт); збоїв/fallback: {len(events)} {events or ''}")
        summary = summarize(rows)
        print_summary(f"{label} — усі точки", summary)
        verdict = criterion(summary)
        print(f"\nКритерій ({label}): найкраща проста = {verdict['best_simple']}; "
              f"MAE FCF prophet/найкраща = {verdict['mae_ratio']:.3f} (треба <= 0.900) -> "
              f"{'так' if verdict['mae_ok'] else 'ні'}; |bias| не гірший -> {'так' if verdict['bias_ok'] else 'ні'}; "
              f"Prophet лишається: {'ТАК' if verdict['prophet_stays'] else 'НІ'}")

        before = [r for r in rows if r["target"] < REGIME_SHIFT]
        after = [r for r in rows if r["target"] >= REGIME_SHIFT]
        n_before = len({r["origin"] for r in before})
        n_after = len({r["origin"] for r in after})
        if n_before >= MIN_POINTS_PER_SIDE and n_after >= MIN_POINTS_PER_SIDE:
            print_summary(f"{label} — ціль < {REGIME_SHIFT} (n = {n_before})", summarize(before))
            print_summary(f"{label} — ціль >= {REGIME_SHIFT} (n = {n_after})", summarize(after))
        else:
            print(f"\nРозбиття за {REGIME_SHIFT} неможливе: {n_before} / {n_after} точок (< {MIN_POINTS_PER_SIDE}).")

        print(f"\n### {label} — точки (FCF, грн)")
        print_points(rows)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--expect-db-port", type=int, required=True)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--out-dir", default=os.path.join(tempfile.gettempdir(), "forecast_backtest"))
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.user_id, args.today, args.expect_db_port, args.out_dir)))
