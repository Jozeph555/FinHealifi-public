"""
Модель навчального прикладу для ML-категоризатора.
"""
from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.models.database import Base


class CategoryTrainingExample(Base):
    """Навчальний приклад «опис -> категорія» для ML-категоризатора.

    Пул наповнюють лише правила (MCC, ключові слова) і ручне підтвердження
    категорії (categorization.record_training_example); передбачення ML у пул не
    потрапляють. source_transaction_id унікальний: повторна категоризація тієї
    самої транзакції оновлює приклад.
    """

    __tablename__ = "category_training_examples"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    source_transaction_id = Column(String, nullable=False, unique=True, index=True)
    description = Column(String, nullable=False)
    category = Column(String, nullable=False)  # канонічна категорія (після CATEGORY_MERGE)
    source = Column(String, nullable=False)  # 'mcc' | 'keyword' | 'manual_confirmed'
    created_at = Column(DateTime(timezone=True), server_default=func.now())
