"""
Клієнт Monobank API.

Токен передається в кожну функцію явно: у кожного користувача свій токен,
збережений у зашифрованому вигляді в Account.mono_token_encrypted.
"""
import time
from datetime import datetime, timedelta

import httpx

MONO_BASE_URL = "https://api.monobank.ua"

# 31 день у секундах — максимальний період одного запиту за документацією Monobank
CHUNK_SECONDS = 2_682_000
RATE_LIMIT_SLEEP_SECONDS = 61  # 1 запит / 60 сек + запас
# Нахлест назад від Account.last_synced_at для інкрементального sync. Повторне
# завантаження накладеного вікна не дублює рядки: _save_transactions (sync.py)
# вставляє з ON CONFLICT DO NOTHING.
INCREMENTAL_SYNC_OVERLAP_SECONDS = 86400


async def get_client_info(token: str) -> dict:
    """Повертає інформацію про клієнта та його рахунки (/personal/client-info).

    Raises:
        httpx.HTTPStatusError: API повернув помилку (зокрема недійсний токен).
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{MONO_BASE_URL}/personal/client-info",
            headers={"X-Token": token},
        )
        response.raise_for_status()
        return response.json()


async def get_statement_chunk(token: str, account_id: str, start: int, end: int) -> list[dict]:
    """Повертає транзакції рахунку за одне вікно до 31 дня.

    Один запит до API; цикл по вікнах і паузи між запитами веде sync.py.

    Raises:
        httpx.HTTPStatusError: API повернув помилку.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{MONO_BASE_URL}/personal/statement/{account_id}/{start}/{end}",
            headers={"X-Token": token},
        )
        response.raise_for_status()
        return response.json()


def _compute_windows(start_time: int, end_time: int) -> list[tuple[int, int]]:
    """Розбиває [start_time, end_time] на вікна до 31 дня, від найновішого до найстарішого."""
    windows = []
    current_end = end_time
    while current_end > start_time:
        current_start = max(current_end - CHUNK_SECONDS, start_time)
        windows.append((current_start, current_end))
        current_end = current_start
    return windows


def compute_chunks(months: int) -> list[tuple[int, int]]:
    """Розбиває останні `months` місяців на вікна до 31 дня, від найновішого до найстарішого."""
    end_time = int(time.time())
    start_time = int((datetime.now() - timedelta(days=months * 30)).timestamp())
    return _compute_windows(start_time, end_time)


def compute_chunks_since(since_ts: int, until_ts: int | None = None) -> list[tuple[int, int]]:
    """Розбиває період від курсора since_ts до until_ts на вікна до 31 дня.

    Args:
        since_ts: Початок періоду, Unix-час.
        until_ts: Кінець періоду, Unix-час; за замовчуванням — поточний момент.
    """
    end_time = until_ts if until_ts is not None else int(time.time())
    return _compute_windows(since_ts, end_time)
