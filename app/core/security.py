"""
Хешування паролів, JWT-токени та шифрування токенів Monobank.
"""
from datetime import datetime, timedelta, timezone

from passlib.context import CryptContext
from jose import jwt
from cryptography.fernet import Fernet

from app.core.config import (
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    FERNET_KEY,
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_fernet = Fernet(FERNET_KEY.encode())


# --- Паролі ---

def hash_password(password: str) -> str:
    """Повертає bcrypt-хеш пароля."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Перевіряє пароль за bcrypt-хешем."""
    return pwd_context.verify(plain_password, password_hash)


# --- JWT ---

def create_access_token(user_id: str) -> str:
    """Створює JWT з user_id у claim 'sub' і строком дії ACCESS_TOKEN_EXPIRE_MINUTES."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str:
    """Повертає user_id (claim 'sub') з токена.

    Raises:
        jose.JWTError: Токен недійсний, підроблений або протермінований.
    """
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    return payload["sub"]


# --- Шифрування Monobank-токена (Account.mono_token_encrypted) ---

def encrypt_token(raw_token: str) -> str:
    """Шифрує токен Monobank (Fernet)."""
    return _fernet.encrypt(raw_token.encode()).decode()


def decrypt_token(encrypted_token: str) -> str:
    """Розшифровує токен Monobank (Fernet)."""
    return _fernet.decrypt(encrypted_token.encode()).decode()
