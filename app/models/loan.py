"""
Модель позики, виданої користувачем.
"""
from sqlalchemy import Column, String, Integer, Date, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.models.database import Base


class Loan(Base):
    """Позика, яку користувач видав іншій людині; remaining_amount зменшується при поверненнях."""
    __tablename__ = "loans"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    borrower_name = Column(String, nullable=False)
    principal_amount = Column(Integer, nullable=False)  # грн, ціле
    remaining_amount = Column(Integer, nullable=False)  # зменшується через POST /transactions/{id}/repay-loan
    issued_at = Column(Date, nullable=False)  # календарна дата без часу
    status = Column(String, nullable=False, default="not_repaid")  # 'not_repaid' | 'partially_repaid' | 'repaid'
    created_at = Column(DateTime(timezone=True), server_default=func.now())
