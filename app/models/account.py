"""
Модель рахунку — джерела транзакцій користувача.
"""
from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.models.database import Base


class Account(Base):
    """Джерело фінансових даних користувача.

    source_type визначає, які поля заповнені:
      - 'monobank_api' -> mono_token_encrypted заповнений, bank_name = 'monobank';
      - 'csv_import'    -> bank_name заповнений, mono_token_encrypted = None
                           (виписки CSV, XLSX і PDF);
      - 'manual'        -> mono_token_encrypted і bank_name = None,
                           id має вигляд f"manual-{user_id}".

    last_synced_at, last_sync_status і last_sync_error — курсор і стан
    синхронізації Monobank, спільні для ручної й періодичної синхронізації.

    bank_id (FK -> banks.id) — ідентифікатор банку для дедуплікації між
    джерелами; bank_name — текст для відображення. bank_id заповнений для
    'monobank_api' (завжди "monobank") і 'csv_import'; для 'manual' — None.
    """
    __tablename__ = "accounts"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    source_type = Column(String, nullable=False)  # 'monobank_api' | 'csv_import' | 'manual'
    mono_token_encrypted = Column(String, nullable=True)
    bank_name = Column(String, nullable=True)
    bank_id = Column(String, ForeignKey("banks.id"), nullable=True)
    masked_pan = Column(String, nullable=True)
    currency_code = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_status = Column(String, nullable=True)  # 'success' | 'failed'
    last_sync_error = Column(String, nullable=True)
