"""
Відновлення запуску дедуплікації з архіву transaction_dedup_archive у transactions.

Логіка — app.services.dedup_apply.restore_dedup_run; коміт і відкат — у цьому
скрипті. За замовчуванням — dry-run (перевірки без змін); запис — з --apply.
Рядки повертаються без змін (усі колонки, зокрема created_at і
time_precision); рядки архіву лишаються й отримують restored_at.

Відмова, якщо: запуск не знайдено або вже відновлено; хоч один id уже є в
transactions; немає користувача, рахунку чи позики рядка; зник рядок, що
лишався при застосуванні (kept_id) — останнє дозволяє --allow-missing-kept.

БД — з DATABASE_URL; порт звіряється з --expect-db-port
(scripts/data_fixes/_db_guard.py), для основної БД (5436) потрібен ще й
ALLOW_MAIN_DB=1.

Запуск (з кореня репозиторію):
    python -m scripts.data_fixes.dedup_restore --expect-db-port <port> --run-id <run_id>
    python -m scripts.data_fixes.dedup_restore --expect-db-port <port> --run-id <run_id> --apply [--allow-missing-kept]
Коди виходу: 0 — успіх; 2 — відмова; 3 — порушення інваріанта.
"""
import argparse
import asyncio
import os
import sys

from app.models.database import AsyncSessionLocal, engine
from app.services.dedup_apply import DedupInvariantError, DedupRefused, restore_dedup_run
from scripts.data_fixes._db_guard import DbGuardRefused, check_db_target


async def main(run_id: str, apply: bool, allow_missing_kept: bool, expect_db_port: int) -> int:
    """Виконує перевірку або відновлення запуску.

    Returns:
        Код виходу (див. докстрінг модуля).
    """
    engine.echo = False
    # До першої сесії: перевірка URL без з'єднання.
    try:
        target = check_db_target(engine.url, expect_db_port, os.environ)
    except DbGuardRefused as e:
        print(f"ВІДМОВА (db_guard): {e}. До БД не підключались.")
        return 2
    print(f"БД: {target}")
    print(f"Режим: {'ЗАПИС (--apply)' if apply else 'dry-run (нічого не змінюється)'}")
    async with AsyncSessionLocal() as session:
        try:
            result = await restore_dedup_run(
                session, run_id, allow_missing_kept=allow_missing_kept, dry_run=not apply,
            )
        except DedupRefused as e:
            await session.rollback()
            print(f"ВІДМОВА ({e.reason}): {e}. Нічого не змінено.")
            return 2
        except DedupInvariantError as e:
            await session.rollback()
            print(f"ПОРУШЕНО ІНВАРІАНТ — транзакцію відкочено, нічого не змінено. {e}")
            return 3
        except Exception:
            await session.rollback()
            raise

        print(f"Рядків у запуску: {result.rows}; зниклих збережених копій: {result.missing_kept}")
        if not apply:
            await session.rollback()
            print("dry-run: нічого не змінено. Для запису: --apply")
            return 0
        await session.commit()
        print(f"ВІДНОВЛЕНО: {result.rows} рядків")
        for name, (got, expected) in result.checks.items():
            print(f"  ✓ {name}: {got} (очікувалось {expected})")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expect-db-port", type=int, required=True,
                        help="очікуваний порт БД; для основної (5436) ще й ALLOW_MAIN_DB=1")
    parser.add_argument("--apply", action="store_true", help="записати (інакше dry-run)")
    parser.add_argument("--allow-missing-kept", action="store_true",
                        help="відновлювати навіть якщо збережену копію вже видалено")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.run_id, args.apply, args.allow_missing_kept, args.expect_db_port)))
