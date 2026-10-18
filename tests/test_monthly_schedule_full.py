"""
Тести довжини помісячного розкладу (monthly_log) стратегій погашення.

simulate_strategy зупиняє цикл, коли всі баланси досягають 0; optimize_milp
рахує весь горизонт і обрізає розклад до months_to_payoff. Справжній солвер
PuLP + CBC.
"""
from app.services.debt_optimizer import optimize_debt_repayment

EPSILON = 0.5  # грн, як у test_milp_optimality.py


def test_heuristic_monthly_log_bounded_by_payoff():
    """Евристики: довжина monthly_log дорівнює months_to_payoff, останній баланс нульовий."""
    debts = [{"name": "Кредит", "balance": 3000, "monthly_rate": 0.02,
              "min_payment": 500, "debt_type": "installment"}]
    result = optimize_debt_repayment(debts, monthly_budget=1000, months=24)

    for strategy in ("avalanche", "snowball", "minimum"):
        s = result[strategy]
        assert s["months_to_payoff"] is not None, (
            f"{strategy}: борг мав повністю погаситись у межах 24 місяців "
            f"при бюджеті 1000грн/міс на баланс 3000грн"
        )
        assert len(s["monthly_log"]) == s["months_to_payoff"], (
            f"{strategy}: довжина monthly_log ({len(s['monthly_log'])}) не "
            f"збігається з months_to_payoff ({s['months_to_payoff']})"
        )
        assert s["monthly_log"][-1]["total_balance"] <= EPSILON


def test_milp_monthly_log_bounded_by_payoff_not_full_horizon():
    """MILP: monthly_log обрізано до months_to_payoff, без нульових місяців до горизонту."""
    debts = [{"name": "Кредит", "balance": 3000, "monthly_rate": 0.02,
              "min_payment": 500, "debt_type": "installment"}]
    result = optimize_debt_repayment(debts, monthly_budget=1000, months=24)
    milp = result["milp"]

    assert milp["status"] == "Optimal"
    assert milp["months_to_payoff"] is not None
    assert len(milp["monthly_log"]) == milp["months_to_payoff"], (
        f"monthly_log ({len(milp['monthly_log'])} записів) мав би бути "
        f"обрізаний до months_to_payoff ({milp['months_to_payoff']}), "
        f"не залишати хвіст аж до горизонту 24"
    )
    assert milp["monthly_log"][-1]["total_balance"] <= EPSILON
    assert milp["months_to_payoff"] < 24  # інакше перевірка нічого не доводить


def test_milp_monthly_log_keeps_full_horizon_when_never_paid_off():
    """MILP: якщо борг не погашено за горизонт (months_to_payoff is None), monthly_log охоплює весь горизонт."""
    debts = [{"name": "Борг, що росте", "balance": 100000, "monthly_rate": 0.03,
              "min_payment": 500, "debt_type": "installment"}]
    result = optimize_debt_repayment(debts, monthly_budget=500, months=24)
    milp = result["milp"]

    assert milp["status"] == "Optimal"
    assert milp["months_to_payoff"] is None, (
        "Тест очікує сценарій, де відсотки (3%/міс на 100000) переважають "
        "фіксований платіж (500грн) — борг не мав погаситись за 24 місяці"
    )
    assert len(milp["monthly_log"]) == 24
