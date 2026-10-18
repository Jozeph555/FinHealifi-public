"""
Модель користувача.
"""
from sqlalchemy import Column, String, DateTime
from sqlalchemy.sql import func
from app.models.database import Base


class User(Base):
    """Користувач застосунку; пароль зберігається як bcrypt-хеш."""
    __tablename__ = "users"

    id = Column(String, primary_key=True)  # uuid4, генерується в app/api/auth.py
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
