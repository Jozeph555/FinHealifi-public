"""
Застосування плану дедуплікації: перенесення дублікатів між джерелами в
архів transaction_dedup_archive і видалення їх з transactions.

Логіка — app.services.dedup_apply.apply_dedup_plan; коміт і відкат — у цьому
скрипті. За замовчуванням — dry-run: план, лічильники, суми в копійках і
відбиток плану. Запис — лише з --apply і відбитком із dry-run
(--expect-fingerprint): якщо план змінився, запис скасовується. Будь-яка
відмова чи порушення інваріанта — відкат без змін. Відновлення —
scripts/data_fixes/dedup_restore.py --run-id <run_id>.

Друкує лише лічильники й сукупні суми. БД — з DATABASE_URL; порт
звіряється з --expect-db-port (scripts/data_fixes/_db_guard.py), для
основної БД (5436) потрібен ще й ALLOW_MAIN_DB=1.

Запуск (з кореня репозиторію):
    python -m scripts.data_fixes.dedup_apply --expect-db-port <port> --user-id <id>
    python -m scripts.data_fixes.dedup_apply --expect-db-port <port> --user-id <id> --apply --expect-fingerprint <hash>
Коди виходу: 0 — успіх або дублікатів немає; 2 — відмова; 3 — порушення інваріанта.
"""
import argparse
import asyncio
import os
import sys
import uuid

from app.models.database import AsyncSessionLocal, engine
from app.services.dedup_apply import DedupInvariantError, DedupRefused, apply_dedup_plan
from scripts.data_fixes._db_guard import DbGuardRefused, check_db_target


def _uah(kopecks: int) -> str:
    """Форматує суму в копійках як гривні."""
    return f"{abs(kopecks) // 100:,}".replace(",", " ")


async def main(user_id: str, apply: bool, expect_fingerprint: str | None, expect_db_port: int) -> int:
    """Виконує dry-run або застосування плану для користувача.

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
    run_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        try:
            result = await apply_dedup_plan(
                session, user_id, run_id, expect_fingerprint=expect_fingerprint, dry_run=not apply,
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

        print(f"Пар у плані: {result.planned}")
        print(f"  витрати: {result.outflow_kopecks} коп. ({_uah(result.outflow_kopecks)} грн); "
              f"надходження: {result.inflow_kopecks} коп. ({_uah(result.inflow_kopecks)} грн)")
        print(f"Відбиток плану: {result.fingerprint}")
        if not apply:
            await session.rollback()
            print("dry-run: нічого не змінено. Для запису: --apply --expect-fingerprint <відбиток>")
            return 0
        if not result.applied:
            await session.rollback()
            print("Нічого робити: однозначних пар немає.")
            return 0
        await session.commit()
        print(f"ЗАСТОСОВАНО. run_id = {run_id}; видалено й заархівовано: {result.removed}")
        for name, (got, expected) in result.checks.items():
            print(f"  ✓ {name}: {got} (очікувалось {expected})")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--expect-db-port", type=int, required=True,
                        help="очікуваний порт БД; для основної (5436) ще й ALLOW_MAIN_DB=1")
    parser.add_argument("--apply", action="store_true", help="записати (інакше dry-run)")
    parser.add_argument("--expect-fingerprint", default=None, help="відбиток плану з dry-run")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.user_id, args.apply, args.expect_fingerprint, args.expect_db_port)))
