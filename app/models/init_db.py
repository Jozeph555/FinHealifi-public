"""
Створення таблиць через Base.metadata.create_all() для швидкого локального старту; схему БД веде Alembic.
"""
import asyncio
from app.models.database import engine, Base
 
# Моделі мають бути імпортовані, щоб потрапити в Base.metadata.
from app.models.user import User
from app.models.account import Account
from app.models.transaction import Transaction
from app.models.debt import Debt
from app.models.loan import Loan
from app.models.forecast import Forecast
from app.models.milp_plan import MilpPlan
from app.models.sync_job import SyncJob


async def init_db():
    """Створює таблиці моделей, імпортованих у цьому модулі."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Таблиці створені: users, accounts, transactions, debts, loans, forecasts, milp_plans, sync_jobs")
 
 
if __name__ == "__main__":
    asyncio.run(init_db())
