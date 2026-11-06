"""
FastAPI-застосунок: роутери, CORS і запуск планувальника.
"""
import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.mono import router as mono_router
from app.api.debt import router as debt_router
from app.api.debt_plan_fact import router as debt_plan_fact_router
from app.api.loans import router as loans_router
from app.api.assets import router as assets_router
from app.api.banks import router as banks_router
from app.api.advisor import router as advisor_router
from app.api.forecast import router as forecast_router
from app.api.imports import router as imports_router
from app.api.transactions import router as transactions_router
from app.core.config import APP_TODAY_OVERRIDE_ENV, today_override
from app.services.scheduler import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)


def warn_today_override() -> date | None:
    """Пише WARNING у журнал, якщо задано APP_TODAY_OVERRIDE; повертає цю дату або None."""
    override = today_override()
    if override is not None:
        logger.warning("%s=%s: «сьогодні» для плану й план-факту — %s, а не реальна дата",
                       APP_TODAY_OVERRIDE_ENV, override.isoformat(), override.isoformat())
    return override


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Запускає планувальник фонових задач на старті застосунку й зупиняє на завершенні.

    На старті пише WARNING, якщо задано APP_TODAY_OVERRIDE. Планувальник —
    щоденна синхронізація Monobank (app/services/scheduler.py).
    """
    warn_today_override()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="FinHealifi API", lifespan=lifespan)

# CORS: запити з фронтенду (Vite dev-сервер, localhost:5173).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev-сервер
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Заголовки з кількістю й сумою рядків GET /transactions, доступні фронтенду.
    expose_headers=["X-Total-Count", "X-Total-Amount"],
)

app.include_router(auth_router)
app.include_router(mono_router)
app.include_router(forecast_router)
app.include_router(debt_router)
app.include_router(debt_plan_fact_router)
app.include_router(loans_router)
app.include_router(assets_router)
app.include_router(banks_router)
app.include_router(advisor_router)
app.include_router(imports_router)
app.include_router(transactions_router)


@app.get("/")
async def root():
    """Перевірка доступності API."""
    return {"status": "ok", "project": "FinHealifi"}
