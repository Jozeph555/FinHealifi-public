"""
Тести переходу залишку бюджету в евристиках simulate_strategy.

Залишок бюджету, що лишився після закриття цільового боргу, у тому ж місяці
переходить на наступний борг у порядку стратегії (avalanche — за спаданням
ставки, snowball — за зростанням залишку). Сума платежів за місяць не
перевищує бюджет. На портфелі з scripts/test_debt.py avalanche з переходом
залишку збігається з MILP до 0,5 грн. Справжній солвер PuLP + CBC.
"""
import random

import pytest

from app.services.debt_optimizer import optimize_debt_repayment, simulate_strategy
from tests.test_milp_optimality import _feasible_budget, _synthetic_debts

CENT = 0.01  # грн
HONEST_BASELINE_EPSILON = 0.5  # грн


def _installment(name: str, balance: float, rate: float, min_payment: float) -> dict:
    return {"name": name, "balance": balance, "monthly_rate": rate,
            "min_payment": min_payment, "debt_type": "installment"}


def test_avalanche_spills_leftover_to_next_debt():
    """Дорогий борг закривається в місяці 1, решта бюджету йде на дешевший."""
    debts = [
        _installment("Дорогий", 1000, 0.05, 100),
        _installment("Дешевий", 10000, 0.01, 200),
    ]
    month1 = simulate_strategy(debts, 3000, 12, "avalanche")["monthly_log"][0]

    # Відсотки: 1000 * 0.05 = 50, 10000 * 0.01 = 100.
    assert month1["payments"]["Дорогий"] == pytest.approx(1050, abs=CENT)
    assert month1["balances"]["Дорогий"] == 0
    assert month1["payments"]["Дешевий"] == pytest.approx(3000 - 1050, abs=CENT)
    assert sum(month1["payments"].values()) == pytest.approx(3000, abs=CENT)


def test_snowball_spills_leftover_in_ascending_balance_order():
    """Найменший борг закривається, решта йде на наступний за залишком, не на найбільший."""
    debts = [
        _installment("Великий", 20000, 0.05, 300),
        _installment("Малий", 1000, 0.0, 100),
        _installment("Середній", 5000, 0.0, 200),
    ]
    month1 = simulate_strategy(debts, 3000, 12, "snowball")["monthly_log"][0]

    assert month1["payments"]["Малий"] == pytest.approx(1000, abs=CENT)
    assert month1["payments"]["Великий"] == pytest.approx(300, abs=CENT)
    assert month1["payments"]["Середній"] == pytest.approx(3000 - 1000 - 300, abs=CENT)
    assert sum(month1["payments"].values()) == pytest.approx(3000, abs=CENT)


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("strategy", ["avalanche", "snowball", "minimum"])
def test_monthly_payments_within_budget(seed, strategy):
    rng = random.Random(seed)
    debts = _synthetic_debts(rng, n_debts=rng.randint(1, 4))
    budget = _feasible_budget(rng, debts)
    result = simulate_strategy(debts, budget, 36, strategy)
    # Кожен платіж у monthly_log округлено до копійок окремо.
    tolerance = CENT * len(debts)
    for month in result["monthly_log"]:
        assert sum(month["payments"].values()) <= budget + tolerance, (
            f"{strategy}, місяць {month['month']}: платежі {month['payments']} > бюджет {budget}"
        )


def test_honest_baseline_avalanche_matches_milp():
    """Портфель з scripts/test_debt.py: MILP і avalanche з переходом залишку майже однакові."""
    debts = [
        {"name": "Monobank кредитний ліміт", "balance": 50000, "monthly_rate": 0.031,
         "min_payment": None, "debt_type": "revolving"},
        {"name": "Розстрочка shiny-diski", "balance": 18032, "monthly_rate": 0.0,
         "min_payment": 2254, "debt_type": "installment"},
        {"name": "Кредит інший банк", "balance": 75000, "monthly_rate": 0.02,
         "min_payment": 2000, "debt_type": "installment"},
    ]
    result = optimize_debt_repayment(debts, 15000, months=36)
    assert result["milp"]["status"] == "Optimal"
    diff = result["avalanche"]["total_interest_paid"] - result["milp"]["total_interest_paid"]
    assert abs(diff) <= HONEST_BASELINE_EPSILON, f"avalanche − MILP = {diff} грн"
