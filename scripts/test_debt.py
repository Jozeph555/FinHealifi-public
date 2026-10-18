"""
Ручний приклад запуску debt_optimizer на тестовому наборі боргів (python -m scripts.test_debt).
"""
from app.services.debt_optimizer import optimize_debt_repayment
import json

DEBTS = [
    {
        "name": "Monobank кредитний ліміт",
        "balance": 50000,
        "monthly_rate": 0.031,
        "min_payment": None,
        "debt_type": "revolving"
    },
    {
        "name": "Розстрочка shiny-diski",
        "balance": 18032,
        "monthly_rate": 0.0,
        "min_payment": 2254,
        "debt_type": "installment"
    },
    {
        "name": "Кредит інший банк",
        "balance": 75000,
        "monthly_rate": 0.02,
        "min_payment": 2000,
        "debt_type": "installment"
    }
]

# Місячний бюджет на борги для прикладу, грн.
MONTHLY_BUDGET = 15000

result = optimize_debt_repayment(DEBTS, MONTHLY_BUDGET, months=36)

for strategy, data in result.items():
    print(f"\n{'='*40}")
    print(f"Стратегія: {strategy.upper()}")
    print(f"Місяців до виплати: {data['months_to_payoff']}")
    print(f"Загальна переплата: {data['total_interest_paid']:,.0f} грн")
    print(f"Перший місяць платежів: {data['monthly_log'][0]['payments']}")
