"""
Бектест прогнозу місячних витрат на датасеті Berka: прості моделі, ETS, Prophet у
продуктовій конфігурації і глобальні моделі (HistGradientBoosting, LSTM,
Transformer-encoder, DLinear).

Основна БД FinHealifi не використовується; дані — CSV з data/external/berka/
(див. berka_audit.py).

Ряди: місячні витрати рахунку (сума |amount| для type з --expense-types); перший і
останній місяць рахунку відкинуто, місяць без списань усередині історії = 0;
беруться рахунки з >= --min-months місяцями.

Розбиття:
- рахунки — train/val/test 60/20/20 перестановкою default_rng(42) відсортованих
  account_id; при --min-months < 36 рахунки основного набору (>= 36) зберігають свою
  групу, решта ділиться тією ж процедурою;
- календар: train-приклад — вхід і ціль < T1 на train-рахунку; val-точка — ціль у
  [T1, T2) на val-рахунку; test-точка — ціль >= T2 на test-рахунку.
Точка (рахунок, t, h): місяці t-11..t і t+h лежать в історії рахунку; горизонти
A (t+2, основний) і B (t+1).

Режим `val` навчає моделі лише на train-прикладах, підбирає гіперпараметри на val і
пише в --out-dir таблиці метрик val, сітки підбору, конфігурацію-кандидат і ваги.
Test-точки будуються лише для перевірок розбиття; моделі на них не запускаються.

Режим `test` — одноразова оцінка на test із замороженою конфігурацією
(val_config_candidate.json). Для набору й типів витрат, на яких її обрано, беруться
збережені ваги й моделі без перенавчання; для наборів чутливості глобальні моделі
перенавчаються з тими самими гіперпараметрами (early stopping на val свого набору).
Результати йдуть у --out-dir/test_<типи>_min<місяці>/; якщо каталог уже існує, запуск
відмовляє. frozen.json фіксує sha256 конфігурації, коду й моделей і git-ревізію.
Модель hgb_sq (HistGradientBoosting із squared_error, гіперпараметри обраного HGB) —
експлораторна: метрики рахуються, вердикт — ні.

Запуск (з кореня репозиторію):
    python -m scripts.analysis.berka_backtest val [--expense-types=VYDAJ,VYBER]
        [--min-months 36] [--out-dir DIR] [--n-jobs N]
    python -m scripts.analysis.berka_backtest test [--expense-types=...] [--min-months N]
        [--out-dir DIR] [--n-jobs N]
"""
import argparse
import copy
import json
import logging
import os
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.analysis.berka_audit import (
    DEFAULT_DATA_DIR,
    DEFAULT_EXPENSE_TYPES,
    aggregate_monthly_expenses,
    drop_edge_months,
    load_trans,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "scripts" / "analysis" / "out" / "berka_backtest"

T1 = pd.Period("1996-12", freq="M")  # початок val
T2 = pd.Period("1997-12", freq="M")  # початок test
INPUT_LEN = 12
HORIZONS = {"A": 2, "B": 1}
MAX_H = max(HORIZONS.values())
GROUPS = ("train", "val", "test")
MAIN_MIN_MONTHS = 36
SPLIT_SEED = 42
SPLIT_FRACTIONS = (0.6, 0.2)  # train, val; решта — test
STAT_SUBSET_SIZE = 200
STAT_SUBSET_SEED = 42
BOOTSTRAP_REPS = 1000
BOOTSTRAP_SEED = 42
INTERVAL_ALPHA = 0.2  # [P10, P90]

MA_WINDOWS = tuple(range(2, 13))
FILE_SIMPLE = ("naive", "mean3", "mean6", "median6", "window_mean")
FILE_SIMPLE_W = tuple(f"{m}_w" for m in FILE_SIMPLE if m != "naive")

ETS_CONFIGS = {
    "ANN": {"trend": None, "damped_trend": False, "seasonal": None},
    "AAdN": {"trend": "add", "damped_trend": True, "seasonal": None},
    "ANA": {"trend": None, "damped_trend": False, "seasonal": "add"},
    "AAdA": {"trend": "add", "damped_trend": True, "seasonal": "add"},
}
ETS_SEASONAL_MIN_MONTHS = 24

HGB_GRID = [
    {"learning_rate": lr, "max_leaf_nodes": leaves, "max_iter": iters}
    for lr in (0.05, 0.1) for leaves in (15, 31) for iters in (100, 300)
]
HGB_SEED = 42

NN_SEEDS = (0, 1, 2, 3, 4)
NN_GRIDS = {
    "lstm": [{"hidden": h, "lr": lr} for h in (32, 64) for lr in (1e-3, 3e-3)],
    "transformer": [{"dropout": d, "lr": lr} for d in (0.0, 0.1) for lr in (1e-3, 3e-3)],
    "dlinear": [{"kernel": k, "lr": lr} for k in (3, 5) for lr in (1e-3, 3e-3)],
}
NN_BATCH = 256
NN_MAX_EPOCHS = 200
NN_PATIENCE = 15


# --- дані й розбиття --------------------------------------------------------------

def load_panel(data_dir: Path, expense_types, min_months: int) -> pd.DataFrame:
    """Повертає місячні витрати рахунків із >= min_months місяцями після відкидання крайніх.

    Returns:
        Фрейм account_id, month (Period[M]), expense (float), впорядкований за рахунком і місяцем.
    """
    monthly = drop_edge_months(aggregate_monthly_expenses(load_trans(data_dir / "trans.csv"), expense_types))
    n_months = monthly.groupby("account_id")["month"].transform("size")
    panel = monthly.loc[n_months >= min_months, ["account_id", "month", "expense"]].copy()
    panel["expense"] = panel["expense"].astype(float)
    return panel.sort_values(["account_id", "month"]).reset_index(drop=True)


def split_accounts(account_ids, seed: int = SPLIT_SEED, base: dict | None = None) -> dict:
    """Ділить рахунки на train/val/test 60/20/20 перестановкою відсортованих id.

    Args:
        account_ids: Рахунки набору.
        seed: Seed numpy.random.default_rng.
        base: Готові групи; рахунки з нього зберігають групу, решта ділиться окремо.

    Returns:
        {account_id: "train" | "val" | "test"}.
    """
    base = base or {}
    ids = sorted({int(a) for a in account_ids})
    rest = np.array([a for a in ids if a not in base], dtype=np.int64)
    perm = np.random.default_rng(seed).permutation(rest)
    n_train = int(round(len(perm) * SPLIT_FRACTIONS[0]))
    n_val = int(round(len(perm) * SPLIT_FRACTIONS[1]))
    groups = {a: base[a] for a in ids if a in base}
    for i, acc in enumerate(perm):
        groups[int(acc)] = "train" if i < n_train else ("val" if i < n_train + n_val else "test")
    return groups


def label_rows(panel: pd.DataFrame, groups: dict) -> np.ndarray:
    """Мітка кожного рядка (рахунок, місяць) за розбиттям за рахунками й календарем.

    "train" — рядок train-рахунку до T1; "val" — рядок val-рахунку в [T1, T2);
    "test" — рядок test-рахунку від T2; "" — рядок, що може бути лише входом точки
    свого рахунку.
    """
    group = panel["account_id"].map(groups).to_numpy()
    month = panel["month"]
    before_t1 = (month < T1).to_numpy()
    in_val = ((month >= T1) & (month < T2)).to_numpy()
    from_t2 = (month >= T2).to_numpy()
    return np.select(
        [(group == "train") & before_t1, (group == "val") & in_val, (group == "test") & from_t2],
        ["train", "val", "test"],
        default="",
    )


@dataclass
class Samples:
    """Приклади однієї групи.

    meta: account_id, origin, h (0 для train), first_month, last_month, row_start, row_origin.
    X: вхідні вікна n×12 у вихідних одиницях.
    y: train — n×2 (цілі t+1, t+2); val/test — n (ціль t+h).
    """

    meta: pd.DataFrame
    X: np.ndarray
    y: np.ndarray


def build_samples(panel: pd.DataFrame, labels: np.ndarray) -> dict[str, Samples]:
    """Будує train-приклади й val/test-точки з міток рядків.

    Train-приклад — вікно t-11..t+2, усі рядки якого мають мітку "train". Val/test-точка —
    ціль t+h з міткою групи; вхід t-11..t — історія того самого рахунку з будь-якою міткою.

    Returns:
        {"train": Samples, "val": Samples, "test": Samples}.
    """
    values = panel["expense"].to_numpy()
    months = panel["month"].to_numpy()
    out = {g:{"meta": [], "X": [], "y": []} for g in GROUPS}
    for acc, idx in panel.groupby("account_id", sort=True).indices.items():
        start, n = idx[0], len(idx)
        for i in range(INPUT_LEN - 1, n - 1):
            lo, origin = start + i - INPUT_LEN + 1, start + i
            x = values[lo:origin + 1]
            last_train = origin + MAX_H
            if i + MAX_H < n and (labels[lo:last_train + 1] == "train").all():
                out["train"]["meta"].append((acc, months[origin], 0, months[lo], months[last_train], start, origin))
                out["train"]["X"].append(x)
                out["train"]["y"].append(values[origin + 1:last_train + 1])
            for h in HORIZONS.values():
                if i + h >= n:
                    continue
                group = labels[origin + h]
                if group in ("val", "test"):
                    out[group]["meta"].append((acc, months[origin], h, months[lo], months[origin + h], start, origin))
                    out[group]["X"].append(x)
                    out[group]["y"].append(values[origin + h])
    cols =["account_id", "origin", "h", "first_month", "last_month", "row_start", "row_origin"]
    result = {}
    for g, d in out.items():
        meta = pd.DataFrame(d["meta"], columns=cols)
        X = np.array(d["X"], dtype=float).reshape(-1, INPUT_LEN)
        y = np.array(d["y"], dtype=float).reshape(-1, MAX_H) if g == "train" else np.array(d["y"], dtype=float)
        result[g] = Samples(meta, X, y)
    return result


def check_no_account_overlap(panel: pd.DataFrame, labels: np.ndarray, samples: dict[str, Samples]) -> None:
    """Перевіряє, що жоден рахунок не належить двом групам (за мітками рядків і за прикладами).

    Raises:
        ValueError: рахунок є у двох групах.
    """
    acc = panel["account_id"].to_numpy()
    owners = {}
    for g in GROUPS:
        accounts = set(acc[labels == g].tolist()) | set(samples[g].meta["account_id"].tolist())
        for a in accounts:
            if a in owners and owners[a] != g:
                raise ValueError(f"рахунок {a} у групах {owners[a]} і {g}")
            owners[a] = g


def check_train_before_t1(panel: pd.DataFrame, labels: np.ndarray, samples: dict[str, Samples]) -> None:
    """Перевіряє, що жоден train-рядок і жодне train-вікно (вхід і ціль) не містить місяців >= T1.

    Raises:
        ValueError: є train-дані з місяцем >= T1.
    """
    train_months = panel["month"][labels == "train"]
    if len(train_months) and train_months.max() >= T1:
        raise ValueError(f"train-рядок з місяцем {train_months.max()} >= {T1}")
    meta = samples["train"].meta
    if len(meta) and (meta["last_month"].max() >= T1 or meta["first_month"].max() >= T1):
        raise ValueError(f"train-вікно закінчується {meta['last_month'].max()} >= {T1}")


def scale_inputs(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Ділить кожне вхідне вікно на його середнє (на 1, якщо середнє 0).

    Returns:
        (масштабовані вікна, множник на кожне вікно).
    """
    factor = X.mean(axis=1)
    factor = np.where(factor == 0, 1.0, factor)
    return X / factor[:, None], factor


def account_stats(panel: pd.DataFrame) -> pd.DataFrame:
    """Рахує по рахунку на місяцях < T1: масштаб MASE (середній |y_m - y_{m-1}|) і частку нульових місяців.

    Returns:
        Фрейм з індексом account_id і стовпцями scale (NaN при < 2 місяцях), zero_share.
    """
    before = panel[panel["month"] < T1]
    g = before.groupby("account_id")["expense"]
    scale = g.apply(lambda s: s.diff().abs().mean() if len(s) >= 2 else np.nan)
    zero_share = g.apply(lambda s: float((s == 0).mean()))
    stats = pd.DataFrame({"scale": scale, "zero_share": zero_share})
    return stats.reindex(panel["account_id"].unique())


def zero_stratum(zero_share: float) -> str:
    """Страта за часткою нульових місяців: "0", "<5%" або ">=5%"."""
    if zero_share == 0:
        return "0"
    return "<5%" if zero_share < 0.05 else ">=5%"


# --- прості моделі ----------------------------------------------------------------

def simple_predictions(points: Samples, values: np.ndarray) -> dict[str, np.ndarray]:
    """Прогнози простих моделей для точок.

    naive, seasonal_naive (місяць t+h-12), ma2..ma12 — на вхідному вікні; mean3, mean6,
    median6, window_mean і їхні _w-варіанти (після _winsorize_mad) — на всій історії
    рахунку до t, як у forecast_backtest.py.

    Args:
        points: Val- чи test-точки.
        values: Стовпець expense панелі (індекси row_start/row_origin вказують у нього).
    """
    from app.services.forecasting import _winsorize_mad

    X, n = points.X, len(points.X)
    h = points.meta["h"].to_numpy()
    preds = {"naive": X[:, -1].copy(), "seasonal_naive": X[np.arange(n), h - 1].copy()}
    for k in MA_WINDOWS:
        preds[f"ma{k}"] = X[:, -k:].mean(axis=1)
    base = {m: np.empty(n) for m in FILE_SIMPLE[1:] + FILE_SIMPLE_W}
    for j, (start, origin) in enumerate(zip(points.meta["row_start"], points.meta["row_origin"])):
        hist = values[start:origin + 1]
        wins = np.array(_winsorize_mad(list(hist)))
        for series, suffix in ((hist, ""), (wins, "_w")):
            base[f"mean3{suffix}"][j] = series[-3:].mean()
            base[f"mean6{suffix}"][j] = series[-6:].mean()
            base[f"median6{suffix}"][j] = np.median(series[-6:])
            base[f"window_mean{suffix}"][j] = series.mean()
    preds.update(base)
    return preds


# --- статистичні моделі ------------------------------------------------------------

def _ets_account(hist_values: np.ndarray, start_month: pd.Period, origins: list[int],
                 configs: tuple[str, ...]) -> list[dict]:
    """Фітить конфігурації ETS на історії до кожного origin і прогнозує t+1, t+2 з [P10, P90]."""
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel

    warnings.filterwarnings("ignore")
    index = pd.period_range(start_month, periods=len(hist_values), freq="M").to_timestamp()
    rows = []
    for o in origins:
        y = pd.Series(hist_values[:o + 1], index=index[:o + 1])
        for name in configs:
            cfg = dict(ETS_CONFIGS[name])
            used = name
            if cfg["seasonal"] and len(y) < ETS_SEASONAL_MIN_MONTHS:
                cfg["seasonal"] = None
                used = name[:-1] + "N"
            row = {"origin_idx": o, "config": name, "used": used, "failed": False}
            try:
                fit = ETSModel(y, error="add", seasonal_periods=12 if cfg["seasonal"] else None, **cfg).fit(disp=False)
                frame = fit.get_prediction(start=len(y), end=len(y) + MAX_H - 1).summary_frame(alpha=INTERVAL_ALPHA)
                mean, lo, hi = frame["mean"].to_numpy(), frame["pi_lower"].to_numpy(), frame["pi_upper"].to_numpy()
                if not np.all(np.isfinite(mean)):
                    raise ValueError("non-finite forecast")
            except Exception:
                row["failed"] = True
                mean, lo, hi = np.full(MAX_H, y.mean()), np.full(MAX_H, np.nan), np.full(MAX_H, np.nan)
            for h in range(1, MAX_H + 1):
                row[f"mean{h}"], row[f"lo{h}"], row[f"hi{h}"] = mean[h - 1], lo[h - 1], hi[h - 1]
            rows.append(row)
    return rows


def ets_predictions(points: Samples, panel: pd.DataFrame, n_jobs: int,
                    configs: tuple[str, ...] = tuple(ETS_CONFIGS)) -> dict:
    """Прогнози ETS заданих конфігурацій для точок (фіт на всій історії рахунку до t).

    Returns:
        {config: {"pred", "lo", "hi", "failed"}} — масиви, вирівняні з точками.
    """
    from joblib import Parallel, delayed

    values = panel["expense"].to_numpy()
    months = panel["month"].to_numpy()
    meta = points.meta
    tasks = []
    for acc, sub in meta.groupby("account_id"):
        start = int(sub["row_start"].iloc[0])
        end = int(sub["row_origin"].max())
        origins = sorted({int(r) - start for r in sub["row_origin"]})
        tasks.append((acc, start, values[start:end + 1], months[start], origins))
    results = Parallel(n_jobs=n_jobs)(delayed(_ets_account)(v, m, o, configs) for _, _, v, m, o in tasks)
    lookup = {}
    for (acc, start, *_), rows in zip(tasks, results):
        for r in rows:
            lookup[(acc, start + r["origin_idx"], r["config"])] = r
    out = {}
    for name in configs:
        rows = [lookup[(a, int(o), name)] for a, o in zip(meta["account_id"], meta["row_origin"])]
        h = meta["h"].to_numpy()
        out[name] = {
            "pred": np.array([r[f"mean{hh}"] for r, hh in zip(rows, h)]).clip(min=0),
            "lo": np.array([r[f"lo{hh}"] for r, hh in zip(rows, h)]),
            "hi": np.array([r[f"hi{hh}"] for r, hh in zip(rows, h)]),
            "failed": np.array([r["failed"] for r in rows]),
            "used": np.array([r["used"] for r in rows]),
        }
    return out


def _prophet_account(hist_values: np.ndarray, start_month: pd.Period, tasks: list[tuple[int, int]]) -> list[tuple]:
    """Прогнози Prophet у продуктовій конфігурації (_winsorize_mad -> _run_prophet) для (origin_idx, h)."""
    from app.services.forecasting import _run_prophet, _winsorize_mad

    for name in ("cmdstanpy", "prophet"):
        logging.getLogger(name).disabled = True
    warnings.filterwarnings("ignore")
    ds = pd.period_range(start_month, periods=len(hist_values), freq="M").to_timestamp()
    out = []
    for o, h in tasks:
        df = pd.DataFrame({"ds": ds[:o + 1], "y": _winsorize_mad(list(hist_values[:o + 1]))})
        target = (pd.Period(start_month, freq="M") + o + h).to_timestamp().date()
        try:
            out.append((o, h, _run_prophet(df, target), ""))
        except Exception as e:  # збій фіту фіксується, точка отримує window_mean
            out.append((o, h, float(np.mean(hist_values[:o + 1])), type(e).__name__))
    return out


def prophet_predictions(points: Samples, panel: pd.DataFrame, n_jobs: int) -> tuple[np.ndarray, np.ndarray]:
    """Прогнози Prophet у продуктовій конфігурації для точок.

    Returns:
        (прогнози, ознака збою) — масиви, вирівняні з точками.
    """
    from joblib import Parallel, delayed

    values = panel["expense"].to_numpy()
    months = panel["month"].to_numpy()
    meta = points.meta
    tasks = []
    for acc, sub in meta.groupby("account_id"):
        start = int(sub["row_start"].iloc[0])
        end = int(sub["row_origin"].max())
        tasks.append((acc, start, values[start:end + 1], months[start],
                      [(int(o) - start, int(h)) for o, h in zip(sub["row_origin"], sub["h"])]))
    results = Parallel(n_jobs=n_jobs)(delayed(_prophet_account)(v, m, t) for _, _, v, m, t in tasks)
    lookup = {}
    for (acc, start, *_), rows in zip(tasks, results):
        for o, h, pred, err in rows:
            lookup[(acc, start + o, h)] = (pred, err)
    pairs = [lookup[(a, int(o), int(h))] for a, o, h in zip(meta["account_id"], meta["row_origin"], meta["h"])]
    return np.array([p for p, _ in pairs]), np.array([bool(e) for _, e in pairs])


# --- глобальні моделі ----------------------------------------------------------------

def hgb_fit_predict(train: Samples, points: Samples, params: dict, h: int, loss: str = "absolute_error",
                    quantile: float | None = None):
    """Навчає HistGradientBoosting на масштабованих train-прикладах для горизонту h і прогнозує точки.

    Returns:
        (прогнози в одиницях витрат для точок із цим h, обрізані нулем; модель).
    """
    from sklearn.ensemble import HistGradientBoostingRegressor

    Xs, factor = scale_inputs(train.X)
    ys = train.y[:, h - 1] / factor
    kwargs = {"quantile": quantile} if quantile is not None else {}
    model = HistGradientBoostingRegressor(loss=loss, random_state=HGB_SEED, early_stopping=False,
                                          **params, **kwargs)
    model.fit(Xs, ys)
    Xp, fp = scale_inputs(points.X)
    return np.clip(model.predict(Xp) * fp, 0, None), model


def build_nn(kind: str, params: dict):
    """Створює нейромережу: вхід — батч вікон (B, 12), вихід — (B, 2) для t+1 і t+2."""
    import torch
    from torch import nn

    class LSTMNet(nn.Module):
        """LSTM з одним шаром і лінійною головою на останньому прихованому стані."""

        def __init__(self, hidden):
            super().__init__()
            self.lstm = nn.LSTM(1, hidden, batch_first=True)
            self.head = nn.Linear(hidden, MAX_H)

        def forward(self, x):
            out, _ = self.lstm(x.unsqueeze(-1))
            return self.head(out[:, -1])

    class TransformerNet(nn.Module):
        """Transformer-encoder: d_model 32, 2 шари, 4 голови, навчені позиційні ембединги."""

        def __init__(self, dropout):
            super().__init__()
            self.proj = nn.Linear(1, 32)
            self.pos = nn.Parameter(torch.randn(1, INPUT_LEN, 32) * 0.02)
            layer = nn.TransformerEncoderLayer(d_model=32, nhead=4, dim_feedforward=64, dropout=dropout,
                                               batch_first=True)
            self.encoder = nn.TransformerEncoder(layer, num_layers=2, enable_nested_tensor=False)
            self.head = nn.Linear(INPUT_LEN * 32, MAX_H)

        def forward(self, x):
            z = self.encoder(self.proj(x.unsqueeze(-1)) + self.pos)
            return self.head(z.flatten(1))

    class DLinear(nn.Module):
        """DLinear: тренд (ковзне середнє з повторенням країв) і залишок, по лінійному шару на кожен."""

        def __init__(self, kernel):
            super().__init__()
            self.kernel = kernel
            self.trend = nn.Linear(INPUT_LEN, MAX_H)
            self.season = nn.Linear(INPUT_LEN, MAX_H)

        def forward(self, x):
            pad = (self.kernel - 1) // 2
            padded = torch.cat([x[:, :1].repeat(1, pad), x, x[:, -1:].repeat(1, pad)], dim=1)
            trend = padded.unfold(1, self.kernel, 1).mean(-1)
            return self.trend(trend) + self.season(x - trend)

    if kind == "lstm":
        return LSTMNet(params["hidden"])
    if kind == "transformer":
        return TransformerNet(params["dropout"])
    if kind == "dlinear":
        return DLinear(params["kernel"])
    raise ValueError(kind)


def train_nn(kind: str, params: dict, seed: int, train: Samples, val: Samples) -> dict:
    """Навчає нейромережу на масштабованих train-прикладах (L1) з early stopping на val.

    Val-втрата — L1 масштабованих прогнозів відповідного горизонту для кожної val-точки.
    Найкращі ваги повертаються.

    Returns:
        Словник pred (прогнози val-точок у одиницях витрат, обрізані нулем), state_dict,
        best_epoch, epochs, seconds.
    """
    import torch

    started = time.perf_counter()
    torch.set_num_threads(1)
    torch.manual_seed(seed)
    model = build_nn(kind, params)
    opt = torch.optim.Adam(model.parameters(), lr=params["lr"])
    Xs, factor = scale_inputs(train.X)
    X = torch.tensor(Xs, dtype=torch.float32)
    Y = torch.tensor(train.y / factor[:, None], dtype=torch.float32)
    Xv_np, fv = scale_inputs(val.X)
    Xv = torch.tensor(Xv_np, dtype=torch.float32)
    hv = torch.tensor(val.meta["h"].to_numpy() - 1)
    yv = torch.tensor(val.y / fv, dtype=torch.float32)
    rows = torch.arange(len(Xv))
    gen = torch.Generator().manual_seed(seed)
    best, best_state, best_epoch, wait, epoch = float("inf"), None, -1, 0, 0
    for epoch in range(NN_MAX_EPOCHS):
        model.train()
        perm = torch.randperm(len(X), generator=gen)
        for b in range(0, len(X), NN_BATCH):
            idx = perm[b:b + NN_BATCH]
            loss = (model(X[idx]) - Y[idx]).abs().mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
        model.eval()
        with torch.no_grad():
            val_loss = (model(Xv)[rows, hv] - yv).abs().mean().item()
        if val_loss < best:
            best, best_state, best_epoch, wait = val_loss, copy.deepcopy(model.state_dict()), epoch, 0
        else:
            wait += 1
            if wait >= NN_PATIENCE:
                break
    model.load_state_dict(best_state)
    model.eval()
    with torch.no_grad():
        pred = model(Xv)[rows, hv].numpy() * fv
    return {"pred": np.clip(pred, 0, None), "state_dict": best_state, "best_epoch": best_epoch,
            "epochs": epoch + 1, "val_loss": best, "seconds": time.perf_counter() - started}


# --- метрики --------------------------------------------------------------------------

def bootstrap_mae_ratio(accounts: np.ndarray, ae_model: np.ndarray, ae_base: np.ndarray,
                        reps: int = BOOTSTRAP_REPS, seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    """95 % перцентильний інтервал відношення MAE моделі до базової (парний bootstrap по рахунках).

    Рахунки вибираються з поверненням; обидві моделі оцінюються на тих самих точках
    вибраних рахунків.
    """
    _, codes = np.unique(accounts, return_inverse=True)
    n_acc = codes.max() + 1
    sum_model = np.bincount(codes, ae_model, n_acc)
    sum_base = np.bincount(codes, ae_base, n_acc)
    idx = np.random.default_rng(seed).integers(0, n_acc, size=(reps, n_acc))
    ratios = sum_model[idx].sum(axis=1) / sum_base[idx].sum(axis=1)
    lo, hi = np.percentile(ratios, [2.5, 97.5])
    return float(lo), float(hi)


def point_metrics(actual: np.ndarray, pred: np.ndarray, scale: np.ndarray) -> dict:
    """pooled MAE, MASE (|e| / масштаб рахунку), WAPE і bias (прогноз − факт) на точках."""
    e = pred - actual
    ae = np.abs(e)
    total = np.abs(actual).sum()
    return {"mae": ae.mean(), "mase": (ae / scale).mean(), "wape": ae.sum() / total if total else np.nan,
            "bias": e.mean()}


def metrics_table(meta: pd.DataFrame, actual: np.ndarray, scale: np.ndarray, preds: dict,
                  best_simple: str | None, intervals: dict | None = None) -> pd.DataFrame:
    """Таблиця метрик моделей на однакових точках.

    Args:
        meta: Метадані точок (account_id).
        actual: Факт.
        scale: Масштаб MASE рахунку кожної точки.
        preds: {модель: прогнози}; має містити "naive" і best_simple.
        best_simple: Модель для відношень і bootstrap; None — без відношень.
        intervals: {модель: (lo, hi)} для довідкового покриття [P10, P90].

    Returns:
        Рядок на модель: n_points, n_accounts, mae, mase, wape, bias, відношення MAE і MASE
        до best_simple, 95 % bootstrap-інтервал відношення MAE, частка рахунків, де MAE < naive,
        покриття.
    """
    acc = meta["account_id"].to_numpy()
    naive_ae = pd.Series(np.abs(preds["naive"] - actual)).groupby(acc).mean()
    base = point_metrics(actual, preds[best_simple], scale) if best_simple else None
    base_ae = np.abs(preds[best_simple] - actual) if best_simple else None
    rows = []
    for name, pred in preds.items():
        m = point_metrics(actual, pred, scale)
        ae = np.abs(pred - actual)
        row = {"model": name, "n_points": len(actual), "n_accounts": len(np.unique(acc)), **m}
        if base is not None:
            lo, hi = bootstrap_mae_ratio(acc, ae, base_ae)
            row.update({"mae_ratio": m["mae"] / base["mae"], "mase_ratio": m["mase"] / base["mase"],
                        "mae_ratio_ci_lo": lo, "mae_ratio_ci_hi": hi})
        model_ae = pd.Series(ae).groupby(acc).mean()
        row["share_accounts_better_than_naive"] = float((model_ae < naive_ae).mean())
        if intervals and name in intervals:
            lo_i, hi_i = intervals[name]
            ok = np.isfinite(lo_i) & np.isfinite(hi_i)
            row["coverage_p10_p90"] = float(((lo_i <= actual) & (actual <= hi_i))[ok].mean()) if ok.any() else np.nan
            row["coverage_n"] = int(ok.sum())
        rows.append(row)
    return pd.DataFrame(rows)


def seed_summary(table: pd.DataFrame) -> pd.DataFrame:
    """Зводить рядки «модель[sN]» у середнє ± sd по seed; інші моделі лишає як є (sd = 0)."""
    t = table.copy()
    t["family"] = t["model"].str.replace(r"\[s\d+\]$", "", regex=True)
    num = [c for c in t.columns if c not in ("model", "family") and pd.api.types.is_numeric_dtype(t[c])]
    mean = t.groupby("family", sort=False)[num].mean()
    sd = t.groupby("family", sort=False)[num].std(ddof=1).fillna(0.0).add_suffix("_sd")
    n = t.groupby("family", sort=False).size().rename("n_seeds")
    return pd.concat([mean, sd, n], axis=1).reset_index().rename(columns={"family": "model"})


# --- етап val ---------------------------------------------------------------------------

def _pooled_mae(actual, pred):
    """Pooled MAE."""
    return float(np.abs(pred - actual).mean())


def prepare(data_dir: Path, expense_types, min_months: int):
    """Завантажує панель, ділить рахунки, будує приклади й перевіряє розбиття.

    Returns:
        (panel, groups, labels, samples, stats).
    """
    panel = load_panel(data_dir, expense_types, min_months)
    if min_months < MAIN_MIN_MONTHS:
        main = load_panel(data_dir, expense_types, MAIN_MIN_MONTHS)
        base = split_accounts(main["account_id"].unique())
    else:
        base = None
    groups = split_accounts(panel["account_id"].unique(), base=base)
    labels = label_rows(panel, groups)
    samples = build_samples(panel, labels)
    check_no_account_overlap(panel, labels, samples)
    check_train_before_t1(panel, labels, samples)
    return panel, groups, labels, samples, account_stats(panel)


def eligible(points: Samples, stats: pd.DataFrame) -> Samples:
    """Лишає точки рахунків з додатним скінченним масштабом MASE."""
    scale = stats["scale"].reindex(points.meta["account_id"]).to_numpy()
    keep = np.isfinite(scale) & (scale > 0)
    return Samples(points.meta[keep].reset_index(drop=True), points.X[keep], points.y[keep])


def subset(points: Samples, mask: np.ndarray) -> Samples:
    """Повертає точки за булевою маскою."""
    return Samples(points.meta[mask].reset_index(drop=True), points.X[mask], points.y[mask])


def stat_subset_accounts(points: Samples) -> np.ndarray:
    """До STAT_SUBSET_SIZE рахунків з точками, випадково з default_rng(STAT_SUBSET_SEED)."""
    accounts = np.array(sorted(points.meta["account_id"].unique()))
    k = min(STAT_SUBSET_SIZE, len(accounts))
    return np.sort(np.random.default_rng(STAT_SUBSET_SEED).choice(accounts, size=k, replace=False))


def run_val(data_dir: Path, out_dir: Path, expense_types, min_months: int, n_jobs: int) -> None:
    """Етап 1: навчання на train, підбір на val, таблиці метрик val; test не оцінюється."""
    from joblib import Parallel, delayed

    timings = {}
    t0 = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "models").mkdir(exist_ok=True)

    panel, groups, labels, samples, stats = prepare(data_dir, expense_types, min_months)
    values = panel["expense"].to_numpy()
    train = samples["train"]
    val_all = samples["val"]
    val = eligible(val_all, stats)
    n_groups = pd.Series(groups).value_counts().to_dict()
    info = {
        "expense_types": list(expense_types), "min_months": min_months,
        "T1": str(T1), "T2": str(T2), "data_last_month": str(panel["month"].max()),
        "accounts": len(groups), "accounts_by_group": n_groups,
        "train_samples": len(train.y),
        "train_accounts_with_samples": int(train.meta["account_id"].nunique()),
        "train_max_month": str(train.meta["last_month"].max()),
        "val_points_all": len(val_all.y), "val_points_eligible": len(val.y),
        "val_accounts_with_points": int(val_all.meta["account_id"].nunique()),
        "val_accounts_eligible": int(val.meta["account_id"].nunique()),
        "test_points_built_not_evaluated": len(samples["test"].y),
        "test_accounts_with_points": int(samples["test"].meta["account_id"].nunique()),
    }
    timings["prepare"] = time.perf_counter() - t0
    print(json.dumps(info, ensure_ascii=False, indent=1))

    scale = stats["scale"].reindex(val.meta["account_id"]).to_numpy()
    h_arr = val.meta["h"].to_numpy()

    # прості
    t = time.perf_counter()
    simple = simple_predictions(val, values)
    timings["simple"] = time.perf_counter() - t
    config = {"expense_types": list(expense_types), "min_months": min_months, "T1": str(T1), "T2": str(T2),
              "horizons": HORIZONS, "ma_k": {}, "best_simple": {}, "hgb": {}, "nn": {}, "ets": None}
    ma_grid, simple_grid = [], []
    for hname, h in HORIZONS.items():
        mask = h_arr == h
        for k in MA_WINDOWS:
            ma_grid.append({"horizon": hname, "k": k, "mae": _pooled_mae(val.y[mask], simple[f"ma{k}"][mask])})
        best_k = min(MA_WINDOWS, key=lambda k: _pooled_mae(val.y[mask], simple[f"ma{k}"][mask]))
        config["ma_k"][hname] = best_k
        candidates = ["naive", "seasonal_naive", f"ma{best_k}", *FILE_SIMPLE[1:], *FILE_SIMPLE_W]
        for c in candidates:
            simple_grid.append({"horizon": hname, "model": c, "mae": _pooled_mae(val.y[mask], simple[c][mask])})
        config["best_simple"][hname] = min(candidates, key=lambda c: _pooled_mae(val.y[mask], simple[c][mask]))
    pd.DataFrame(ma_grid).to_csv(out_dir / "val_grid_ma.csv", index=False)
    pd.DataFrame(simple_grid).to_csv(out_dir / "val_grid_simple.csv", index=False)

    # HistGradientBoosting
    t = time.perf_counter()
    hgb_grid, hgb_pred, hgb_int = [], {}, {}
    for hname, h in HORIZONS.items():
        mask = h_arr == h
        pts = subset(val, mask)
        fits = Parallel(n_jobs=n_jobs)(delayed(hgb_fit_predict)(train, pts, p, h) for p in HGB_GRID)
        maes = [_pooled_mae(pts.y, f[0]) for f in fits]
        for p, m in zip(HGB_GRID, maes):
            hgb_grid.append({"horizon": hname, **p, "mae": m})
        best_i = int(np.argmin(maes))
        config["hgb"][hname] = HGB_GRID[best_i]
        pred = np.empty(len(val.y))
        pred[mask] = fits[best_i][0]
        hgb_pred[hname] = pred
        pd.to_pickle(fits[best_i][1], out_dir / "models" / f"hgb_{hname}.pkl")
        q = Parallel(n_jobs=2)(delayed(hgb_fit_predict)(train, pts, HGB_GRID[best_i], h, "quantile", qq)
                               for qq in (INTERVAL_ALPHA / 2, 1 - INTERVAL_ALPHA / 2))
        lo, hi = np.full(len(val.y), np.nan), np.full(len(val.y), np.nan)
        lo[mask], hi[mask] = q[0][0], q[1][0]
        hgb_int[hname] = (lo, hi)
        for qq, (_, model) in zip(("p10", "p90"), q):
            pd.to_pickle(model, out_dir / "models" / f"hgb_{hname}_{qq}.pkl")
    pd.DataFrame(hgb_grid).to_csv(out_dir / "val_grid_hgb.csv", index=False)
    timings["hgb"] = time.perf_counter() - t

    # нейромережі
    t = time.perf_counter()
    jobs = [(kind, i, p, s) for kind, grid in NN_GRIDS.items() for i, p in enumerate(grid) for s in NN_SEEDS]
    results = Parallel(n_jobs=n_jobs)(delayed(train_nn)(kind, p, s, train, val) for kind, _, p, s in jobs)
    nn_grid = []
    mask_a = h_arr == HORIZONS["A"]
    for (kind, i, p, s), r in zip(jobs, results):
        nn_grid.append({"model": kind, "config_idx": i, **p, "seed": s, "mae_A": _pooled_mae(val.y[mask_a], r["pred"][mask_a]),
                        "best_epoch": r["best_epoch"], "epochs": r["epochs"], "val_loss": r["val_loss"],
                        "seconds": r["seconds"]})
    nn_grid_df = pd.DataFrame(nn_grid)
    nn_grid_df.to_csv(out_dir / "val_grid_nn.csv", index=False)
    import torch

    nn_pred = {}
    for kind, grid in NN_GRIDS.items():
        means = nn_grid_df[nn_grid_df["model"] == kind].groupby("config_idx")["mae_A"].mean()
        best_i = int(means.idxmin())
        config["nn"][kind] = grid[best_i]
        for (k, i, p, s), r in zip(jobs, results):
            if k == kind and i == best_i:
                nn_pred[f"{kind}[s{s}]"] = r["pred"]
                torch.save(r["state_dict"], out_dir / "models" / f"{kind}_s{s}.pt")
    timings["nn"] = time.perf_counter() - t

    # статистична підмножина: ETS і Prophet
    stat_accounts = stat_subset_accounts(val)
    stat_mask = np.isin(val.meta["account_id"].to_numpy(), stat_accounts)
    stat_pts = subset(val, stat_mask)
    t = time.perf_counter()
    ets = ets_predictions(stat_pts, panel, n_jobs)
    timings["ets"] = time.perf_counter() - t
    sh = stat_pts.meta["h"].to_numpy()
    ets_grid = []
    for name, r in ets.items():
        for hname, h in HORIZONS.items():
            m = sh == h
            ets_grid.append({"config": name, "horizon": hname, "mae": _pooled_mae(stat_pts.y[m], r["pred"][m]),
                             "failed": int(r["failed"][m].sum()),
                             "nonseasonal_fallback": int((r["used"][m] != name).sum())})
    ets_grid_df = pd.DataFrame(ets_grid)
    ets_grid_df.to_csv(out_dir / "val_grid_ets.csv", index=False)
    a = ets_grid_df[ets_grid_df["horizon"] == "A"].set_index("config")["mae"]
    config["ets"] = str(a.idxmin())
    t = time.perf_counter()
    prophet_pred, prophet_failed = prophet_predictions(stat_pts, panel, n_jobs)
    timings["prophet"] = time.perf_counter() - t

    # таблиці
    tables_full, tables_stat, strata = [], [], []
    zero = stats["zero_share"].reindex(val.meta["account_id"]).to_numpy()
    for hname, h in HORIZONS.items():
        mask = h_arr == h
        best = config["best_simple"][hname]
        simple_cols = ["naive", "seasonal_naive", f"ma{config['ma_k'][hname]}", *FILE_SIMPLE[1:], *FILE_SIMPLE_W]
        preds = {c: simple[c][mask] for c in simple_cols}
        preds["hgb"] = hgb_pred[hname][mask]
        preds.update({k: v[mask] for k, v in nn_pred.items()})
        intervals = {"hgb": (hgb_int[hname][0][mask], hgb_int[hname][1][mask])}
        full = metrics_table(val.meta[mask], val.y[mask], scale[mask], preds, best, intervals)
        full.insert(0, "horizon", hname)
        tables_full.append(full)

        smask = mask & stat_mask
        local = sh == h
        spreds = {c: simple[c][smask] for c in simple_cols}
        spreds["hgb"] = hgb_pred[hname][smask]
        spreds.update({k: v[smask] for k, v in nn_pred.items()})
        spreds[f"ets_{config['ets']}"] = ets[config["ets"]]["pred"][local]
        spreds["prophet_product"] = prophet_pred[local]
        sint = {"hgb": (hgb_int[hname][0][smask], hgb_int[hname][1][smask]),
                f"ets_{config['ets']}": (ets[config["ets"]]["lo"][local], ets[config["ets"]]["hi"][local])}
        stat_t = metrics_table(val.meta[smask], val.y[smask], scale[smask], spreds, best, sint)
        stat_t.insert(0, "horizon", hname)
        tables_stat.append(stat_t)

        for stratum in ("0", "<5%", ">=5%"):
            sm = mask & np.array([zero_stratum(z) == stratum for z in zero])
            if not sm.any():
                continue
            st = metrics_table(val.meta[sm], val.y[sm], scale[sm],
                               {c: simple[c][sm] for c in simple_cols}
                               | {"hgb": hgb_pred[hname][sm]} | {k: v[sm] for k, v in nn_pred.items()}, best)
            st.insert(0, "stratum", stratum)
            st.insert(0, "horizon", hname)
            strata.append(st)

    full_df, stat_df, strata_df = pd.concat(tables_full), pd.concat(tables_stat), pd.concat(strata)
    full_df.to_csv(out_dir / "val_metrics_full.csv", index=False)
    stat_df.to_csv(out_dir / "val_metrics_stat_subset.csv", index=False)
    strata_df.to_csv(out_dir / "val_metrics_strata.csv", index=False)
    summary = pd.concat([seed_summary(g).assign(horizon=h, set=s)
                         for s, df in (("full", full_df), ("stat_subset", stat_df))
                         for h, g in df.groupby("horizon", sort=False)])
    summary.to_csv(out_dir / "val_summary.csv", index=False)

    info["stat_subset_accounts"] = len(stat_accounts)
    info["stat_subset_points"] = int(stat_mask.sum())
    info["ets_failed_points"] = {k: int(v["failed"].sum()) for k, v in ets.items()}
    info["prophet_failed_points"] = int(prophet_failed.sum())
    timings["total"] = time.perf_counter() - t0
    info["timings_seconds"] = {k: round(v, 1) for k, v in timings.items()}
    (out_dir / "val_config_candidate.json").write_text(json.dumps(config, ensure_ascii=False, indent=1))
    (out_dir / "val_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=1, default=str))

    cols = ["model", "n_seeds", "mae", "mae_sd", "mase", "mase_sd", "wape", "wape_sd", "bias", "bias_sd",
            "mae_ratio", "mase_ratio", "mae_ratio_ci_hi", "share_accounts_better_than_naive"]
    for (s, h), g in summary.groupby(["set", "horizon"], sort=False):
        print(f"\n=== val, {s}, горизонт {h}, найкраща проста = {config['best_simple'][h]} ===")
        print(g[[c for c in cols if c in g.columns]].to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
    print(json.dumps({"config": config, "timings": info["timings_seconds"]}, ensure_ascii=False, indent=1))


# --- етап test ---------------------------------------------------------------------------

EXPLORATORY_MODELS = ("hgb_sq",)
VERDICT_MODELS_FULL = ("hgb", "lstm", "transformer", "dlinear")
CRITERION_MAE_RATIO = 0.9
CRITERION_MASE_RATIO = 0.9
CRITERION_CI_HI = 1.0
CODE_FILES = ("scripts/analysis/berka_backtest.py", "scripts/analysis/berka_audit.py")


def sha256_file(path: Path) -> str:
    """Повертає sha256 файлу у hex."""
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_revision() -> dict:
    """Повертає поточну git-ревізію (HEAD) і чи є незакомічені зміни."""
    import subprocess

    rev = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    return {"rev": rev, "dirty": bool(status.strip())}


def freeze_record(config_path: Path, models_dir: Path | None) -> dict:
    """Фіксує sha256 конфігурації, файлів коду й (якщо задано) збережених моделей і git-ревізію."""
    record = {
        "config_path": str(config_path.relative_to(ROOT) if config_path.is_relative_to(ROOT) else config_path),
        "config_sha256": sha256_file(config_path),
        "git": git_revision(),
        "code_sha256": {f: sha256_file(ROOT / f) for f in CODE_FILES},
    }
    if models_dir is not None:
        record["models_sha256"] = {p.name: sha256_file(p) for p in sorted(models_dir.iterdir()) if p.is_file()}
    return record


def claim_output_dir(path: Path) -> None:
    """Створює каталог результатів test; відмовляє, якщо test для цього варіанта вже запускався.

    Raises:
        FileExistsError: каталог уже існує.
    """
    if path.exists():
        raise FileExistsError(f"{path} уже існує: test для цього варіанта запускався, повторний запуск заборонено")
    path.mkdir(parents=True)


def predict_nn(kind: str, params: dict, state_dict, points: Samples) -> np.ndarray:
    """Прогнозує точки нейромережею зі збереженими вагами; результат у одиницях витрат, обрізаний нулем."""
    import torch

    model = build_nn(kind, params)
    model.load_state_dict(state_dict)
    model.eval()
    Xs, factor = scale_inputs(points.X)
    rows = torch.arange(len(Xs))
    h = torch.tensor(points.meta["h"].to_numpy() - 1)
    with torch.no_grad():
        out = model(torch.tensor(Xs, dtype=torch.float32))[rows, h].numpy()
    return np.clip(out * factor, 0, None)


def _hgb_predict(model, points: Samples) -> np.ndarray:
    """Прогноз збереженої HGB-моделі на точках у одиницях витрат, обрізаний нулем."""
    Xs, factor = scale_inputs(points.X)
    return np.clip(model.predict(Xs) * factor, 0, None)


def criterion_table(table: pd.DataFrame, best_simple: str, families: tuple[str, ...]) -> pd.DataFrame:
    """Застосовує критерій передреєстрації до моделей таблиці метрик.

    Умови (усі обов'язкові): mae_ratio <= 0.9; mase_ratio <= 0.9; |bias| <= |bias| best_simple;
    верхня межа 95 % bootstrap-інтервалу відношення MAE < 1.0. Для моделей з кількома seed
    («модель[sN]») сімейство проходить, лише якщо проходять усі seed.

    Returns:
        Рядок на сімейство: n_seeds, кожна умова (частка seed, що її виконали), passed, failed.
    """
    base_bias = abs(float(table.loc[table["model"] == best_simple, "bias"].iloc[0]))
    t = table.copy()
    t["family"] = t["model"].str.replace(r"\[s\d+\]$", "", regex=True)
    t = t[t["family"].isin(families)]
    conditions = {
        "mae_ratio_le_0.9": t["mae_ratio"] <= CRITERION_MAE_RATIO,
        "mase_ratio_le_0.9": t["mase_ratio"] <= CRITERION_MASE_RATIO,
        "abs_bias_not_worse": t["bias"].abs() <= base_bias,
        "ci_hi_lt_1": t["mae_ratio_ci_hi"] < CRITERION_CI_HI,
    }
    flags = pd.DataFrame(conditions).assign(family=t["family"].to_numpy())
    rows = []
    for family, g in flags.groupby("family", sort=False):
        shares = {c: float(g[c].mean()) for c in conditions}
        failed = [c for c in conditions if not g[c].all()]
        rows.append({"model": family, "n_seeds": len(g), **shares, "passed": not failed,
                     "failed": ";".join(failed)})
    return pd.DataFrame(rows)


def _strata_tables(meta, y, scale, zero, preds, best) -> pd.DataFrame:
    """Таблиці метрик по стратах частки нульових місяців."""
    out = []
    strata = np.array([zero_stratum(z) for z in zero])
    for stratum in ("0", "<5%", ">=5%"):
        m = strata == stratum
        if m.any():
            t = metrics_table(meta[m], y[m], scale[m], {k: v[m] for k, v in preds.items()}, best)
            t.insert(0, "stratum", stratum)
            out.append(t)
    return pd.concat(out)


def run_test(data_dir: Path, out_dir: Path, expense_types, min_months: int, n_jobs: int) -> None:
    """Етап 2: одноразова оцінка на test із замороженою конфігурацією етапу val."""
    from joblib import Parallel, delayed
    import torch

    t0 = time.perf_counter()
    config_path = out_dir / "val_config_candidate.json"
    config = json.loads(config_path.read_text())
    retrain = not (list(expense_types) == config["expense_types"] and min_months == config["min_months"])
    variant = f"{'+'.join(expense_types)}_min{min_months}"
    run_dir = out_dir / f"test_{variant}"
    models_dir = out_dir / "models"
    frozen = freeze_record(config_path, None if retrain else models_dir)
    frozen.update({"variant": variant, "retrain_global_models": retrain,
                   "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    claim_output_dir(run_dir)
    (run_dir / "frozen.json").write_text(json.dumps(frozen, ensure_ascii=False, indent=1))
    print(json.dumps(frozen, ensure_ascii=False, indent=1))

    panel, groups, labels, samples, stats = prepare(data_dir, expense_types, min_months)
    values = panel["expense"].to_numpy()
    train, val = samples["train"], eligible(samples["val"], stats)
    test_all = samples["test"]
    test = eligible(test_all, stats)
    h_arr = test.meta["h"].to_numpy()
    scale = stats["scale"].reindex(test.meta["account_id"]).to_numpy()
    zero = stats["zero_share"].reindex(test.meta["account_id"]).to_numpy()
    info = {
        "variant": variant, "accounts": len(groups), "accounts_by_group": pd.Series(groups).value_counts().to_dict(),
        "train_samples": len(train.y), "train_max_month": str(train.meta["last_month"].max()),
        "test_points_all": len(test_all.y), "test_points_eligible": len(test.y),
        "test_accounts_with_points": int(test_all.meta["account_id"].nunique()),
        "test_accounts_eligible": int(test.meta["account_id"].nunique()),
        "test_target_months": [str(test.meta["last_month"].min()), str(test.meta["last_month"].max())],
    }
    timings = {"prepare": time.perf_counter() - t0}

    t = time.perf_counter()
    simple = simple_predictions(test, values)
    timings["simple"] = time.perf_counter() - t

    t = time.perf_counter()
    hgb, hgb_sq, hgb_lo, hgb_hi = (np.full(len(test.y), np.nan) for _ in range(4))
    for hname, h in HORIZONS.items():
        mask = h_arr == h
        pts = subset(test, mask)
        params = config["hgb"][hname]
        if retrain:
            fits = Parallel(n_jobs=3)(delayed(hgb_fit_predict)(train, pts, params, h, loss, q) for loss, q in
                                      (("absolute_error", None), ("quantile", INTERVAL_ALPHA / 2),
                                       ("quantile", 1 - INTERVAL_ALPHA / 2)))
            hgb[mask], hgb_lo[mask], hgb_hi[mask] = (f[0] for f in fits)
        else:
            hgb[mask] = _hgb_predict(pd.read_pickle(models_dir / f"hgb_{hname}.pkl"), pts)
            hgb_lo[mask] = _hgb_predict(pd.read_pickle(models_dir / f"hgb_{hname}_p10.pkl"), pts)
            hgb_hi[mask] = _hgb_predict(pd.read_pickle(models_dir / f"hgb_{hname}_p90.pkl"), pts)
        hgb_sq[mask] = hgb_fit_predict(train, pts, params, h, loss="squared_error")[0]
    timings["hgb"] = time.perf_counter() - t

    t = time.perf_counter()
    nn_pred, nn_meta = {}, []
    for kind in NN_GRIDS:
        params = config["nn"][kind]
        if retrain:
            results = Parallel(n_jobs=n_jobs)(delayed(train_nn)(kind, params, s, train, val) for s in NN_SEEDS)
            states = [r["state_dict"] for r in results]
            nn_meta += [{"model": kind, "seed": s, "best_epoch": r["best_epoch"], "epochs": r["epochs"],
                         "seconds": r["seconds"]} for s, r in zip(NN_SEEDS, results)]
        else:
            states = [torch.load(models_dir / f"{kind}_s{s}.pt", weights_only=True) for s in NN_SEEDS]
        for s, state in zip(NN_SEEDS, states):
            nn_pred[f"{kind}[s{s}]"] = predict_nn(kind, params, state, test)
    if nn_meta:
        pd.DataFrame(nn_meta).to_csv(run_dir / "retrain_nn.csv", index=False)
    timings["nn"] = time.perf_counter() - t

    stat_accounts = stat_subset_accounts(test)
    stat_mask = np.isin(test.meta["account_id"].to_numpy(), stat_accounts)
    stat_pts = subset(test, stat_mask)
    sh = stat_pts.meta["h"].to_numpy()
    ets_name = config["ets"]
    t = time.perf_counter()
    ets = ets_predictions(stat_pts, panel, n_jobs, configs=(ets_name,))[ets_name]
    timings["ets"] = time.perf_counter() - t
    t = time.perf_counter()
    prophet_pred, prophet_failed = prophet_predictions(stat_pts, panel, n_jobs)
    timings["prophet"] = time.perf_counter() - t

    full_tables, stat_tables, strata_tables, verdicts = [], [], [], []
    for hname, h in HORIZONS.items():
        best = config["best_simple"][hname]
        simple_cols = ["naive", "seasonal_naive", f"ma{config['ma_k'][hname]}", *FILE_SIMPLE[1:], *FILE_SIMPLE_W]
        mask = h_arr == h
        preds = {c: simple[c][mask] for c in simple_cols}
        preds |= {"hgb": hgb[mask], "hgb_sq": hgb_sq[mask]} | {k: v[mask] for k, v in nn_pred.items()}
        full = metrics_table(test.meta[mask], test.y[mask], scale[mask], preds, best,
                             {"hgb": (hgb_lo[mask], hgb_hi[mask])})
        full.insert(0, "horizon", hname)
        full_tables.append(full)
        v = criterion_table(full, best, VERDICT_MODELS_FULL)
        v.insert(0, "set", "full")
        v.insert(0, "horizon", hname)
        verdicts.append(v)

        smask, local = mask & stat_mask, sh == h
        spreds = {c: simple[c][smask] for c in simple_cols}
        spreds |= {"hgb": hgb[smask], "hgb_sq": hgb_sq[smask]} | {k: v[smask] for k, v in nn_pred.items()}
        spreds |= {f"ets_{ets_name}": ets["pred"][local], "prophet_product": prophet_pred[local]}
        st = metrics_table(test.meta[smask], test.y[smask], scale[smask], spreds, best,
                           {"hgb": (hgb_lo[smask], hgb_hi[smask]),
                            f"ets_{ets_name}": (ets["lo"][local], ets["hi"][local])})
        st.insert(0, "horizon", hname)
        stat_tables.append(st)
        v = criterion_table(st, best, (f"ets_{ets_name}", "prophet_product"))
        v.insert(0, "set", "stat_subset")
        v.insert(0, "horizon", hname)
        verdicts.append(v)

        strata = _strata_tables(test.meta[mask], test.y[mask], scale[mask], zero[mask], preds, best)
        strata.insert(0, "horizon", hname)
        strata_tables.append(strata)

    full_df, stat_df = pd.concat(full_tables), pd.concat(stat_tables)
    verdict_df, strata_df = pd.concat(verdicts), pd.concat(strata_tables)
    full_df.to_csv(run_dir / "test_metrics_full.csv", index=False)
    stat_df.to_csv(run_dir / "test_metrics_stat_subset.csv", index=False)
    strata_df.to_csv(run_dir / "test_metrics_strata.csv", index=False)
    verdict_df.to_csv(run_dir / "test_verdict.csv", index=False)
    summary = pd.concat([seed_summary(g).assign(horizon=h, set=s)
                         for s, df in (("full", full_df), ("stat_subset", stat_df))
                         for h, g in df.groupby("horizon", sort=False)])
    summary.to_csv(run_dir / "test_summary.csv", index=False)
    val_summary_path = out_dir / "val_summary.csv"
    if not retrain and val_summary_path.exists():
        cols = ["set", "horizon", "model", "mae", "mase", "bias", "mae_ratio", "mase_ratio"]
        val_s = pd.read_csv(val_summary_path)[cols]
        both = val_s.merge(summary[cols], on=["set", "horizon", "model"], suffixes=("_val", "_test"))
        both.to_csv(run_dir / "val_vs_test.csv", index=False)

    info.update({"stat_subset_accounts": len(stat_accounts), "stat_subset_points": int(stat_mask.sum()),
                 "ets_config": ets_name, "ets_failed_points": int(ets["failed"].sum()),
                 "ets_nonseasonal_fallback": int((ets["used"] != ets_name).sum()),
                 "prophet_failed_points": int(prophet_failed.sum()), "exploratory_models": list(EXPLORATORY_MODELS)})
    timings["total"] = time.perf_counter() - t0
    info["timings_seconds"] = {k: round(v, 1) for k, v in timings.items()}
    (run_dir / "test_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=1, default=str))

    show = ["model", "n_seeds", "mae", "mae_sd", "mase", "wape", "bias", "bias_sd", "mae_ratio", "mase_ratio",
            "mae_ratio_ci_lo", "mae_ratio_ci_hi", "share_accounts_better_than_naive"]
    for (s, h), g in summary.groupby(["set", "horizon"], sort=False):
        print(f"\n=== test {variant}, {s}, горизонт {h}, найкраща проста = {config['best_simple'][h]} ===")
        print(g[[c for c in show if c in g.columns]].to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
    print("\n=== вердикт за передреєстрацією ===")
    print(verdict_df.to_string(index=False))
    print(json.dumps(info, ensure_ascii=False, indent=1, default=str))


def main() -> None:
    """Розбирає аргументи командного рядка і запускає етап val або test."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("val", "test"):
        v = sub.add_parser(name)
        v.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
        v.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
        v.add_argument("--expense-types", nargs="+", default=[",".join(DEFAULT_EXPENSE_TYPES)])
        v.add_argument("--min-months", type=int, default=MAIN_MIN_MONTHS)
        v.add_argument("--n-jobs", type=int, default=os.cpu_count())
    args = p.parse_args()
    types = [t for x in args.expense_types for t in x.split(",") if t]
    run = run_val if args.cmd == "val" else run_test
    run(args.data_dir, args.out_dir, types, args.min_months, args.n_jobs)


if __name__ == "__main__":
    sys.exit(main())
