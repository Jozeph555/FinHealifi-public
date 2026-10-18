"""
Правила мінімального платежу за боргом.

Спільні для оптимізаторів (debt_optimizer.py, milp_optimizer.py), прогнозу
(forecasting.py) і API боргів.

- revolving (кредитна картка, кредитний ліміт): мінімум залежить від поточного
  балансу, Debt.min_payment не заповнюється;
- installment (розстрочка, кредит): фіксований Debt.min_payment.
"""

REVOLVING_MIN_RATE = 0.04
REVOLVING_MIN_FLOOR = 200


def revolving_min_payment(balance: float) -> float:
    """Повертає мінімальний платіж revolving-боргу: 4 % балансу, але не менше 200 грн."""
    return max(REVOLVING_MIN_RATE * balance, REVOLVING_MIN_FLOOR)
