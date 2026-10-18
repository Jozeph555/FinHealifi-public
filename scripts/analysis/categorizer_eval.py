"""
Оцінка якості ML-категоризатора. Лише читає БД; файли моделей не змінюються.

Схеми розбиття:
- group — 5-fold StratifiedGroupKFold, група = нормалізований опис (нижній
  регістр, цифри -> #): жоден опис не потрапляє одночасно в train і test;
- random — один стратифікований split 80/20, як у _fit_default_classifier;
  показує, наскільки результат залежить від повторюваних описів.

Пул і модель ті самі, що в навчанні: ml_categorizer._load_pool,
_drop_rare_classes і _fit_resampled (SMOTE + LogisticRegression). Поріг
впевненості — значення за замовчуванням predict_category. Якщо в train-частині
фолду в класу менше 2 прикладів, клас у цьому фолді не вчиться, а його
тестові рядки рахуються помилками; такі випадки виводяться у звіті.

Бейзлайни на тих самих фолдах: мажоритарний клас train-частини і пошук
«нормалізований опис -> найчастіша мітка в train» (якщо опису в train немає —
мажоритарний клас). Стеля — частка рядків, яку може вгадати будь-яка функція
від нормалізованого опису.

Запуск (з кореня репозиторію; основна БД — лише читання):
    ALLOW_MAIN_DB=1 python -m scripts.analysis.categorizer_eval \\
        --expect-db-port 5436 [--out-dir scripts/analysis/out]
Коди виходу: 0 — успіх; 2 — відмова перевірки цілі БД.
"""
import argparse
import asyncio
import csv
import inspect
import os
import re
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from sklearn.metrics import accuracy_score, f1_score, precision_recall_fscore_support
from sklearn.model_selection import StratifiedGroupKFold, train_test_split
from sqlalchemy import text

from app.services.ml_categorizer import (
    _drop_rare_classes,
    _fit_resampled,
    _get_embedding_model,
    _load_pool,
    predict_category,
)

SEED = 42
N_SPLITS = 5
N_BOOTSTRAP = 1000
# Поріг, що діє під час категоризації (значення за замовчуванням predict_category).
CONFIDENCE_THRESHOLD = inspect.signature(predict_category).parameters["threshold"].default
# Як у _fit_default_classifier.
RANDOM_TEST_SIZE = 0.2
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "out"

SCHEMES = ("random", "group")
MODELS = ("model", "majority", "lookup")


def normalize_description(description: str | None) -> str:
    """Нормалізує опис транзакції для групування.

    Нижній регістр, кожна група цифр -> '#', інші не-словесні символи -> пробіл,
    пробіли схлопнуто: «Київстар 380671234567» -> «київстар #».
    """
    s = (description or "").lower()
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"[^\w#]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def group_folds(labels, groups, n_splits: int = N_SPLITS, seed: int = SEED):
    """Розбиває вибірку на фолди так, що кожна група цілком у train або цілком у test.

    Returns:
        Список пар (train_idx, test_idx).
    """
    splitter = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    return list(splitter.split(np.zeros(len(labels)), labels, groups))


def random_split(labels, seed: int = SEED):
    """Повертає один стратифікований split 80/20, як у _fit_default_classifier."""
    idx = np.arange(len(labels))
    train_idx, test_idx = train_test_split(
        idx, test_size=RANDOM_TEST_SIZE, random_state=seed, stratify=labels
    )
    return [(train_idx, test_idx)]


def compute_metrics(y_true, y_pred, confidence, threshold: float = CONFIDENCE_THRESHOLD) -> dict:
    """Рахує accuracy, macro-F1 і weighted-F1.

    Якщо передано confidence, рахує також частку передбачень з упевненістю не
    нижчою за threshold і точність на цій частці.

    Returns:
        Словник n, accuracy, macro_f1, weighted_f1, coverage,
        precision_at_threshold (останні два — None без confidence).
    """
    y_true, y_pred = list(y_true), list(y_pred)
    result = {
        "n": len(y_true),
        "accuracy": accuracy_score(y_true, y_pred),
        "macro_f1": f1_score(y_true, y_pred, average="macro", zero_division=0),
        "weighted_f1": f1_score(y_true, y_pred, average="weighted", zero_division=0),
        "coverage": None,
        "precision_at_threshold": None,
    }
    if confidence is not None:
        covered = np.asarray(confidence) >= threshold
        result["coverage"] = float(covered.mean())
        if covered.any():
            result["precision_at_threshold"] = float(
                np.mean(np.asarray(y_true)[covered] == np.asarray(y_pred)[covered])
            )
    return result


