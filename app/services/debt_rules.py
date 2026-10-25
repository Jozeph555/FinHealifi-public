"""
Правило мінімального платежу за боргом.

Одне правило для MILP (milp_optimizer.py), евристик (debt_optimizer.py),
бюджету плану (API боргів, радник) і розбивки витрат (forecasting.py):

- revolving (кредитна картка, кредитний ліміт): max(4 % балансу на початок
  місяця до нарахування відсотків, 200 грн); Debt.min_payment не заповнюється;
- installment (розстрочка, кредит): фіксований Debt.min_payment;
- в обох випадках не більше суми боргу з відсотками цього місяця, поки борг
  активний: якщо мінімум більший, борг гаситься повністю.

min_payment_lower_bounds задає нижні межі мінімуму й працює як з числами, так
і з лінійними виразами PuLP (у MILP — коефіцієнти обмежень). debt_min_payment
рахує з них число для конкретного балансу.
"""

REVOLVING_MIN_RATE = 0.04
REVOLVING_MIN_FLOOR = 200


def min_payment_lower_bounds(debt_type: str, balance_start, min_payment: float | None) -> list:
    """Повертає нижні межі мінімального платежу; мінімум — найбільша з них.

    Args:
        debt_type: "revolving" або "installment".
        balance_start: Баланс на початок місяця до нарахування відсотків — число
            або лінійний вираз PuLP.
        min_payment: Фіксований мінімальний платіж installment-боргу.

    Returns:
        Для revolving — [4 % від balance_start, 200]; для installment —
        [min_payment або 0].
    """
    if debt_type == "revolving":
        return [REVOLVING_MIN_RATE * balance_start, REVOLVING_MIN_FLOOR]
    return [min_payment or 0]


def debt_min_payment(debt_type: str, balance_start: float, monthly_rate: float,
                     min_payment: float | None) -> float:
    """Повертає обов'язковий мінімальний платіж боргу за місяць.

    Найбільша з min_payment_lower_bounds, але не більше balance_start · (1 +
    monthly_rate) — суми боргу з відсотками цього місяця. Для погашеного боргу 0.

    Args:
        debt_type: "revolving" або "installment".
        balance_start: Баланс на початок місяця до нарахування відсотків, грн.
        monthly_rate: Місячна ставка (0.03 = 3 %).
        min_payment: Фіксований мінімальний платіж installment-боргу.
    """
    if balance_start <= 0:
        return 0.0
    required = max(min_payment_lower_bounds(debt_type, balance_start, min_payment))
    return min(required, balance_start * (1 + monthly_rate))
