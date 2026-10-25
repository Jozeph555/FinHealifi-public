"""
Порівняння стратегій погашення боргів.

Чотири стратегії:
- avalanche, snowball, minimum — евристики, симуляція місяць за місяцем;
- milp — оптимальний план з MILP-моделі (app/services/milp_optimizer.py).

MILP-модель — спрощена версія RPML (Rios-Solis та ін., 2017) без штрафів за
недоплату, зі сталим бюджетом.
"""
from app.services.milp_optimizer import optimize_milp
from app.services.debt_rules import debt_min_payment


def optimize_debt_repayment(debts: list[dict], monthly_budget: float, months: int = 24) -> dict:
    """Рахує план погашення за кожною з чотирьох стратегій.

    Args:
        debts: Борги з ключами name, balance, monthly_rate, debt_type, min_payment.
        monthly_budget: Загальна сума на платежі щомісяця, грн.
        months: Горизонт планування в місяцях.

    Returns:
        Словник {назва стратегії: результат} для avalanche, snowball, minimum і milp.
    """
    strategies = {}

    for strategy in ["avalanche", "snowball", "minimum"]:
        strategies[strategy] = simulate_strategy(debts, monthly_budget, months, strategy)

    strategies["milp"] = optimize_milp(debts, monthly_budget, months)

    return strategies


def simulate_strategy(debts: list[dict], monthly_budget: float,
                       months: int, strategy: str) -> dict:
    """Симулює евристичну стратегію погашення місяць за місяцем.

    Щомісяця нараховує відсотки, сплачує мінімальні платежі
    (debt_rules.debt_min_payment від балансу на початок місяця), а залишок бюджету
    спрямовує на борги в порядку стратегії: за спаданням ставки (avalanche) або
    за зростанням залишку (snowball). Якщо цільовий борг закривається, решта
    бюджету в тому ж місяці переходить на наступний борг у цьому порядку. Для
    minimum залишок бюджету не використовується.

    Args:
        debts: Борги з ключами name, balance, monthly_rate, debt_type, min_payment.
        monthly_budget: Загальна сума на платежі щомісяця, грн.
        months: Горизонт симуляції в місяцях.
        strategy: "avalanche", "snowball" або "minimum".

    Returns:
        Словник зі стратегією, months_to_payoff (None, якщо борги не погашено за
        горизонт), total_interest_paid, monthly_log і final_balances.
    """
    balances = {d["name"]: d["balance"] for d in debts}
    rates = {d["name"]: d["monthly_rate"] for d in debts}
    total_interest_paid = 0
    months_to_payoff = months
    monthly_log = []

    for month in range(1, months + 1):
        if all(b <= 0 for b in balances.values()):
            months_to_payoff = month - 1
            break

        balances_start = dict(balances)
        interest_this_month = {}
        for name, balance in balances.items():
            if balance > 0:
                interest = balance * rates[name]
                balances[name] += interest
                interest_this_month[name] = round(interest, 2)
                total_interest_paid += interest

        remaining_budget = monthly_budget
        payments = {}

        for d in debts:
            name = d["name"]
            if balances[name] <= 0:
                payments[name] = 0
                continue
            min_pay = debt_min_payment(d["debt_type"], balances_start[name], d["monthly_rate"],
                                       d.get("min_payment"))
            # balance·(1 + r) і balance + balance·r можуть різнитися в останньому
            # знаку float; платіж не перевищує фактичного залишку.
            min_pay = min(min_pay, balances[name])
            payments[name] = min_pay
            remaining_budget -= min_pay

        if remaining_budget > 0 and strategy in ("avalanche", "snowball"):
            active_debts = [d for d in debts if balances[d["name"]] > 0]
            if strategy == "avalanche":
                order = sorted(active_debts, key=lambda d: d["monthly_rate"], reverse=True)
            else:
                order = sorted(active_debts, key=lambda d: balances[d["name"]])
            # Залишок після закриття боргу переходить на наступний у порядку стратегії.
            for target in order:
                if remaining_budget <= 0:
                    break
                extra = min(remaining_budget, balances[target["name"]] - payments[target["name"]])
                payments[target["name"]] += extra
                remaining_budget -= extra

        for name in balances:
            balances[name] = max(0, balances[name] - payments[name])

        monthly_log.append({
            "month": month,
            "balances": {k: round(v, 2) for k, v in balances.items()},
            "payments": {k: round(v, 2) for k, v in payments.items()},
            "interest": {k: round(v, 2) for k, v in interest_this_month.items()},
            "total_balance": round(sum(balances.values()), 2)
        })

    if not all(b <= 0 for b in balances.values()):
        months_to_payoff = None

    return {
        "strategy": strategy,
        "months_to_payoff": months_to_payoff,
        "total_interest_paid": round(total_interest_paid, 2),
        "monthly_log": monthly_log,
        "final_balances": {k: round(v, 2) for k, v in balances.items()}
    }