def bootstrap_macro_f1_ci(y_true, y_pred, n: int = N_BOOTSTRAP, seed: int = SEED):
    """Повертає 95 % перцентильний інтервал macro-F1 (бутстреп по тестових рядках)."""
    y_true, y_pred = np.asarray(y_true), np.asarray(y_pred)
    rng = np.random.default_rng(seed)
    scores = []
    for _ in range(n):
        idx = rng.integers(0, len(y_true), len(y_true))
        scores.append(f1_score(y_true[idx], y_pred[idx], average="macro", zero_division=0))
    lo, hi = np.percentile(scores, [2.5, 97.5])
    return float(lo), float(hi)


def ambiguity_stats(groups, labels) -> dict:
    """Рахує описи з кількома мітками і стелю точності.

    Стеля — частка рядків, яку вгадує найкраща функція від нормалізованого опису
    (у кожній групі — найчастіша мітка).
    """
    by_group = defaultdict(Counter)
    for g, label in zip(groups, labels):
        by_group[g][label] += 1
    ambiguous = [c for c in by_group.values() if len(c) > 1]
    return {
        "n_groups": len(by_group),
        "ambiguous_groups": len(ambiguous),
        "rows_in_ambiguous_groups": sum(sum(c.values()) for c in ambiguous),
        "ceiling_accuracy": sum(c.most_common(1)[0][1] for c in by_group.values()) / len(labels),
    }


def _run_scheme(folds, embeddings, labels, groups):
    """Повертає передбачення моделі й бейзлайнів на тестових рядках усіх фолдів."""
    out = {m: [] for m in MODELS}
    y_true, confidence = [], []
    lookup_hits = seen = 0
    skipped = []  # (фолд, клас) — клас не вчився у фолді (< 2 прикладів у train)
    for fold_no, (train_idx, test_idx) in enumerate(folds):
        counts = Counter(labels[train_idx])
        learnable = np.array([counts[label] >= 2 for label in labels[train_idx]])
        skipped += [(fold_no, c) for c in sorted(set(labels)) if counts[c] < 2]
        fit_idx = train_idx[learnable]

        clf = _fit_resampled(embeddings[fit_idx], labels[fit_idx])
        proba = clf.predict_proba(embeddings[test_idx])
        out["model"] += list(clf.classes_[proba.argmax(axis=1)])
        confidence += list(proba.max(axis=1))

        majority = counts.most_common(1)[0][0]
        out["majority"] += [majority] * len(test_idx)

        by_desc = defaultdict(Counter)
        for i in train_idx:
            by_desc[groups[i]][labels[i]] += 1
        for i in test_idx:
            if groups[i] in by_desc:
                lookup_hits += 1
                out["lookup"].append(by_desc[groups[i]].most_common(1)[0][0])
            else:
                out["lookup"].append(majority)
        train_groups = {groups[i] for i in train_idx}
        seen += sum(groups[i] in train_groups for i in test_idx)
        y_true += list(labels[test_idx])
    return y_true, out, confidence, lookup_hits, seen, skipped


def evaluate(texts, labels, embeddings) -> dict:
    """Оцінює модель і бейзлайни за обома схемами розбиття.

    Args:
        texts: Описи пулу після _drop_rare_classes.
        labels: Мітки пулу.
        embeddings: Ембедінги описів.

    Returns:
        Звіт: метрики, бутстреп-інтервал, частка тестових описів, що є в train,
        таблиця по класах для кожної схеми, а також стеля точності.
    """
    labels = np.asarray(labels, dtype=object)
    embeddings = np.asarray(embeddings)
    groups = np.asarray([normalize_description(t) for t in texts], dtype=object)

    report = {
        "n_rows": len(labels),
        "n_classes": len(set(labels)),
        "ambiguity": ambiguity_stats(groups, labels),
        "threshold": CONFIDENCE_THRESHOLD,
    }
    splits = {"random": random_split(labels), "group": group_folds(labels, groups)}
    for scheme in SCHEMES:
        y_true, preds, confidence, lookup_hits, seen, skipped = _run_scheme(
            splits[scheme], embeddings, labels, groups
        )
        n_test = len(y_true)
        classes = sorted(set(labels))
        p, r, f, s = precision_recall_fscore_support(
            y_true, preds["model"], labels=classes, zero_division=0
        )
        report[scheme] = {
            "n_folds": len(splits[scheme]),
            "n_test": n_test,
            "test_desc_seen_in_train": seen / n_test,
            "lookup_hit_rate": lookup_hits / n_test,
            "skipped_classes": skipped,
            "metrics": {
                m: compute_metrics(y_true, preds[m], confidence if m == "model" else None)
                for m in MODELS
            },
            "macro_f1_ci": bootstrap_macro_f1_ci(y_true, preds["model"]),
            "per_class": [
                {"class": c, "precision": float(pi), "recall": float(ri), "f1": float(fi), "support": int(si)}
                for c, pi, ri, fi, si in zip(classes, p, r, f, s)
            ],
        }
    return report


def _fmt(x, digits=3):
    """Форматує число з заданою кількістю знаків або «—» для None."""
    return "—" if x is None else f"{x:.{digits}f}"


