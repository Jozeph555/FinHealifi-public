"""
Модель прогнозу вільного грошового потоку.
"""
from sqlalchemy import Column, String, Float, Boolean, DateTime, Date, JSON, ForeignKey
from sqlalchemy.sql import func
from app.models.database import Base


class Forecast(Base):
    """Прогноз доходу, витрат і вільного грошового потоку на місяць."""
    __tablename__ = "forecasts"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    generated_at = Column(DateTime(timezone=True), server_default=func.now())
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    predicted_income = Column(Float, nullable=False)
    predicted_expenses = Column(Float, nullable=False)
    free_cash_flow = Column(Float, nullable=False)
    method = Column(String, nullable=False)  # 'prophet' | 'average_fallback'
    low_confidence = Column(Boolean, default=False)  # мало історії -> fallback і попередження в UI
    raw_output = Column(JSON, nullable=True)  # вікно, причини fallback і розбивка витрат
