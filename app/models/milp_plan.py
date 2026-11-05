"""
Модель збереженого плану погашення боргів.
"""
from sqlalchemy import CheckConstraint, Column, String, Integer, Float, DateTime, JSON, ForeignKey
from sqlalchemy.sql import func
from app.models.database import Base


class MilpPlan(Base):
    """Збережений план погашення боргів.

    Кожен збережений план — чернетка (status='draft'); прийняття створює
    окрему версію в debt_plan_versions. plan_data містить результати всіх
    чотирьох стратегій. extra_monthly_uah, debts_fingerprint і
    debts_structure_fingerprint заповнює POST /debt/optimize.
    """
    __tablename__ = "milp_plans"
    __table_args__ = (
        CheckConstraint("status IN ('draft')", name="ck_milp_plans_status"),
    )

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    forecast_id = Column(String, ForeignKey("forecasts.id"), nullable=True, index=True)
    generated_at = Column(DateTime(timezone=True), server_default=func.now())
    months_to_payoff = Column(Integer, nullable=False)
    total_interest_paid = Column(Float, nullable=False)
    plan_data = Column(JSON, nullable=False)  # результати стратегій з debt_optimizer.py, зокрема monthly_log
    status = Column(String, nullable=False, default="draft", server_default="draft")  # 'draft'
    # NULL для планів, збережених без додаткового платежу й відбитка.
    extra_monthly_uah = Column(Float, nullable=True)  # гривні, як решта сум плану
    debts_fingerprint = Column(String(64), nullable=True)  # sha256 hex, app/services/debt_fingerprint.py
    debts_structure_fingerprint = Column(String(64), nullable=True)  # той самий відбиток без залишків