def format_report(report: dict) -> str:
    """Форматує звіт evaluate() як текстові таблиці."""
    lines = [
        f"\n=== Оцінка категоризатора: {report['n_rows']} прикладів, {report['n_classes']} класів ===",
        "",
        "| схема | модель | acc | macro-F1 | weighted-F1 | покриття ≥{:.2f} | точність на покритті |".format(
            report["threshold"]
        ),
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for scheme in SCHEMES:
        for m in MODELS:
            x = report[scheme]["metrics"][m]
            lines.append(
                f"| {scheme} | {m} | {_fmt(x['accuracy'])} | {_fmt(x['macro_f1'])} | {_fmt(x['weighted_f1'])} "
                f"| {_fmt(x['coverage'])} | {_fmt(x['precision_at_threshold'])} |"
            )
    lines.append("")
    for scheme in SCHEMES:
        r = report[scheme]
        lo, hi = r["macro_f1_ci"]
        lines.append(
            f"{scheme}: фолдів {r['n_folds']}, тестових рядків {r['n_test']}; "
            f"macro-F1 моделі {_fmt(r['metrics']['model']['macro_f1'])} "
            f"(95 % бутстреп-інтервал {_fmt(lo)}–{_fmt(hi)}); "
            f"тестових описів, що є в train: {r['test_desc_seen_in_train']:.1%}; "
            f"lookup знайшов опис у train: {r['lookup_hit_rate']:.1%}"
        )
        if r["skipped_classes"]:
            lines.append(f"  класи, що не вчились у фолді (< 2 прикладів у train): {r['skipped_classes']}")
    a = report["ambiguity"]
    lines += [
        "",
        f"Стеля: {a['ambiguous_groups']} з {a['n_groups']} нормалізованих описів мають > 1 мітки "
        f"({a['rows_in_ambiguous_groups']} рядків); найкраща можлива точність будь-якої функції опису — "
        f"{a['ceiling_accuracy']:.1%}",
        "",
        "По класах (group, модель):",
        "| клас | precision | recall | F1 | support |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in sorted(report["group"]["per_class"], key=lambda r: -r["support"]):
        lines.append(
            f"| {row['class']} | {_fmt(row['precision'], 2)} | {_fmt(row['recall'], 2)} "
            f"| {_fmt(row['f1'], 2)} | {row['support']} |"
        )
    return "\n".join(lines)


def write_csv(report: dict, path: Path) -> None:
    """Записує звіт у CSV довгого формату: scheme, model, class, metric, value."""
    rows = []
    for scheme in SCHEMES:
        r = report[scheme]
        for m in MODELS:
            for metric, value in r["metrics"][m].items():
                rows.append((scheme, m, "", metric, value))
        lo, hi = r["macro_f1_ci"]
        rows += [
            (scheme, "model", "", "macro_f1_ci_low", lo),
            (scheme, "model", "", "macro_f1_ci_high", hi),
            (scheme, "", "", "test_desc_seen_in_train", r["test_desc_seen_in_train"]),
            (scheme, "lookup", "", "hit_rate", r["lookup_hit_rate"]),
        ]
        for row in r["per_class"]:
            for metric in ("precision", "recall", "f1", "support"):
                rows.append((scheme, "model", row["class"], metric, row[metric]))
    for metric, value in report["ambiguity"].items():
        rows.append(("", "", "", metric, value))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["scheme", "model", "class", "metric", "value"])
        writer.writerows(rows)


def evaluation_report(texts, labels, embeddings) -> str:
    """Повертає текстовий звіт оцінки; передається в ml_categorizer.train_and_save()."""
    return format_report(evaluate(texts, labels, embeddings))


async def main(expect_db_port: int, out_dir: Path) -> int:
    """Завантажує пул з основної БД (лише читання), оцінює модель і записує CSV.

    Returns:
        Код виходу: 0 — успіх, 2 — відмова перевірки цілі БД.
    """
    from app.models.database import AsyncSessionLocal, engine
    from scripts.data_fixes._db_guard import DbGuardRefused, check_db_target

    engine.echo = False
    try:
        target_db = check_db_target(engine.url, expect_db_port, os.environ)
    except DbGuardRefused as e:
        print(f"ВІДМОВА (db_guard): {e}. До БД не підключались.")
        return 2
    print(f"БД: {target_db} (лише читання)")

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                texts, labels, owners = await _load_pool(session=session)
                texts, labels, owners = _drop_rare_classes(texts, labels, owners)
        finally:
            await session.rollback()
    for w in caught:
        print(f"Попередження пулу: {w.message}")

    embeddings = _get_embedding_model().encode(texts, show_progress_bar=False)
    report = evaluate(texts, labels, embeddings)
    print(format_report(report))

    path = out_dir / "categorizer_eval.csv"
    write_csv(report, path)
    print(f"\nCSV: {path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expect-db-port", type=int, required=True)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.expect_db_port, args.out_dir)))
