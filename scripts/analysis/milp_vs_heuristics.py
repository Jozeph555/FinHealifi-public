"""
Порівняння MILP з евристиками avalanche, snowball і minimum на синтетичних портфелях.

Генератор портфелів — той самий, що в tests/test_milp_optimality.py:
2–4 борги, бюджет 1,2–2,5 від суми стартових мінімальних платежів,
горизонт 36 місяців, seed 42, 200 портфелів. MILP розв'язується один раз на
портфель; кожна евристика рахується у трьох варіантах:
  repo   — simulate_strategy з app/services/debt_optimizer.py;
  legacy — залишок бюджету після закриття цільового боргу пропадає;
  spill  — залишок переходить на наступний борг у порядку стратегії.

Виграш MILP, у відсотках від значення евристики (нульовий знаменник дає 0):
  int  — від сумарних відсотків (переплати): (int_heur − int_milp) / int_heur;
         основна метрика;
  full — від повної вартості, тобто платежів за горизонт плюс кінцевих
         залишків боргів;
  pay  — від сумарних платежів; лише діагностика: коли кінцеві залишки
         різні, стратегія, що не погашає борги (minimum), платить менше і
         «виграє».
Для портфелів, де MILP не погасив борги за горизонт, друкується окрема
діагностика: відношення бюджету до стартових мінімальних платежів і чи
погасили борги евристики.

Запуск (з кореня репозиторію; БД не потрібна):
    python -m scripts.analysis.milp_vs_heuristics [--seed 42] [--n 200]
"""
import argparse
import random
import statistics
from collections import Counter

import numpy as np
from pulp import PULP_CBC_CMD

from app.services.debt_optimizer import simulate_strategy
from app.services.debt_rules import debt_min_payment
from app.services.milp_optimizer import optimize_milp

MONTHS = 36
STRATEGIES = ("avalanche", "snowball", "minimum")
VARIANTS = ("repo", "legacy", "spill")
WIN_THRESHOLD_PCT = 0.1


def synthetic_debts(rng: random.Random, n_debts: int) -> list[dict]:
    """Генерує портфель боргів так само, як tests/test_milp_optimality.py."""
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


def start_min_payments(debts: list[dict]) -> float:
    """Повертає суму мінімальних платежів на старті."""
    return sum(
        max(d["balance"] * 0.04, 200) if d["debt_type"] == "revolving" else d["min_payment"]
        for d in debts
    )


def feasible_budget(rng: random.Random, debts: list[dict]) -> float:
    """Повертає бюджет 1,2–2,5 від суми стартових мінімальних платежів."""
    return round(start_min_payments(debts) * rng.uniform(1.2, 2.5), 2)


def simulate_local(debts: list[dict], monthly_budget: float, months: int,
                   strategy: str, spillover: bool) -> dict:
    """Симулює евристику з переходом залишку бюджету (spillover) або без нього.

    Нарахування відсотків і мінімальні платежі (debt_rules.debt_min_payment) — як у
    simulate_strategy.
    Повертає total_interest_paid, total_paid і final_balance (сума залишків).
    """
    balances = {d["name"]: d["balance"] for d in debts}
    total_interest = 0.0
    total_paid = 0.0

    for _ in range(months):
        if all(b <= 0 for b in balances.values()):
            break
        balances_start = dict(balances)
        for d in debts:
            name = d["name"]
            if balances[name] > 0:
                interest = balances[name] * d["monthly_rate"]
                balances[name] += interest
                total_interest += interest

        remaining = monthly_budget
        payments = {}
        for d in debts:
            name = d["name"]
            if balances[name] <= 0:
                payments[name] = 0
                continue
            min_pay = min(debt_min_payment(d["debt_type"], balances_start[name], d["monthly_rate"],
                                           d["min_payment"]), balances[name])
            payments[name] = min_pay
            remaining -= min_pay

        if remaining > 0 and strategy != "minimum":
            active = [d for d in debts if balances[d["name"]] > 0]
            if strategy == "avalanche":
                order = sorted(active, key=lambda d: d["monthly_rate"], reverse=True)
            else:
                order = sorted(active, key=lambda d: balances[d["name"]])
            if not spillover:
                order = order[:1]
            for d in order:
                if remaining <= 0:
                    break
                name = d["name"]
                extra = min(remaining, balances[name] - payments[name])
                payments[name] += extra
                remaining -= extra

        for name in balances:
            balances[name] = max(0, balances[name] - payments[name])
        total_paid += sum(payments.values())

    return {
        "total_interest_paid": total_interest,
        "total_paid": total_paid,
        "final_balance": sum(balances.values()),
    }


def _total_paid(result: dict) -> float:
    """Повертає сумарні платежі за monthly_log (MILP або simulate_strategy)."""
    return sum(sum(m["payments"].values()) for m in result["monthly_log"])


def _win_pct(heur: float, milp: float) -> float:
    """Повертає виграш MILP у відсотках від значення евристики."""
    return (heur - milp) / heur * 100 if heur > 0 else 0.0


def _summary(values: list[float]) -> str:
    """Форматує рядок таблиці: mean, median, P95, max, частка > порогу."""
    share = sum(v > WIN_THRESHOLD_PCT for v in values) / len(values) * 100
    return (f"{statistics.mean(values):9.4f} {statistics.median(values):9.4f} "
            f"{np.percentile(values, 95):9.4f} {max(values):9.4f} {share:8.1f}")


