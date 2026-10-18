"""
Модель архіву транзакцій, видалених як дублікати.
"""
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, BigInteger, DateTime, JSON,
    CheckConstraint, PrimaryKeyConstraint, Index,
)
from sqlalchemy.sql import func
from app.models.database import Base


class TransactionDedupArchive(Base):
    """Архів транзакцій, видалених як дублікати між джерелами.

    Рядок архіву — повна копія рядка transactions (усі колонки з тими самими
    типами) плюс метадані запуску; restore_dedup_run повертає його без змін.
    Зовнішніх ключів немає: наявність пов'язаних записів перевіряє restore.
    PK (run_id, id): після відновлення той самий рядок можна заархівувати знову.
    """

    __tablename__ = "transaction_dedup_archive"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "id", name="pk_transaction_dedup_archive"),
        CheckConstraint(
            "time_precision IN ('date', 'minute', 'second')",
            name="ck_transaction_dedup_archive_time_precision",
        ),
        CheckConstraint(
            "match_kind IN ('date', 'minute', 'second')",
            name="ck_transaction_dedup_archive_match_kind",
        ),
        Index("ix_transaction_dedup_archive_run_id", "run_id"),
        Index("ix_transaction_dedup_archive_user_id", "user_id"),
        Index("ix_transaction_dedup_archive_kept_id", "kept_id"),
    )

    # --- копія рядка transactions (ті самі назви й типи) ---
    id = Column(String, nullable=False)
    user_id = Column(String, nullable=False)
    account_id = Column(String, nullable=False)
    time = Column(BigInteger, nullable=False)
    time_precision = Column(String, nullable=False)
    description = Column(String)
    mcc = Column(Integer)
    amount = Column(Integer)
    amount_uah = Column(Integer)
    currency_code = Column(Integer)
    cashback_amount = Column(Integer)
    balance = Column(BigInteger)
    hold = Column(Boolean)
    category = Column(String)
    category_method = Column(String)
    category_confidence = Column(Float)
    loan_id = Column(String)
    created_at = Column(DateTime(timezone=True))

    # --- метадані запуску ---
    run_id = Column(String, nullable=False)
    archived_at = Column(DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp())
    reason = Column(String, nullable=False)  # 'cross_source_duplicate'
    match_kind = Column(String, nullable=False)  # точність, за якою зіставлено пару
    kept_id = Column(String, nullable=False)  # id збереженої копії, без FK
    transfers = Column(JSON, nullable=True)  # завжди NULL: перенесення атрибутів не підтримується
    restored_at = Column(DateTime(timezone=True), nullable=True)
