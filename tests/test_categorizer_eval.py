"""
Тести оцінки ML-категоризатора (scripts/analysis/categorizer_eval.py).

(а) групове розбиття: нормалізований опис не потрапляє і в train, і в test
    одного фолду;
(б) нормалізація опису: регістр і цифри;
(в) функції метрик на синтетичних прикладах з відомим результатом.

Без БД і без embedding-моделі: синтетичні описи й випадкові вектори.
"""
import numpy as np
import pytest

from scripts.analysis.categorizer_eval import (
    ambiguity_stats,
    bootstrap_macro_f1_ci,
    compute_metrics,
    evaluate,
    group_folds,
    normalize_description,
)


def _synthetic_pool():
    # Описи повторюються з різним регістром і цифрами — як «Сільпо» в реальному пулі.
    texts, labels = [], []
    for i in range(12):
        texts += [f"Сільпо {i}", f"СІЛЬПО #{i * 7}", f"АТБ маркет {i}"]
        labels += ["Продукти", "Продукти", "Продукти"]
        texts += [f"WOG АЗС {i}", f"wog азс {i + 100}"]
        labels += ["АЗС", "АЗС"]
        texts += [f"Аптека Подорожник {i}", f"аптека {i}"]
        labels += ["Здоров'я", "Здоров'я"]
        texts += [f"Uklon поїздка {i}"]
        labels += ["Транспорт"]
    # Кілька унікальних описів, щоб у кожного класу було більше груп, ніж фолдів.
    for j, (label, word) in enumerate(
        [("Продукти", "Фора"), ("Продукти", "Novus"), ("АЗС", "OKKO"), ("АЗС", "Shell"),
         ("Здоров'я", "Бажаємо здоров'я"), ("Здоров'я", "Аптека 911"), ("Транспорт", "Bolt"),
         ("Транспорт", "Метро"), ("Транспорт", "Таксі"), ("АЗС", "Укрнафта"), ("Продукти", "Сільпо експрес"),
         ("Здоров'я", "Ескулаб"), ("Транспорт", "Укрзалізниця")]
    ):
        texts.append(word)
        labels.append(label)
    return texts, labels


# --- (а) групове розбиття ---------------------------------------------------

def test_group_folds_never_share_description_between_train_and_test():
    texts, labels = _synthetic_pool()
    groups = [normalize_description(t) for t in texts]
    folds = group_folds(labels, groups)

    assert len(folds) == 5
    seen_in_test = []
    for train_idx, test_idx in folds:
        train_desc = {groups[i] for i in train_idx}
        test_desc = {groups[i] for i in test_idx}
        assert train_desc & test_desc == set()
        seen_in_test += list(test_idx)
    # Кожен рядок — у test рівно одного фолду.
    assert sorted(seen_in_test) == list(range(len(texts)))


def test_evaluate_reports_zero_overlap_and_zero_lookup_hits_on_group_folds():
    texts, labels = _synthetic_pool()
    embeddings = np.random.default_rng(0).normal(size=(len(texts), 8))

    report = evaluate(texts, labels, embeddings)

    assert report["group"]["test_desc_seen_in_train"] == 0.0
    assert report["group"]["lookup_hit_rate"] == 0.0
    # Випадковий split тих самих даних: описи перетинаються.
    assert report["random"]["test_desc_seen_in_train"] > 0.5


# --- (б) нормалізація ---------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Сільпо", "сільпо"),
        ("СІЛЬПО", "сільпо"),
        ("Київстар 380671234567", "київстар #"),
        ("Оплата 12.05.2024", "оплата # # #"),
        ("  WOG   АЗС-17 ", "wog азс #"),
        ("**** 1234", "#"),
        (None, ""),
    ],
)
def test_normalize_description(raw, expected):
    assert normalize_description(raw) == expected


def test_normalize_description_merges_case_and_digit_variants():
    assert normalize_description("Сільпо 123") == normalize_description("сІЛЬПО 7")


# --- (в) метрики -----------------------------------------------------------

def test_compute_metrics_known_values():
    y_true = ["a", "a", "b", "b"]
    y_pred = ["a", "b", "b", "b"]
    confidence = [0.9, 0.5, 0.7, 0.6]

    m = compute_metrics(y_true, y_pred, confidence, threshold=0.6)

    assert m["n"] == 4
    assert m["accuracy"] == pytest.approx(0.75)
    # F1(a) = 2·1·0.5/1.5 = 2/3; F1(b) = 2·(2/3)·1/(5/3) = 0.8.
    assert m["macro_f1"] == pytest.approx((2 / 3 + 0.8) / 2)
    assert m["weighted_f1"] == pytest.approx((2 / 3 + 0.8) / 2)
    # Поріг включний (як у predict_category: max_proba >= threshold) — 3 з 4.
    assert m["coverage"] == pytest.approx(0.75)
    assert m["precision_at_threshold"] == pytest.approx(1.0)


def test_compute_metrics_without_confidence_has_no_coverage():
    m = compute_metrics(["a", "b"], ["a", "a"], None, threshold=0.6)
    assert m["accuracy"] == pytest.approx(0.5)
    assert m["coverage"] is None
    assert m["precision_at_threshold"] is None


def test_bootstrap_ci_is_reproducible_and_brackets_point():
    rng = np.random.default_rng(1)
    y_true = list(rng.choice(["a", "b", "c"], size=60))
    y_pred = [t if rng.random() < 0.7 else "a" for t in y_true]

    lo, hi = bootstrap_macro_f1_ci(y_true, y_pred)
    point = compute_metrics(y_true, y_pred, None, threshold=0.6)["macro_f1"]

    assert (lo, hi) == bootstrap_macro_f1_ci(y_true, y_pred)
    assert lo <= point <= hi
    assert bootstrap_macro_f1_ci(["a", "b"] * 5, ["a", "b"] * 5) == (1.0, 1.0)


def test_ambiguity_stats_known_values():
    groups = ["x", "x", "x", "y", "z", "z"]
    labels = ["a", "a", "b", "a", "c", "c"]

    s = ambiguity_stats(groups, labels)

    assert s["n_groups"] == 3
    assert s["ambiguous_groups"] == 1
    assert s["rows_in_ambiguous_groups"] == 3
    # Найкраще можливе для будь-якої функції опису: 2 з 3 у «x», усі інші — 5/6.
    assert s["ceiling_accuracy"] == pytest.approx(5 / 6)
