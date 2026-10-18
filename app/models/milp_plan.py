"""
Модель збереженого плану погашення боргів.
"""
from sqlalchemy import Column, String, Integer, Float, DateTime, JSON, ForeignKey
from sqlalchemy.sql import func
from app.models.database import Base


class MilpPlan(Base):
    """Збережений план погашення боргів.

    plan_data містить результати всіх чотирьох стратегій. extra_monthly_uah і
    debts_fingerprint заповнюються лише для планів з POST /debt/optimize.
    """
    __tablename__ = "milp_plans"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    forecast_id = Column(String, ForeignKey("forecasts.id"), nullable=True, index=True)
    generated_at = Column(DateTime(timezone=True), server_default=func.now())
    months_to_payoff = Column(Integer, nullable=False)
    total_interest_paid = Column(Float, nullable=False)
    plan_data = Column(JSON, nullable=False)  # результати стратегій з debt_optimizer.py, зокрема monthly_log
    status = Column(String, nullable=False, default="active")  # 'active' | 'superseded' | 'completed'
    # NULL, якщо план побудовано не через POST /debt/optimize (додатковий
    # платіж і актуальність невідомі).
    extra_monthly_uah = Column(Float, nullable=True)  # гривні, як решта сум плану
    debts_fingerprint = Column(String(64), nullable=True)  # sha256 hex, app/services/debt_fingerprint.py
