"""
Реєстрація, вхід і дані поточного користувача (JWT).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.core.security import hash_password, verify_password, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    """Тіло запиту реєстрації."""
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    """Тіло запиту входу: JSON з email і паролем."""
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """Відповідь з JWT-токеном доступу."""
    access_token: str
    token_type: str = "bearer"


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Реєструє користувача й повертає токен доступу.

    Пароль має містити щонайменше 8 символів. Повертає 400, якщо email уже
    зареєстровано або пароль закороткий.
    """
    existing = await db.execute(select(User).where(User.email == data.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="Користувач з таким email вже існує")

    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Пароль має містити щонайменше 8 символів")

    user = User(
        id=str(uuid.uuid4()),
        email=data.email,
        password_hash=hash_password(data.password),
    )
    db.add(user)
    await db.commit()

    token = create_access_token(user.id)
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Перевіряє email і пароль і повертає токен доступу.

    Повертає 401 з однаковим повідомленням для неіснуючого email і хибного пароля.
    """
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(data.password, user.password_hash):
        # Однакова помилка для неіснуючого email і хибного пароля не розкриває,
        # які email зареєстровані.
        raise HTTPException(status_code=401, detail="Невірний email або пароль")

    token = create_access_token(user.id)
    return TokenResponse(access_token=token)


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    """Повертає id і email поточного користувача."""
    return {"id": current_user.id, "email": current_user.email}
