"""
Бектест правил планування додаткового платежу за боргами.

Лише читає БД; записів не створює. На кожній точці t правило дає заплановану
суму P понад мінімальні платежі (вони вже вилучені з вільного потоку FCF);
додатковий платіж E = max(P, 0) порівнюється з фактичним FCF цілі.

Ряди, вікно, точки відліку й горизонти — ті самі, що в
scripts/analysis/forecast_backtest.py (функції імпортуються звідти).
Горизонт A (t -> t+2) — основний, B (t -> t+1) — додатковий.

Правила (сирі ряди вікна, без вінзоризації, якщо не вказано інакше):
  R0  P = 0 (лише мінімальні платежі)
  R1  медіана FCF за останні 6 місяців вікна
  R2  медіана FCF за останні 12
  R3  25-й перцентиль FCF за останні 12
  R4  мінімум FCF за останні 6
  R5  mean3_w: mean3_w(дохід) - mean3_w(витрати), як у forecast_backtest
  R6  P25(дохід, 12) - медіана(витрати, 6)
  R7  Prophet: прогноз доходу - прогноз витрат
Якщо у вікні менше місяців, ніж потрібно, правило бере всі наявні й пише про
це в note; без жодного місяця — P = 0. Перцентилі — лінійна інтерполяція
(numpy.percentile). Діапазон (не правило): [P10, P90] FCF за останні 12
місяців вікна.

Критерій, заданий до запуску: серед R1..R7 з часткою недобору (факт < E серед
точок з E > 0) не більше 25 % обирається правило з найбільшим середнім E; якщо
два найкращі різняться не більше ніж на 5 % середнього E — простіше (менше
параметрів); якщо жодне не проходить — лише мінімальні платежі (R0), діапазон
окремо.

Запуск (з кореня репозиторію; основна БД — лише читання):
    ALLOW_MAIN_DB=1 python -m scripts.analysis.planning_rules_backtest \\
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
from datetime import date

import numpy as np
from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from app.models.database import AsyncSessionLocal, engine
from app.services.forecasting import _month_key
from scripts.analysis.forecast_backtest import (
    HORIZONS,
    MIN_POINTS_PER_SIDE,
    REGIME_SHIFT,
    load_series,
    origins,
    prophet_forecast,
    simple_forecasts,
    window_at,
)
from scripts.data_fixes._db_guard import DbGuardRefused, check_db_target

RULES = ["R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7"]
CANDIDATES = RULES[1:]
RULE_LABELS = {
    "R0": "P = 0 (лише мінімальні)",
    "R1": "медіана FCF, 6 міс.",
    "R2": "медіана FCF, 12 міс.",
    "R3": "P25 FCF, 12 міс.",
    "R4": "мінімум FCF, 6 міс.",
    "R5": "mean3_w FCF",
    "R6": "P25 дохід 12 − медіана витрати 6",
    "R7": "Prophet FCF (продукт)",
}
# Кількість параметрів — для тай-брейку «простіше». Prophet — найскладніше.
RULE_PARAMS = {"R1": 1, "R2": 1, "R4": 1, "R3": 2, "R5": 2, "R6": 3, "R7": math.inf}
MAX_SHORTFALL_SHARE = 0.25
TIE_SHARE = 0.05
STAT_TABLES = ("forecasts", "milp_plans", "transactions", "category_training_examples")


# --- правила ------------------------------------------------------------------

def _last(values: list[float], n: int, what: str) -> tuple[list[float], str]:
    """Повертає останні n значень; якщо їх менше — усі наявні з поясненням."""
    if len(values) >= n:
        return values[-n:], ""
    return list(values), f"{what}: історія {len(values)} < {n} міс., узято всі наявні"


def _percentile(values: list[float], q: float) -> float:
    """Повертає q-й перцентиль (лінійна інтерполяція)."""
    return float(np.percentile(values, q))


def plan_at(window: dict, target: date, prophet: bool = True) -> dict:
    """Рахує заплановану суму кожного правила для вікна в точці t.

    Returns:
        {rule: {"P": float, "note": str}} і "_interval": (P10, P90, note).
    """
    income, expenses = window["income"], window["expenses"]
    fcf = [i - e for i, e in zip(income, expenses)]
    if not fcf:
        empty = {"P": 0.0, "note": "немає історії у вікні — P = 0"}
        return {**{r: dict(empty) for r in RULES}, "_interval": (math.nan, math.nan, empty["note"])}

    plans = {"R0": {"P": 0.0, "note": ""}}
    v6, n6 = _last(fcf, 6, "FCF")
    v12, n12 = _last(fcf, 12, "FCF")
    plans["R1"] = {"P": statistics.median(v6), "note": n6}
    plans["R2"] = {"P": statistics.median(v12), "note": n12}
    plans["R3"] = {"P": _percentile(v12, 25), "note": n12}
    plans["R4"] = {"P": min(v6), "note": n6}

    inc_simple, exp_simple = simple_forecasts(income), simple_forecasts(expenses)
    plans["R5"] = {"P": inc_simple["mean3_w"] - exp_simple["mean3_w"], "note": ""}

    inc12, ni = _last(income, 12, "дохід")
    exp6, ne = _last(expenses, 6, "витрати")
    plans["R6"] = {"P": _percentile(inc12, 25) - statistics.median(exp6), "note": "; ".join(x for x in (ni, ne) if x)}

    if prophet:
        p_inc, _, ev_i = prophet_forecast(window["months"], income, window["fallback_reason"], target)
        p_exp, _, ev_e = prophet_forecast(window["months"], expenses, window["fallback_reason"], target)
        plans["R7"] = {"P": p_inc - p_exp, "note": "; ".join(e for e in (ev_i, ev_e) if e)}
    else:
        plans["R7"] = {"P": math.nan, "note": "prophet вимкнено"}

    plans["_interval"] = (_percentile(v12, 10), _percentile(v12, 90), n12)
    return plans


def run(series: dict, today: date, horizon: int, prophet: bool = True) -> list[dict]:
    """Повертає рядки (точка, правило): P, E, фактичний FCF цілі, нотатки й діапазон."""
    rows = []
    for origin in origins(series, today, horizon):
        window = window_at(series, origin)
        target = origin + relativedelta(months=horizon)
        key = _month_key(target)
        fact = series["income"].get(key, 0.0) - series["expenses"].get(key, 0.0)
        plans = plan_at(window, target, prophet=prophet)
        p10, p90, interval_note = plans["_interval"]
        for rule in RULES:
            p = plans[rule]["P"]
            rows.append({
                "origin": _month_key(origin), "target": key, "window_months": len(window["months"]),
                "rule": rule, "P": p, "E": max(p, 0.0) if not math.isnan(p) else math.nan,
                "fcf_actual": fact, "note": plans[rule]["note"],
                "p10": p10, "p90": p90, "inside": (p10 <= fact <= p90) if not math.isnan(p10) else "",
                "interval_note": interval_note,
            })
    return rows


# --- метрики --------------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Повертає 95 % довірчий інтервал Вілсона для частки k/n; для n = 0 — (nan, nan)."""
    if n == 0:
        return math.nan, math.nan
    p = k / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def rule_metrics(rows: list[dict], rule: str) -> dict:
    """Рахує метрики правила: частку недобору з інтервалом Вілсона, середні E і недобору."""
    rr = [r for r in rows if r["rule"] == rule and not math.isnan(r["E"])]
    pos = [r for r in rr if r["E"] > 0]
    short = [r for r in pos if r["fcf_actual"] < r["E"]]
    over = [r for r in rr if r["fcf_actual"] > r["E"]]
    mean_e = statistics.fmean(r["E"] for r in rr) if rr else math.nan
    mean_pos_fact = statistics.fmean(max(r["fcf_actual"], 0.0) for r in rr) if rr else math.nan
    return {
        "n": len(rr),
        "n_pos": len(pos),
        "k_short": len(short),
        "share": len(short) / len(pos) if pos else 0.0,
        "wilson": wilson(len(short), len(pos)),
        "short_mean_in_short": statistics.fmean(r["E"] - r["fcf_actual"] for r in short) if short else 0.0,
        "short_mean_all": statistics.fmean(max(0.0, r["E"] - r["fcf_actual"]) for r in rr) if rr else math.nan,
        "mean_e": mean_e,
        "under_plan": statistics.fmean(r["fcf_actual"] - r["E"] for r in over) if over else 0.0,
        "ratio": mean_e / mean_pos_fact if mean_pos_fact else math.nan,
    }


