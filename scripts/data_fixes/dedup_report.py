"""
Звіт плану дедуплікації: що зіставилося б як дублікат, які групи
неоднозначні, де конфлікти. Лише читає БД (app.services.dedup.plan_duplicate_removal).

Друкує тільки лічильники й сукупні суми, без описів, імен, сум чи дат
окремих транзакцій.

Запуск (з кореня репозиторію; БД — з DATABASE_URL):
    python -m scripts.data_fixes.dedup_report                 # усі користувачі
    python -m scripts.data_fixes.dedup_report --user-id <id>  # один користувач
"""
import argparse
import asyncio
from collections import Counter

from sqlalchemy import text

from app.models.database import AsyncSessionLocal, engine
from app.services.dedup import plan_duplicate_removal

USERS_QUERY = text("SELECT DISTINCT user_id FROM transactions ORDER BY user_id")

# Пари (місцева дата за Києвом, сума в копійках) в одному рахунку; план їх
# не зіставляє, у звіті вони для контролю.
SAME_ACCOUNT_PAIRS_QUERY = text("""
    WITH t AS (
        SELECT id, account_id, amount,
               (to_timestamp(time) AT TIME ZONE 'Europe/Kyiv')::date AS d
        FROM transactions WHERE user_id = :user_id
    )
    SELECT a.id AS id_a, b.id AS id_b
    FROM t a JOIN t b
      ON a.account_id = b.account_id AND a.d = b.d AND a.amount = b.amount AND a.id < b.id
""")

ACCOUNT_SOURCE_QUERY = text("""
    SELECT t.id, a.source_type FROM transactions t JOIN accounts a ON a.id = t.account_id
    WHERE t.user_id = :user_id
""")


def _uah(matches) -> str:
    """Повертає обсяг пар (сума модулів сум) у гривнях."""
    total = sum(abs(m.amount) for m in matches) // 100
    return f"{total:,}".replace(",", " ")


async def _report_user(session, user_id: str) -> None:
    """Друкує звіт плану дедуплікації одного користувача."""
    plan = await plan_duplicate_removal(session, user_id)
    source_of = dict((await session.execute(ACCOUNT_SOURCE_QUERY, {"user_id": user_id})).all())
    same_account = (await session.execute(SAME_ACCOUNT_PAIRS_QUERY, {"user_id": user_id})).all()

    matched_ids = {m.duplicate_id for m in plan.matches} | {m.survivor_id for m in plan.matches}
    same_account_touched = sum(1 for a, b in same_account if a in matched_ids and b in matched_ids)

    print(f"\nКористувач {user_id[:8]}…")
    outflows = [m for m in plan.matches if m.amount < 0]
    inflows = [m for m in plan.matches if m.amount > 0]
    print(f"  Однозначні пари (буде видалено дублікатів): {len(plan.matches)}, обсяг {_uah(plan.matches)} грн")
    print(f"    витрати: {len(outflows)} пар, {_uah(outflows)} грн; надходження: {len(inflows)} пар, {_uah(inflows)} грн")
    by_kind = Counter(m.kind for m in plan.matches)
    for kind in ("second", "minute", "date"):
        kind_matches = [m for m in plan.matches if m.kind == kind]
        print(f"    за точністю {kind:6}: {by_kind[kind]:4} пар, обсяг {_uah(kind_matches)} грн")
    print(f"    з них хоча б один бік з точністю date: {by_kind['date']}")
    by_sources = Counter(
        " ↔ ".join(sorted((source_of[m.survivor_id], source_of[m.duplicate_id]))) for m in plan.matches
    )
    for sources, count in sorted(by_sources.items()):
        print(f"    джерела {sources}: {count}")
    print(f"    з перенесенням атрибутів: {sum(1 for m in plan.matches if m.transfers)} "
          f"(категорія: {sum(1 for m in plan.matches if 'category' in m.transfers)}, "
          f"loan_id: {sum(1 for m in plan.matches if 'loan_id' in m.transfers)})")
    print(f"  Неоднозначні групи (лише звіт): {len(plan.ambiguous)}, "
          f"видалило б за правилом max по джерелах: {sum(g.would_remove for g in plan.ambiguous)}")
    print(f"  Конфлікти (пара не зливається): {len(plan.conflicts)} {dict(Counter(c.reason for c in plan.conflicts))}")
    print(f"  Пари (місцева дата, сума) в одному рахунку: {len(same_account)}, "
          f"з них зачеплено планом: {same_account_touched}")


async def main(user_id: str | None) -> None:
    """Друкує звіт для одного або всіх користувачів."""
    engine.echo = False
    async with AsyncSessionLocal() as session:
        user_ids = [user_id] if user_id else list((await session.execute(USERS_QUERY)).scalars())
        for uid in user_ids:
            await _report_user(session, uid)
        await session.rollback()  # лише читання — явно нічого не фіксуємо


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.user_id))
