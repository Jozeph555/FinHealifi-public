"""
Модель довідника банків.
"""
from sqlalchemy import Column, String, Boolean
from app.models.database import Base


class Bank(Base):
    """Банк з довідника — ідентифікатор для дедуплікації транзакцій між джерелами.

    mfo — код МФО (6 цифр); id — рядковий ідентифікатор для Account.bank_id.
    is_seed=True — банк з початкового списку топ-20 (дані НБУ); is_seed=False —
    доданий користувачем під час імпорту виписки.
    """
    __tablename__ = "banks"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    mfo = Column(String, nullable=True)
    is_seed = Column(Boolean, nullable=False, default=False)
