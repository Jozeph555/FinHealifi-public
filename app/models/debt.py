"""
Модель боргу користувача.
"""
from sqlalchemy import Column, String, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.models.database import Base


class Debt(Base):
    """Борг користувача: кредитна картка (revolving) або розстрочка чи кредит (installment).

    monthly_rate — частка за місяць (0.04 = 4 %/міс). Погашений борг
    позначається is_active=False.
    """
    __tablename__ = "debts"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    balance = Column(Float, nullable=False)
    monthly_rate = Column(Float, nullable=False, default=0.0)
    min_payment = Column(Float, nullable=True)
    debt_type = Column(String, nullable=False)  # 'revolving' | 'installment'
    is_active = Column(Boolean, default=True)  # False — погашено
    created_at = Column(DateTime(timezone=True), server_default=func.now())
