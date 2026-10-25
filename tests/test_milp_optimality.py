"""
Тести оптимальності MILP відносно евристик avalanche, snowball і minimum.

milp_optimizer мінімізує суму відсотків за тих самих обмежень (бюджет на
місяць, динаміка балансу, мінімальний платіж поки борг активний), яким
підпорядковані евристики, тож кожен розклад евристики — допустима точка
MILP-задачі. Отже, для будь-якої розв'язної задачі (status == "Optimal"):

    milp.total_interest_paid <= avalanche.total_interest_paid
    milp.total_interest_paid <= snowball.total_interest_paid
    milp.total_interest_paid <= minimum.total_interest_paid

Справжній солвер PuLP + CBC на синтетичних портфелях боргів. EPSILON покриває
округлення до копійок (round(x, 2)).
"""
import random

import pytest

from app.services.debt_optimizer import optimize_debt_repayment

EPSILON = 0.01  # грн
# Допуск рівності MILP і avalanche для одного боргу.
MATCH_EPSILON = 0.5  # грн


def assert_milp_not_worse(debts: list[dict], monthly_budget: float, months: int = 24) -> dict:
    result = optimize_debt_repayment(debts, monthly_budget, months=months)
    milp = result["milp"]

    assert milp["status"] == "Optimal", (
        f"MILP не знайшов оптимальний розв'язок (status={milp['status']}) — "
        f"тест не може перевірити інваріант оптимальності на цьому наборі. "
        f"debts={debts}, budget={monthly_budget}, months={months}"
    )

    for strategy_name in ("avalanche", "snowball", "minimum"):
        heuristic = result[strategy_name]
        assert milp["total_interest_paid"] <= heuristic["total_interest_paid"] + EPSILON, (
            f"MILP ({milp['total_interest_paid']} грн переплати) ГІРШИЙ за "
            f"{strategy_name} ({heuristic['total_interest_paid']} грн) — це порушує "
            f"саме означення MILP-оптимальності (кожен розклад {strategy_name} "
            f"допустимий і для MILP, тож MILP не може програвати). "
            f"Це баг у формулюванні задачі, не edge case. "
            f"debts={debts}, budget={monthly_budget}, months={months}"
        )

    return result


# --- Задані вручну сценарії ---------------------------------------------

def test_milp_beats_heuristics_realistic_portfolio():
    """Кредитна картка й дві розстрочки з різними ставками."""
    debts = [
        {"name": "Кредитний ліміт", "balance": 50000, "monthly_rate": 0.031,
         "min_payment": None, "debt_type": "revolving"},
        {"name": "Розстрочка shiny-diski", "balance": 18032, "monthly_rate": 0.0,
         "min_payment": 2254, "debt_type": "installment"},
        {"name": "Кредит інший банк", "balance": 75000, "monthly_rate": 0.02,
         "min_payment": 2000, "debt_type": "installment"},
    ]
    assert_milp_not_worse(debts, monthly_budget=15000, months=36)


def test_milp_matches_heuristics_single_debt():
    """Для одного боргу всі три стратегії дають майже однакову переплату."""
    debts = [{"name": "Кредит", "balance": 20000, "monthly_rate": 0.025,
              "min_payment": 1500, "debt_type": "installment"}]
    result = assert_milp_not_worse(debts, monthly_budget=5000, months=24)
    assert abs(result["milp"]["total_interest_paid"]
               - result["avalanche"]["total_interest_paid"]) <= MATCH_EPSILON


def test_milp_beats_heuristics_tight_budget():
    """Бюджет ледь перевищує мінімальні платежі."""
    debts = [
        {"name": "Картка A", "balance": 30000, "monthly_rate": 0.04,
         "min_payment": None, "debt_type": "revolving"},
        {"name": "Картка B", "balance": 15000, "monthly_rate": 0.03,
         "min_payment": None, "debt_type": "revolving"},
    ]
    min_needed = sum(max(d["balance"] * 0.04, 200) for d in debts)
    assert_milp_not_worse(debts, monthly_budget=min_needed + 100, months=36)


def test_milp_beats_heuristics_equal_rates_many_debts():
    """Однакові ставки: avalanche і snowball обирають ціль по-різному, MILP не гірший за обидві."""
    debts = [
        {"name": f"Борг {i}", "balance": 5000 + i * 3000, "monthly_rate": 0.02,
         "min_payment": 300, "debt_type": "installment"}
        for i in range(5)
    ]
    assert_milp_not_worse(debts, monthly_budget=8000, months=24)


def test_milp_beats_heuristics_zero_rate_debt_present():
    """Безвідсоткова розстрочка серед відсоткових боргів."""
    debts = [
        {"name": "0% розстрочка", "balance": 10000, "monthly_rate": 0.0,
         "min_payment": 1000, "debt_type": "installment"},
        {"name": "Дорога картка", "balance": 25000, "monthly_rate": 0.05,
         "min_payment": None, "debt_type": "revolving"},
    ]
    assert_milp_not_worse(debts, monthly_budget=6000, months=24)


def test_milp_beats_heuristics_generous_budget():
    """Бюджет з великим запасом над мінімальними платежами."""
    debts = [
        {"name": "Дуже дорога картка", "balance": 40000, "monthly_rate": 0.06,
         "min_payment": None, "debt_type": "revolving"},
        {"name": "Дешевий кредит", "balance": 8000, "monthly_rate": 0.01,
         "min_payment": 500, "debt_type": "installment"},
    ]
    assert_milp_not_worse(debts, monthly_budget=20000, months=24)


# --- Property-based sweep: багато синтетичних портфелів, фіксований seed --

def _synthetic_debts(rng: random.Random, n_debts: int) -> list[dict]:
    debts = []
    for i in range(n_debts):
        debt_type = rng.choice(["revolving", "installment"])
        balance = round(rng.uniform(1000, 40000), 2)
        rate = round(rng.uniform(0.0, 0.05), 4)
        min_payment = round(rng.uniform(200, max(300, balance * 0.08)), 2) \
            if debt_type == "installment" else None
        debts.append({
            "name": f"synthetic_{i}",
            "balance": balance,
            "monthly_rate": rate,
            "min_payment": min_payment,
            "debt_type": debt_type,
        })
    return debts


def _feasible_budget(rng: random.Random, debts: list[dict]) -> float:
    # Бюджет із запасом над сумою мінімальних платежів на старті гарантує
    # розв'язність на всьому горизонті: мінімальні платежі з часом зменшуються
    # (revolving) або не змінюються (installment).
    baseline = sum(
        max(d["balance"] * 0.04, 200) if d["debt_type"] == "revolving" else d["min_payment"]
        for d in debts
    )
    return round(baseline * rng.uniform(1.2, 2.5), 2)


@pytest.mark.parametrize("seed", range(20))
def test_milp_beats_heuristics_random_synthetic_portfolios(seed):
    rng = random.Random(seed)
    debts = _synthetic_debts(rng, n_debts=rng.randint(1, 4))
    budget = _feasible_budget(rng, debts)
    assert_milp_not_worse(debts, budget, months=24)
