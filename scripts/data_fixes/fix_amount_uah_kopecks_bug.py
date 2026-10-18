"""
Виправлення amount_uah у транзакціях, де він не дорівнює FLOOR(amount / 100).

Друкує кількість таких рядків за джерелами до і після виправлення. Чіпає лише
рядки з розбіжністю, тож повторний запуск безпечний.

Запуск (з кореня репозиторію, після `docker-compose up -d`):
    python -m scripts.data_fixes.fix_amount_uah_kopecks_bug          # лише звіт
    python -m scripts.data_fixes.fix_amount_uah_kopecks_bug --apply  # звіт і виправлення
"""
import argparse
import asyncio

from sqlalchemy import text

from app.models.database import AsyncSessionLocal

DIAGNOSTIC_QUERY = text("""
    SELECT a.source_type,
           count(*) AS total,
           count(*) FILTER (
               WHERE t.amount_uah <> FLOOR(t.amount::numeric / 100)
           ) AS mismatched
    FROM transactions t
    JOIN accounts a ON a.id = t.account_id
    GROUP BY a.source_type
    ORDER BY a.source_type
""")

FIX_QUERY = text("""
    UPDATE transactions
    SET amount_uah = FLOOR(amount::numeric / 100)
    WHERE amount_uah <> FLOOR(amount::numeric / 100)
""")


async def _report(session, label: str):
    """Друкує кількість транзакцій і розбіжностей amount_uah за джерелами."""
    rows = (await session.execute(DIAGNOSTIC_QUERY)).all()
    print(f"\n--- {label} ---")
    if not rows:
        print("Транзакцій немає.")
        return
    for source_type, total, mismatched in rows:
        flag = " <-- РОЗБІЖНІСТЬ" if mismatched else ""
        print(f"{source_type:15} total={total:5} mismatched={mismatched:5}{flag}")


async def main(apply: bool):
    """Друкує звіт і, з apply=True, виправляє рядки з розбіжністю."""
    async with AsyncSessionLocal() as session:
        await _report(session, "До")

        if not apply:
            print("\nDry-run (без --apply) — нічого не змінено.")
            return

        result = await session.execute(FIX_QUERY)
        await session.commit()
        print(f"\nОновлено рядків: {result.rowcount}")

        await _report(session, "Після")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Справді виконати UPDATE (без цього — лише звіт)")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
