"""
Модель транзакції.
"""
from sqlalchemy import Column, String, Integer, Float, Boolean, BigInteger, DateTime, ForeignKey, CheckConstraint
from sqlalchemy.sql import func
from app.models.database import Base
 
 
class Transaction(Base):
    """Транзакція з будь-якого джерела (Monobank, виписка, ручне введення).

    amount — сума в копійках (від'ємна — витрата); amount_uah = amount // 100.
    """
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "time_precision IN ('date', 'minute', 'second')",
            name="ck_transactions_time_precision",
        ),
    )
 
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False, index=True)
    time = Column(BigInteger, nullable=False)
    # Точність `time`: 'date' — відома лише дата (time = 00:00 місцевого дня),
    # 'minute' — до хвилини, 'second' — точний час. Значення за замовчуванням немає:
    # кожен шлях вставки задає його явно — sync.py ('second'), transactions.py
    # ('date'), imports.py (за колонкою файлу; PDF — 'date').
    time_precision = Column(String, nullable=False)
    description = Column(String)
    mcc = Column(Integer)
    amount = Column(Integer)  # в копійках
    amount_uah = Column(Integer)  # amount // 100
    currency_code = Column(Integer)
    cashback_amount = Column(Integer)
    balance = Column(BigInteger)
    hold = Column(Boolean, default=False)
    category = Column(String, nullable=True)  # заповнюється після категоризації
    # 'mcc' | 'keyword' | 'pending' | 'bank_provided' | 'manual' | 'reimbursement' |
    # 'income_backfill_fallback' | 'ml'
    category_method = Column(String, nullable=True)
    # Упевненість класифікатора (max_proba); заповнюється лише для
    # category_method='ml'. low_confidence — category_confidence <
    # LOW_CONFIDENCE_THRESHOLD, окремої колонки немає.
    category_confidence = Column(Float, nullable=True)
    loan_id = Column(String, ForeignKey("loans.id"), nullable=True, index=True)  # заповнюється через POST /transactions/{id}/repay-loan
    # Порядок вставки для вибору рядка серед дублікатів з однаковим source_type
    # (id — випадковий uuid). clock_timestamp() дає різний час кожному рядку навіть
    # у межах однієї транзакції БД, на відміну від now().
    created_at = Column(DateTime(timezone=True), server_default=func.clock_timestamp())
