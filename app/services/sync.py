"""
Синхронізація транзакцій Monobank.

- run_sync_job — ручна синхронізація: один SyncJob на кілька рахунків одного
  токена, рахунки обробляються послідовно. Запускається через FastAPI
  BackgroundTasks, тобто в тому самому процесі.
- run_periodic_sync — щоденна інкрементальна синхронізація всіх рахунків
  Monobank, викликається планувальником (app/services/scheduler.py).

Після завантаження обидва шляхи запускають категоризацію і видалення
дублікатів між джерелами.
"""
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.database import AsyncSessionLocal
from app.models.transaction import Transaction
from app.models.sync_job import SyncJob
from app.models.account import Account
from app.services.monobank import (
    get_statement_chunk,
    compute_chunks,
    compute_chunks_since,
    RATE_LIMIT_SLEEP_SECONDS,
    CHUNK_SECONDS,
    INCREMENTAL_SYNC_OVERLAP_SECONDS,
)
from app.services.categorization import categorize_all_transactions
from app.services.dedup import remove_duplicate_transactions
from app.core.security import decrypt_token
import asyncio

logger = logging.getLogger(__name__)


async def _save_transactions(
    session, transactions: list[dict], account_id: str, user_id: str
) -> int:
    """Вставляє транзакції з відповіді API; наявні id пропускаються (ON CONFLICT DO NOTHING)."""
    for t in transactions:
        stmt = insert(Transaction).values(
            id=t["id"],
            user_id=user_id,
            account_id=account_id,
            time=t["time"],
            description=t.get("description"),
            mcc=t.get("mcc"),
            amount=t.get("amount"),
            amount_uah=t.get("amount", 0) // 100,
            currency_code=t.get("currencyCode"),
            cashback_amount=t.get("cashbackAmount"),
            balance=t.get("balance"),
            hold=t.get("hold", False),
            time_precision="second",  # unix-час з API Monobank — до секунди
            category=None,
            category_method=None,
        ).on_conflict_do_nothing(index_elements=["id"])
        await session.execute(stmt)
    return len(transactions)


async def run_sync_job(job_id: str, user_id: str, encrypted_token: str, months: int):
    """Виконує ручну синхронізацію рахунків SyncJob.

    Рахунки обробляються послідовно, вікнами до 31 дня з паузою між запитами.
    SyncJob.chunks_done оновлюється після кожного вікна, тож GET
    /mono/sync/status/{job_id} показує прогрес. Статуси job: running ->
    categorizing -> completed (або failed). Після завантаження запускаються
    категоризація і видалення дублікатів; кількість видалених зберігається в
    SyncJob.duplicates_removed (None, якщо дедуплікація не вдалася).

    Args:
        job_id: Id запису SyncJob.
        user_id: Власник рахунків.
        encrypted_token: Зашифрований токен Monobank.
        months: Глибина історії в місяцях.
    """
    token = decrypt_token(encrypted_token)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SyncJob).where(SyncJob.id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            return

        account_ids = job.account_ids
        all_windows = {
            account_id: compute_chunks(months) for account_id in account_ids
        }
        total_chunks = sum(len(w) for w in all_windows.values())

        job.status = "running"
        job.chunks_total = total_chunks
        job.chunks_done = 0
        job.started_at = datetime.now(timezone.utc)
        await session.commit()

        try:
            chunk_index = 0
            for account_id in account_ids:
                job.current_account_id = account_id
                await session.commit()

                windows = all_windows[account_id]
                for i, (start, end) in enumerate(windows):
                    transactions = await get_statement_chunk(token, account_id, start, end)
                    await _save_transactions(session, transactions, account_id, user_id)
                    await session.commit()

                    chunk_index += 1
                    job.chunks_done = chunk_index
                    await session.commit()

                    is_last_chunk_overall = chunk_index == total_chunks
                    if not is_last_chunk_overall:
                        await asyncio.sleep(RATE_LIMIT_SLEEP_SECONDS)

                # Курсор Account.last_synced_at спільний для ручної й періодичної
                # синхронізації; посувається після успішного завершення рахунку.
                account = await session.get(Account, account_id)
                if account:
                    account.last_synced_at = datetime.now(timezone.utc)
                    account.last_sync_status = "success"
                    account.last_sync_error = None
                    await session.commit()

            # Далі — категоризація.
            job.current_account_id = None
            job.status = "categorizing"
            await session.commit()

        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)
            # Помилку отримує лише рахунок, на якому job зупинився; раніше
            # оброблені рахунки вже мають статус success.
            if job.current_account_id:
                failed_account = await session.get(Account, job.current_account_id)
                if failed_account:
                    failed_account.last_sync_status = "failed"
                    failed_account.last_sync_error = str(e)
            await session.commit()
            return

    # categorize_all_transactions відкриває власну сесію БД.
    try:
        await categorize_all_transactions()
    except Exception as e:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(SyncJob).where(SyncJob.id == job_id))
            job = result.scalar_one_or_none()
            if job:
                job.status = "failed"
                job.error_message = f"Sync успішний, але категоризація впала: {e}"
                await session.commit()
        return

    # Видалення дублікатів між джерелами (наприклад, той самий період уже
    # імпортовано з виписки). Помилка логується і не скасовує статус completed.
    duplicates_removed = None
    try:
        async with AsyncSessionLocal() as dedup_session:
            dedup_result = await remove_duplicate_transactions(dedup_session, user_id)
            await dedup_session.commit()
            duplicates_removed = dedup_result.removed_count
    except Exception:
        logger.exception("Дедуплікація після sync job=%s user=%s впала", job_id, user_id)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SyncJob).where(SyncJob.id == job_id))
        job = result.scalar_one_or_none()
        if job:
            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            job.duplicates_removed = duplicates_removed
            await session.commit()


