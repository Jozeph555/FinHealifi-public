"""
Модель власної категорії витрат користувача.
"""
from sqlalchemy import Column, String, DateTime, ForeignKey, Index, func
from app.models.database import Base


class UserCategory(Base):
    """Власна категорія витрат користувача.

    Прогноз, радник, перевірка знаку й ML обробляють її як звичайну витрату.
    Назва унікальна в межах користувача без урахування регістру (user_id,
    lower(name)).
    """

    __tablename__ = "user_categories"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(40), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


Index(
    "uq_user_categories_user_id_lower_name",
    UserCategory.user_id,
    func.lower(UserCategory.name),
    unique=True,
)
