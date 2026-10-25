"""
Тести єдиного правила мінімального платежу (debt_rules) у MILP і евристиках.

(а) На 200 синтетичних портфелях (seed 42, генератор з test_milp_optimality,
    2–4 борги) мінімум, який застосовує евристика (стратегія minimum за один
    місяць), збігається з мінімумом, який вимагає MILP: одномісячна MILP-задача
    з бюджетом, рівним сумі мінімумів евристики, розв'язна, платить ті самі
    суми по кожному боргу, а з бюджетом на копійку меншим — нерозв'язна.
    Стан береться на місяць 1 і на останній місяць розкладу avalanche, де
    баланси малі й діє обмеження «не більше боргу з відсотками».
(б) Евристика не перевищує бюджет у місяці, де бюджет не менший за суму
    мінімумів.
(в) Картка 20 000 грн під 4 % з бюджетом 800 грн: перший платіж MILP і всіх
    евристик однаковий — 800 грн (4 % від балансу до нарахування відсотків).
Справжній солвер PuLP + CBC.
"""
import random

import pytest

from app.services.debt_optimizer import optimize_debt_repayment, simulate_strategy
from app.services.debt_rules import debt_min_payment
from app.services.milp_optimizer import optimize_milp
from tests.test_milp_optimality import _feasible_budget, _synthetic_debts

CENT = 0.01  # грн
LOG_ROUNDING = 0.005  # грн: monthly_log округлює кожну суму до копійок
N_PORTFOLIOS = 200
MONTHS = 36


def _portfolios():
    rng = random.Random(42)
    out = []
    for _ in range(N_PORTFOLIOS):
        debts = _synthetic_debts(rng, n_debts=rng.randint(2, 4))
        out.append((debts, _feasible_budget(rng, debts)))
    return out


PORTFOLIOS = _portfolios()


def _start_balances(debts: list[dict], monthly_log: list[dict]) -> list[dict]:
    """Баланси на початок кожного місяця розкладу."""
    starts = [{d["name"]: d["balance"] for d in debts}]
    starts += [m["balances"] for m in monthly_log[:-1]]
    return starts


def _state(debts: list[dict], balances: dict) -> list[dict]:
    return [dict(d, balance=balances[d["name"]]) for d in debts if balances[d["name"]] > 0]


def _assert_same_minimum(state: list[dict]):
    heuristic = simulate_strategy(state, 0, 1, "minimum")["monthly_log"][0]["payments"]
    total = sum(heuristic.values())
    slack = LOG_ROUNDING * len(state)

    feasible = optimize_milp(state, total + slack, 1)
    assert feasible["status"] == "Optimal", f"MILP не прийняв мінімуми евристики {heuristic}; {state}"
    milp_payments = feasible["monthly_log"][0]["payments"]
    for name, amount in heuristic.items():
        assert abs(milp_payments[name] - amount) <= CENT + 2 * slack, (
            f"{name}: MILP {milp_payments[name]} проти евристики {amount}; {state}"
        )

    below = optimize_milp(state, total - CENT - slack, 1)
    assert below["status"] != "Optimal", (
        f"MILP дозволив заплатити менше за мінімуми евристики {heuristic}; {state}"
    )


@pytest.mark.parametrize("idx", range(N_PORTFOLIOS))
def test_milp_and_heuristic_apply_same_minimum(idx):
    debts, budget = PORTFOLIOS[idx]
    _assert_same_minimum(_state(debts, {d["name"]: d["balance"] for d in debts}))

    avalanche_log = simulate_strategy(debts, budget, MONTHS, "avalanche")["monthly_log"]
    last_state = _state(debts, _start_balances(debts, avalanche_log)[-1])
    if last_state:
        _assert_same_minimum(last_state)


@pytest.mark.parametrize("strategy", ["avalanche", "snowball", "minimum"])
def test_heuristic_within_budget_when_budget_covers_minimums(strategy):
    for debts, budget in PORTFOLIOS:
        log = simulate_strategy(debts, budget, MONTHS, strategy)["monthly_log"]
        rate = {d["name"]: d for d in debts}
        for start, month in zip(_start_balances(debts, log), log):
            required = sum(
                debt_min_payment(rate[n]["debt_type"], b, rate[n]["monthly_rate"], rate[n]["min_payment"])
                for n, b in start.items()
            )
            if budget < required:
                continue
            assert sum(month["payments"].values()) <= budget + LOG_ROUNDING * len(debts), (
                f"{strategy}, місяць {month['month']}: {month['payments']} > {budget}"
            )


def test_card_first_payment_identical_for_all_strategies():
    debts = [{"name": "Картка", "balance": 20000.0, "monthly_rate": 0.04,
              "min_payment": None, "debt_type": "revolving"}]
    result = optimize_debt_repayment(debts, 800, months=MONTHS)

    assert result["milp"]["status"] == "Optimal"
    for strategy in ("milp", "avalanche", "snowball", "minimum"):
        assert result[strategy]["monthly_log"][0]["payments"]["Картка"] == pytest.approx(800, abs=CENT), strategy