async def run_periodic_sync(session=None):
    """Інкрементально синхронізує всі рахунки Monobank усіх користувачів.

    - Для рахунку з курсором last_synced_at завантажує період від курсора мінус
      INCREMENTAL_SYNC_OVERLAP_SECONDS до поточного моменту; для рахунку без
      курсора — останні CHUNK_SECONDS (31 день). Повну історію завантажує лише
      ручна синхронізація (POST /mono/sync/start).
    - Рахунки обробляються послідовно, з паузою RATE_LIMIT_SLEEP_SECONDS між
      усіма запитами.
    - Помилка одного рахунку записується в його last_sync_status і
      last_sync_error, решта рахунків обробляється далі.

    Після завантаження запускає категоризацію й видалення дублікатів; помилки
    цих кроків лише логуються.

    Args:
        session: Сесія БД; якщо не задано, відкривається власна.
    """
    if session is not None:
        await _run_periodic_sync(session)
    else:
        async with AsyncSessionLocal() as new_session:
            await _run_periodic_sync(new_session)

    # Помилка категоризації не відкочує вже збережені транзакції.
    try:
        await categorize_all_transactions()
    except Exception:
        pass

    # Дедуплікація для всіх користувачів (user_id=None). SyncJob тут немає,
    # тож результат лише логується.
    try:
        if session is not None:
            dedup_result = await remove_duplicate_transactions(session, None)
            await session.commit()
        else:
            async with AsyncSessionLocal() as dedup_session:
                dedup_result = await remove_duplicate_transactions(dedup_session, None)
                await dedup_session.commit()
        if dedup_result.removed_count:
            logger.info(
                "Періодичний sync: дедуп видалив %d транзакцій", dedup_result.removed_count
            )
    except Exception:
        logger.exception("Дедуплікація після періодичного sync впала")


async def _run_periodic_sync(session) -> None:
    """Завантажує нові транзакції для всіх рахунків Monobank з токеном."""
    result = await session.execute(
        select(Account).where(Account.source_type == "monobank_api")
    )
    accounts = [a for a in result.scalars().all() if a.mono_token_encrypted]
    if not accounts:
        return

    now_ts = int(time.time())
    account_windows = {}
    for account in accounts:
        if account.last_synced_at is not None:
            since_ts = int(account.last_synced_at.timestamp()) - INCREMENTAL_SYNC_OVERLAP_SECONDS
        else:
            since_ts = now_ts - CHUNK_SECONDS
        account_windows[account.id] = compute_chunks_since(since_ts, now_ts)

    total_chunks = sum(len(w) for w in account_windows.values())
    chunk_index = 0

    for account in accounts:
        token = decrypt_token(account.mono_token_encrypted)
        windows = account_windows[account.id]
        db_account = await session.get(Account, account.id)

        try:
            for start, end in windows:
                transactions = await get_statement_chunk(token, account.id, start, end)
                await _save_transactions(session, transactions, account.id, account.user_id)
                await session.commit()

                chunk_index += 1
                if chunk_index < total_chunks:
                    await asyncio.sleep(RATE_LIMIT_SLEEP_SECONDS)

            db_account.last_synced_at = datetime.now(timezone.utc)
            db_account.last_sync_status = "success"
            db_account.last_sync_error = None
            await session.commit()
        except Exception as e:
            await session.rollback()
            db_account = await session.get(Account, account.id)
            if db_account:
                db_account.last_sync_status = "failed"
                db_account.last_sync_error = str(e)
                await session.commit()
            continue
