"""
Модель активу користувача.
"""
from sqlalchemy import Column, String, Integer, Float, Boolean, Date, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.models.database import Base


class Asset(Base):
    """Актив користувача: заощадження, інвестиції, нерухомість, готівка.

    Довідковий запис: current_amount оновлюється вручну і не пов'язаний з
    транзакціями. Архівування — is_active=False.
    """
    __tablename__ = "assets"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    asset_type = Column(String, nullable=False)  # довільний рядок; рекомендовані типи — GET /assets/types
    current_amount = Column(Integer, nullable=False)  # грн, ціле
    liquidity_level = Column(String, nullable=False)  # 'high' | 'medium' | 'low'
    liquidity_days = Column(Integer, nullable=True)
    annual_rate = Column(Float, nullable=True)  # % річних, напр. 2.0 = 2%/рік
    rate_note = Column(String, nullable=True)
    opened_at = Column(Date, nullable=True)  # календарна дата без часу
    is_active = Column(Boolean, default=True)  # False — архівовано
    created_at = Column(DateTime(timezone=True), server_default=func.now())