def choose(metrics: dict) -> dict:
    """Застосовує критерій вибору правила.

    Returns:
        {"rule", "verdict", "eligible", "runner_up"}.
    """
    eligible = [r for r in CANDIDATES if metrics[r]["share"] <= MAX_SHORTFALL_SHARE]
    if not eligible:
        return {"rule": "R0", "eligible": [], "runner_up": None,
                "verdict": "планувати лише мінімальні платежі (R0), діапазон показувати окремо"}
    ranked = sorted(eligible, key=lambda r: -metrics[r]["mean_e"])
    best, runner_up = ranked[0], ranked[1] if len(ranked) > 1 else None
    chosen, tie = best, False
    if runner_up is not None:
        gap = metrics[best]["mean_e"] - metrics[runner_up]["mean_e"]
        if gap <= TIE_SHARE * metrics[best]["mean_e"] and RULE_PARAMS[runner_up] < RULE_PARAMS[best]:
            chosen, tie = runner_up, True
    verdict = f"{chosen} ({RULE_LABELS[chosen]})"
    if tie:
        verdict += f" — тай-брейк ≤ 5 %: простіше за {best}"
    if metrics[chosen]["mean_e"] == 0:
        # Частка недобору 0/0 (жодного E > 0) рахується як 0 %.
        verdict += " — увага: середнє E = 0 (жодного E > 0), на практиці те саме, що R0"
    return {"rule": chosen, "eligible": eligible, "runner_up": runner_up, "verdict": verdict}


