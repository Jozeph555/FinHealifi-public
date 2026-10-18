"""
Модель задачі синхронізації Monobank.
"""
from sqlalchemy import Column, String, Integer, DateTime, JSON, ForeignKey
from app.models.database import Base


class SyncJob(Base):
    """Задача ручної синхронізації кількох рахунків Monobank і її прогрес."""
    __tablename__ = "sync_jobs"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    account_ids = Column(JSON, nullable=False)  # список account_id, синхронізуються послідовно
    current_account_id = Column(String, nullable=True)  # який рахунок обробляється зараз
    status = Column(String, nullable=False, default="pending")  # pending | running | categorizing | completed | failed
    chunks_done = Column(Integer, default=0)  # прогрес по всіх обраних рахунках
    chunks_total = Column(Integer, default=0)
    duplicates_removed = Column(Integer, nullable=True)  # кількість видалених дублікатів; None — дедуплікація не виконана
    error_message = Column(String, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
