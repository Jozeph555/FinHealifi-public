"""
Залежності FastAPI: сесія БД і поточний користувач за JWT.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
 
from app.models.database import AsyncSessionLocal
from app.models.user import User
from app.core.security import decode_access_token
 
# HTTPBearer: /auth/login приймає JSON {"email", "password"}, а не форму
# OAuth2 password grant. У Swagger достатньо вставити отриманий токен.
bearer_scheme = HTTPBearer()
 
 
async def get_db():
    """Відкриває сесію БД на час запиту."""
    async with AsyncSessionLocal() as session:
        yield session
 
 
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Повертає користувача за Bearer-токеном.

    Raises:
        HTTPException: 401, якщо токен недійсний або користувача не існує.
    """
    token = credentials.credentials
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не вдалося підтвердити користувача",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        user_id = decode_access_token(token)
    except JWTError:
        raise credentials_exception
 
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
    return user