# --- вивід ------------------------------------------------------------------------

def _fmt(x, pct=False):
    """Форматує число (або відсоток) для таблиці; «—» для відсутнього значення."""
    if isinstance(x, float) and math.isnan(x):
        return "—"
    if pct:
        return f"{x * 100:.0f} %"
    return f"{x:,.0f}".replace(",", " ")


def print_metrics(title: str, rows: list[dict]) -> dict:
    """Друкує таблицю метрик правил."""
    metrics = {rule: rule_metrics(rows, rule) for rule in RULES}
    n_points = len({r["origin"] for r in rows})
    print(f"\n### {title} (n = {n_points})")
    print("| правило | E > 0 | недобір | частка | Wilson 95 % | сер. недобір (у міс. недобору) | "
          "сер. недобір (усі точки) | сер. E | недопланування | E / факт+ |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for rule in RULES:
        m = metrics[rule]
        lo, hi = m["wilson"]
        print(f"| {rule} {RULE_LABELS[rule]} | {m['n_pos']}/{m['n']} | {m['k_short']} | {_fmt(m['share'], True)} | "
              f"{_fmt(lo, True)}–{_fmt(hi, True)} | {_fmt(m['short_mean_in_short'])} | {_fmt(m['short_mean_all'])} | "
              f"{_fmt(m['mean_e'])} | {_fmt(m['under_plan'])} | {_fmt(m['ratio'], True)} |")
    interval = [r for r in rows if r["rule"] == "R0" and r["inside"] != ""]
    inside = sum(1 for r in interval if r["inside"])
    print(f"\nДіапазон [P10, P90] FCF за 12 міс.: факт усередині у {inside}/{len(interval)} "
          f"({_fmt(inside / len(interval), True) if interval else '—'}).")
    return metrics


async def _stats(session) -> dict:
    """Повертає лічильники вставок/оновлень/видалень і кількість рядків таблиць для перевірки, що скрипт нічого не змінив."""
    rows = (await session.execute(text(
        "select relname, n_tup_ins, n_tup_upd, n_tup_del, n_live_tup from pg_stat_user_tables "
        "where relname = any(:t) order by relname"), {"t": list(STAT_TABLES)})).all()
    counts = {}
    for table in STAT_TABLES:
        counts[table] = (await session.execute(text(f"select count(*) from {table}"))).scalar_one()
    return {"stat": [tuple(r) for r in rows], "counts": counts}


async def read_only(fn):
    """Виконує fn у транзакції лише для читання з відкатом наприкінці."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            return await fn(session)
        finally:
            await session.rollback()


async def main(user_id: str, today: date, expect_db_port: int, out_dir: str) -> int:
    """Завантажує ряди, рахує правила для обох горизонтів, друкує метрики й вердикт і пише CSV.

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

    stats_before = await read_only(_stats)
    series = await read_only(lambda s: load_series(s, user_id))
    for logger in ("cmdstanpy", "prophet"):
        logging.getLogger(logger).disabled = True

    os.makedirs(out_dir, exist_ok=True)
    for name, horizon in HORIZONS.items():
        rows = run(series, today, horizon)
        path = os.path.join(out_dir, f"planning_rules_{name}.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        label = f"Горизонт {name}: t → t+{horizon}"
        print(f"\n## {label}; CSV: {path}")
        notes = sorted({r["note"] for r in rows if r["note"]})
        if notes:
            print("Fallback/нотатки правил: " + " | ".join(notes))
        metrics = print_metrics(f"{label} — усі точки", rows)
        decision = choose(metrics)
        role = "рішення" if name == "A" else "довідково, у рішенні не бере участі"
        print(f"\nКритерій ({label}; {role}): допустимі (частка ≤ 25 %): {decision['eligible'] or 'немає'}; "
              f"ВЕРДИКТ: {decision['verdict']}")
        if decision["eligible"]:
            lo, hi = metrics[decision["rule"]]["wilson"]
            overlap = [r for r in CANDIDATES if r != decision["rule"]
                       and not (metrics[r]["wilson"][1] < lo or metrics[r]["wilson"][0] > hi)]
            print(f"Інтервали Вілсона, що перекриваються з {decision['rule']}: {overlap}")

        before = [r for r in rows if r["target"] < REGIME_SHIFT]
        after = [r for r in rows if r["target"] >= REGIME_SHIFT]
        nb, na = len({r["origin"] for r in before}), len({r["origin"] for r in after})
        if nb >= MIN_POINTS_PER_SIDE and na >= MIN_POINTS_PER_SIDE:
            for part, part_rows in ((f"ціль < {REGIME_SHIFT}", before), (f"ціль >= {REGIME_SHIFT}", after)):
                m = print_metrics(f"{label} — {part}", part_rows)
                print(f"Критерій на підвибірці: {choose(m)['verdict']}")
        else:
            print(f"\nРозбиття за {REGIME_SHIFT} неможливе: {nb} / {na} точок (< {MIN_POINTS_PER_SIDE}).")

    stats_after = await read_only(_stats)
    same = stats_before == stats_after
    print(f"\nЛічильники pg_stat_user_tables і кількості рядків ({', '.join(STAT_TABLES)}): "
          f"до == після: {'ТАК' if same else 'НІ'}")
    print(f"  до:    {stats_before}\n  після: {stats_after}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--expect-db-port", type=int, required=True)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--out-dir", default=os.path.join(tempfile.gettempdir(), "planning_rules_backtest"))
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.user_id, args.today, args.expect_db_port, args.out_dir)))
