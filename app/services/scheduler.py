"""
Планувальник фонових задач у процесі застосунку (APScheduler).

Раз на 24 години запускає інкрементальну синхронізацію Monobank
(app.services.sync.run_periodic_sync). Задача живе в пам'яті процесу: не
переживає перезапуск uvicorn і не координується між кількома воркерами.
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services.sync import run_periodic_sync

scheduler = AsyncIOScheduler()


def start_scheduler():
    """Реєструє щоденну синхронізацію Monobank і запускає планувальник."""
    scheduler.add_job(
        run_periodic_sync,
        "interval",
        hours=24,
        id="periodic_monobank_sync",
        replace_existing=True,
    )
    scheduler.start()


def stop_scheduler():
    """Зупиняє планувальник, не чекаючи завершення поточних задач."""
    scheduler.shutdown(wait=False)
