"""
Тести розбиття й метрик бектесту Berka (scripts/analysis/berka_backtest.py).

На синтетичній панелі, без БД і без вивантаженого датасету:
(а) жоден рахунок не належить двом групам;
(б) жодне train-вікно і жоден train-рядок не містять місяців >= T1;
(в) масштабування залежить лише від вхідного вікна;
(г) мутація: випадкове розбиття по рядках замість розбиття за рахунками ламає (а) і (б).
Також: пропорції розбиття і збереження груп основного набору, сезонна наївна,
bootstrap-інтервал і MASE; критерій передреєстрації, заборона повторного запуску test,
фіксація хешів і прогноз нейромереж зі збережених ваг.
"""
import hashlib

import numpy as np
import pandas as pd
import pytest

import scripts.analysis.berka_backtest as backtest
from scripts.analysis.berka_backtest import (
    GROUPS,
    T1,
    Samples,
    bootstrap_mae_ratio,
    build_samples,
    check_no_account_overlap,
    check_train_before_t1,
    claim_output_dir,
    criterion_table,
    freeze_record,
    label_rows,
    point_metrics,
    scale_inputs,
    simple_predictions,
    split_accounts,
)

LAST = pd.Period("1998-11", freq="M")


def _panel(n_accounts=60, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for acc in range(1, n_accounts + 1):
        start = pd.Period("1993-02", freq="M") + int(rng.integers(0, 30))
        for m in pd.period_range(start, LAST, freq="M"):
            value = 0.0 if rng.random() < 0.05 else float(rng.integers(1000, 20000))
            rows.append((acc * 7, m, value))
    return pd.DataFrame(rows, columns=["account_id", "month", "expense"])


def _account_split(panel):
    labels = label_rows(panel, split_accounts(panel["account_id"].unique()))
    return labels, build_samples(panel, labels)


def _random_row_split(panel):
    rng = np.random.default_rng(42)
    labels = rng.choice(np.array(GROUPS), size=len(panel), p=[0.6, 0.2, 0.2])
    return labels, build_samples(panel, labels)


def _assert_no_account_in_two_groups(panel, labels, samples):
    accounts = {
        g: set(panel["account_id"][labels == g]) | set(samples[g].meta["account_id"]) for g in GROUPS
    }
    assert not accounts["train"] & accounts["val"]
    assert not accounts["train"] & accounts["test"]
    assert not accounts["val"] & accounts["test"]


def _assert_train_before_t1(panel, labels, samples):
    assert (panel["month"][labels == "train"] < T1).all()
    meta = samples["train"].meta
    assert (meta["first_month"] < T1).all()
    assert (meta["last_month"] < T1).all()


def test_no_account_in_two_groups():
    panel = _panel()
    labels, samples = _account_split(panel)

    assert all(len(samples[g].y) for g in GROUPS)
    _assert_no_account_in_two_groups(panel, labels, samples)
    check_no_account_overlap(panel, labels, samples)


def test_train_windows_end_before_t1():
    panel = _panel()
    labels, samples = _account_split(panel)

    assert len(samples["train"].y) > 0
    _assert_train_before_t1(panel, labels, samples)
    check_train_before_t1(panel, labels, samples)


def test_scaling_does_not_use_future_values():
    panel = _panel()
    labels, before = _account_split(panel)

    for g in GROUPS:
        # усі місяці після найранішої точки відліку групи змінюються; її вікна — ні
        cutoff = before[g].meta["origin"].min()
        changed = panel.copy()
        future = changed["month"] > cutoff
        changed.loc[future, "expense"] = changed.loc[future, "expense"] * 10 + 12345
        after = build_samples(changed, labels)[g]
        keep = (before[g].meta["origin"] == cutoff).to_numpy()

        assert keep.any()
        xs_before, f_before = scale_inputs(before[g].X[keep])
        xs_after, f_after = scale_inputs(after.X[keep])
        np.testing.assert_array_equal(xs_before, xs_after)
        np.testing.assert_array_equal(f_before, f_after)
        assert (before[g].y[keep] != after.y[keep]).all()


def test_scaling_zero_window_divides_by_one():
    X = np.array([[0.0] * 12, [2.0] * 12])
    xs, factor = scale_inputs(X)

    np.testing.assert_array_equal(factor, [1.0, 2.0])
    np.testing.assert_array_equal(xs[0], np.zeros(12))
    np.testing.assert_array_equal(xs[1], np.ones(12))


def test_mutation_random_row_split_breaks_checks():
    panel = _panel()
    labels, samples = _random_row_split(panel)

    with pytest.raises(AssertionError):
        _assert_no_account_in_two_groups(panel, labels, samples)
    with pytest.raises(AssertionError):
        _assert_train_before_t1(panel, labels, samples)
    with pytest.raises(ValueError):
        check_no_account_overlap(panel, labels, samples)
    with pytest.raises(ValueError):
        check_train_before_t1(panel, labels, samples)


def test_split_proportions_and_base_groups_kept():
    ids = list(range(100, 200))
    groups = split_accounts(ids)
    counts = pd.Series(groups).value_counts()

    assert (counts["train"], counts["val"], counts["test"]) == (60, 20, 20)
    assert split_accounts(ids) == groups

    extended = split_accounts(ids + list(range(500, 550)), base=groups)
    assert all(extended[a] == groups[a] for a in ids)
    extra = pd.Series({a: extended[a] for a in range(500, 550)}).value_counts()
    assert (extra["train"], extra["val"], extra["test"]) == (30, 10, 10)


def test_seasonal_naive_takes_month_twelve_before_target():
    values = np.arange(1.0, 15.0)
    X = np.vstack([values[:12], values[:12]])
    meta = pd.DataFrame({"account_id": [1, 1], "h": [1, 2], "row_start": [0, 0], "row_origin": [11, 11]})
    preds = simple_predictions(Samples(meta, X, np.zeros(2)), values)

    # ціль t+1 = місяць 13 → сезонна наївна бере місяць 1; ціль t+2 → місяць 2
    np.testing.assert_array_equal(preds["seasonal_naive"], [1.0, 2.0])
    np.testing.assert_array_equal(preds["naive"], [12.0, 12.0])
    assert preds["window_mean"][0] == pytest.approx(6.5)


def test_bootstrap_ratio_of_identical_models_is_one():
    accounts = np.repeat(np.arange(30), 4)
    ae = np.random.default_rng(0).random(len(accounts))

    assert bootstrap_mae_ratio(accounts, ae, ae) == (pytest.approx(1.0), pytest.approx(1.0))
    lo, hi = bootstrap_mae_ratio(accounts, ae * 0.5, ae)
    assert lo == pytest.approx(0.5) and hi == pytest.approx(0.5)


def test_criterion_requires_all_conditions_and_all_seeds():
    table = pd.DataFrame([
        {"model": "best", "mae_ratio": 1.0, "mase_ratio": 1.0, "bias": -100.0, "mae_ratio_ci_hi": 1.0},
        {"model": "good[s0]", "mae_ratio": 0.85, "mase_ratio": 0.8, "bias": 50.0, "mae_ratio_ci_hi": 0.95},
        {"model": "good[s1]", "mae_ratio": 0.9, "mase_ratio": 0.9, "bias": -100.0, "mae_ratio_ci_hi": 0.99},
        {"model": "mixed[s0]", "mae_ratio": 0.85, "mase_ratio": 0.8, "bias": 50.0, "mae_ratio_ci_hi": 0.95},
        {"model": "mixed[s1]", "mae_ratio": 0.85, "mase_ratio": 0.8, "bias": -150.0, "mae_ratio_ci_hi": 0.95},
        {"model": "single", "mae_ratio": 0.95, "mase_ratio": 0.8, "bias": 10.0, "mae_ratio_ci_hi": 1.0},
    ])
    v = criterion_table(table, "best", ("good", "mixed", "single")).set_index("model")

    assert v.loc["good", "passed"] and v.loc["good", "n_seeds"] == 2
    assert not v.loc["mixed", "passed"]
    assert v.loc["mixed", "failed"] == "abs_bias_not_worse"
    assert v.loc["mixed", "abs_bias_not_worse"] == 0.5
    assert not v.loc["single", "passed"]
    assert v.loc["single", "failed"] == "mae_ratio_le_0.9;ci_hi_lt_1"


def test_claim_output_dir_refuses_second_run(tmp_path):
    run_dir = tmp_path / "test_VYDAJ_min36"
    claim_output_dir(run_dir)

    assert run_dir.is_dir()
    with pytest.raises(FileExistsError):
        claim_output_dir(run_dir)


def test_freeze_record_hashes_config_and_models(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"a": 1}')
    models = tmp_path / "models"
    models.mkdir()
    (models / "m.pt").write_bytes(b"weights")
    record = freeze_record(config, models)

    assert record["config_sha256"] == hashlib.sha256(b'{"a": 1}').hexdigest()
    assert record["models_sha256"] == {"m.pt": hashlib.sha256(b"weights").hexdigest()}
    assert len(record["git"]["rev"]) == 40
    assert set(record["code_sha256"]) == {"scripts/analysis/berka_backtest.py", "scripts/analysis/berka_audit.py"}


@pytest.mark.parametrize("kind,params", [("lstm", {"hidden": 8, "lr": 1e-2}), ("dlinear", {"kernel": 3, "lr": 1e-2}),
                                         ("transformer", {"dropout": 0.0, "lr": 1e-2})])
def test_predict_nn_with_saved_weights_matches_training_predictions(monkeypatch, kind, params):
    monkeypatch.setattr(backtest, "NN_MAX_EPOCHS", 3)
    panel = _panel(n_accounts=20)
    _, samples = _account_split(panel)
    result = backtest.train_nn(kind, params, 0, samples["train"], samples["val"])

    np.testing.assert_allclose(
        backtest.predict_nn(kind, params, result["state_dict"], samples["val"]), result["pred"], rtol=1e-6
    )


def test_point_metrics():
    actual = np.array([10.0, 0.0, 20.0])
    pred = np.array([12.0, 1.0, 17.0])
    m = point_metrics(actual, pred, scale=np.array([2.0, 1.0, 3.0]))

    assert m["mae"] == pytest.approx(2.0)
    assert m["mase"] == pytest.approx((1.0 + 1.0 + 1.0) / 3)
    assert m["wape"] == pytest.approx(6.0 / 30.0)
    assert m["bias"] == pytest.approx(0.0)
