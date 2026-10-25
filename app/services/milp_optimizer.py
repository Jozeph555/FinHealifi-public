"""
MILP-модель оптимального погашення боргів (PuLP + CBC).

Математична модель:
  Змінні рішення:
    balance[i,t]  — залишок боргу i після місяця t (безперервна, >= 0)
    payment[i,t]  — платіж по боргу i в місяці t (безперервна, >= 0)
    interest[i,t] — нараховані відсотки по боргу i в місяці t (безперервна, >= 0)
    active[i,t]   — чи борг i ще не погашений у місяці t (БІНАРНА)

  Цільова функція:
    minimize  Σ interest[i,t]  (сумарна переплата за весь горизонт)

  Обмеження:
    - Динаміка балансу: balance[i,t] = balance[i,t-1] + interest[i,t] - payment[i,t]
    - Нарахування: interest[i,t] = rate[i] * balance[i,t-1]  (лінійно)
    - Не можна переплатити понад залишок боргу цього місяця
    - Бюджет: Σ_i payment[i,t] <= monthly_budget, для кожного t
    - Мінімальний платіж (debt_rules.min_payment_lower_bounds від
      balance[i,t-1]) діє лише поки борг активний (big-M лінеаризація через
      бінарну active[i,t]): payment >= межа - M_межі * (1 - active), де M_межі —
      значення межі на верхній оцінці балансу min(M, balance0 * (1 + rate)^(t-1));
      борг вважається неактивним лише з нульовим балансом:
      balance[i,t] <= M * active[i,t]

Бінарні змінні active[i,t] роблять задачу змішаною цілочисельною (MILP):
умова «мінімальний платіж, поки борг не погашений» не виражається в чистому LP.
"""
from pulp import LpProblem, LpMinimize, LpVariable, LpBinary, lpSum, PULP_CBC_CMD, LpStatus

from app.services.debt_rules import min_payment_lower_bounds


def optimize_milp(debts: list[dict], monthly_budget: float, months: int = 36) -> dict:
    """Складає план погашення боргів з мінімальною сумарною переплатою відсотками.

    Args:
        debts: Борги з ключами name, balance, monthly_rate, debt_type, min_payment.
        monthly_budget: Загальна сума на платежі щомісяця, грн.
        months: Горизонт планування в місяцях.

    Returns:
        Словник зі статусом солвера (status), months_to_payoff (None, якщо борги
        не погашено за горизонт), total_interest_paid, monthly_log (до місяця
        погашення або на весь горизонт) і final_balances.
    """
    names = [d["name"] for d in debts]
    balance0 = {d["name"]: d["balance"] for d in debts}
    rate = {d["name"]: d["monthly_rate"] for d in debts}
    debt_type = {d["name"]: d["debt_type"] for d in debts}
    min_payment = {d["name"]: d.get("min_payment") for d in debts}

    total_initial = sum(balance0.values())
    M = max(total_initial * 3, 10_000)  # big-M: більше за будь-який можливий баланс чи платіж

    prob = LpProblem("debt_payoff_milp", LpMinimize)

    balance = {(n, t): LpVariable(f"balance_{n}_{t}", lowBound=0)
               for n in names for t in range(0, months + 1)}
    payment = {(n, t): LpVariable(f"payment_{n}_{t}", lowBound=0)
               for n in names for t in range(1, months + 1)}
    interest = {(n, t): LpVariable(f"interest_{n}_{t}", lowBound=0)
                for n in names for t in range(1, months + 1)}
    active = {(n, t): LpVariable(f"active_{n}_{t}", cat=LpBinary)
              for n in names for t in range(1, months + 1)}

    for n in names:
        prob += balance[(n, 0)] == balance0[n]

    for n in names:
        for t in range(1, months + 1):
            prob += interest[(n, t)] == rate[n] * balance[(n, t - 1)]
            prob += balance[(n, t)] == balance[(n, t - 1)] + interest[(n, t)] - payment[(n, t)]
            prob += payment[(n, t)] <= balance[(n, t - 1)] + interest[(n, t)]
            prob += balance[(n, t)] <= M * active[(n, t)]

            # Мінімум — найбільша з нижніх меж, діє лише поки борг активний.
            # Обмеження «не більше боргу з відсотками» дає верхня межа платежу:
            # якщо мінімум її перевищує, борг не може лишитися активним і
            # гаситься повністю. Для кожної межі big-M — її значення на верхній
            # оцінці балансу (межі не спадають з балансом): так обмеження
            # вимикається при active = 0 і не слабшає більше, ніж потрібно.
            balance_ub = min(M, balance0[n] * (1 + rate[n]) ** (t - 1))
            bounds = min_payment_lower_bounds(debt_type[n], balance[(n, t - 1)], min_payment[n])
            bounds_m = min_payment_lower_bounds(debt_type[n], balance_ub, min_payment[n])
            for bound, bound_m in zip(bounds, bounds_m):
                prob += payment[(n, t)] >= bound - bound_m * (1 - active[(n, t)])

    for t in range(1, months + 1):
        prob += lpSum(payment[(n, t)] for n in names) <= monthly_budget

    prob += lpSum(interest[(n, t)] for n in names for t in range(1, months + 1))

    solver = PULP_CBC_CMD(msg=0)
    prob.solve(solver)
    status = LpStatus[prob.status]

    total_interest_paid = sum(interest[(n, t)].value() or 0 for n in names for t in range(1, months + 1))

    monthly_log = []
    months_to_payoff = None
    for t in range(1, months + 1):
        bals = {n: round(balance[(n, t)].value() or 0, 2) for n in names}
        pays = {n: round(payment[(n, t)].value() or 0, 2) for n in names}
        ints = {n: round(interest[(n, t)].value() or 0, 2) for n in names}
        total_balance = round(sum(bals.values()), 2)
        monthly_log.append({
            "month": t,
            "balances": bals,
            "payments": pays,
            "interest": ints,
            "total_balance": total_balance,
        })
        if months_to_payoff is None and total_balance <= 0.01:
            months_to_payoff = t

    return {
        "strategy": "milp",
        "status": status,
        "months_to_payoff": months_to_payoff,
        "total_interest_paid": round(total_interest_paid, 2),
        # Розклад до місяця погашення; якщо борги не погашено за горизонт —
        # на весь горизонт.
        "monthly_log": monthly_log[:months_to_payoff] if months_to_payoff else monthly_log,
        "final_balances": {n: round(balance[(n, months)].value() or 0, 2) for n in names},
    }