METRICS = (
    ("int", "від сумарних відсотків (основна)"),
    ("full", "від повної вартості: платежі + кінцеві залишки"),
    ("pay", "від сумарних платежів (лише діагностика: некоректна, коли кінцеві залишки різні)"),
)


def main() -> None:
    """Генерує портфелі, рахує стратегії й друкує таблиці виграшу MILP."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n", type=int, default=200)
    args = parser.parse_args()

    solver = PULP_CBC_CMD(msg=0)
    print(f"CBC: PULP_CBC_CMD(msg=0), gapRel={solver.optionsDict.get('gapRel')}, "
          f"timeLimit={solver.timeLimit}")

    rng = random.Random(args.seed)
    statuses = Counter()
    unpaid = []
    wins = {(s, v, m): [] for s in STRATEGIES for v in VARIANTS for m, _ in METRICS}
    abs_int = {(s, v): [] for s in STRATEGIES for v in VARIANTS}
    negative = {}

    for idx in range(args.n):
        debts = synthetic_debts(rng, rng.randint(2, 4))
        budget = feasible_budget(rng, debts)
        milp = optimize_milp(debts, budget, MONTHS)
        statuses[milp["status"]] += 1
        milp_int = milp["total_interest_paid"]
        milp_pay = _total_paid(milp)
        milp_full = milp_pay + sum(milp["final_balances"].values())

        heur_unpaid = {}
        for s in STRATEGIES:
            repo = simulate_strategy(debts, budget, MONTHS, s)
            repo_pay = _total_paid(repo)
            legacy = simulate_local(debts, budget, MONTHS, s, False)
            spill = simulate_local(debts, budget, MONTHS, s, True)
            variants = {
                "repo": (repo["total_interest_paid"], repo_pay,
                         repo_pay + sum(repo["final_balances"].values())),
                "legacy": (legacy["total_interest_paid"], legacy["total_paid"],
                           legacy["total_paid"] + legacy["final_balance"]),
                "spill": (spill["total_interest_paid"], spill["total_paid"],
                          spill["total_paid"] + spill["final_balance"]),
            }
            heur_unpaid[s] = {"repo": repo["months_to_payoff"] is None,
                              "spill": spill["final_balance"] > 0.01}
            for v, (h_int, h_pay, h_full) in variants.items():
                wins[(s, v, "int")].append(_win_pct(h_int, milp_int))
                wins[(s, v, "pay")].append(_win_pct(h_pay, milp_pay))
                wins[(s, v, "full")].append(_win_pct(h_full, milp_full))
                abs_int[(s, v)].append(h_int - milp_int)
                if h_int - milp_int < 0:
                    negative.setdefault((s, v), []).append(h_int - milp_int)

        if milp["months_to_payoff"] is None:
            unpaid.append({
                "idx": idx,
                "n_debts": len(debts),
                "budget_ratio": budget / start_min_payments(debts),
                "milp_final": sum(milp["final_balances"].values()),
                "heur_unpaid": heur_unpaid,
            })

    print(f"Портфелів: {args.n}, seed={args.seed}, горизонт={MONTHS} міс")
    print(f"Статуси солвера: {dict(statuses)}; не Optimal: {args.n - statuses['Optimal']}; "
          f"MILP не погасив за горизонт: {len(unpaid)}")

    header = f"{'стратегія':10} {'варіант':7} {'mean':>9} {'median':>9} {'P95':>9} {'max':>9} {'>0.1%':>8}"
    for metric, title in METRICS:
        print(f"\nВиграш MILP, % {title}")
        print(header)
        for s in STRATEGIES:
            for v in VARIANTS:
                print(f"{s:10} {v:7} {_summary(wins[(s, v, metric)])}")

    print("\nВиграш MILP у відсотках, грн (mean / median / max)")
    for s in STRATEGIES:
        for v in VARIANTS:
            vals = abs_int[(s, v)]
            print(f"{s:10} {v:7} {statistics.mean(vals):10.2f} {statistics.median(vals):10.2f} "
                  f"{max(vals):10.2f}")

    if negative:
        print("\nПортфелі, де MILP гірший за евристику за відсотками (к-сть, найбільша різниця, грн):")
        for key, vals in negative.items():
            print(f"  {key}: {len(vals)}, {min(vals):.4f}")

    print("\nПортфелі, де MILP не погасив борги за горизонт:")
    print(f"{'#':>4} {'боргів':>6} {'бюджет/мін':>10} {'залишок MILP':>13}  евристики не погасили (repo/spill)")
    for u in unpaid:
        flags = ", ".join(
            f"{s}={'так' if f['repo'] else 'ні'}/{'так' if f['spill'] else 'ні'}"
            for s, f in u["heur_unpaid"].items()
        )
        print(f"{u['idx']:4} {u['n_debts']:6} {u['budget_ratio']:10.3f} {u['milp_final']:13.2f}  {flags}")
    ratios = [r for r in (u["budget_ratio"] for u in unpaid)]
    if ratios:
        print(f"бюджет/мін у непогашених: min {min(ratios):.3f}, max {max(ratios):.3f}; "
              f"діапазон генератора 1,2–2,5")


if __name__ == "__main__":
    main()
