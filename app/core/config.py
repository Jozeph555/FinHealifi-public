"""
Налаштування застосунку зі змінних середовища (.env).

JWT_SECRET_KEY, FERNET_KEY і ANTHROPIC_API_KEY обов'язкові: без них імпорт
модуля завершується RuntimeError.
"""
import os
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY не встановлено в .env.\n"
        "Згенеруй один раз і додай у .env:\n"
        '  python -c "import secrets; print(secrets.token_hex(32))"'
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

FERNET_KEY = os.getenv("FERNET_KEY")
if not FERNET_KEY:
    raise RuntimeError(
        "FERNET_KEY не встановлено в .env (для шифрування Monobank-токена).\n"
        "Згенеруй один раз і додай у .env:\n"
        '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    )

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    raise RuntimeError(
        "ANTHROPIC_API_KEY не встановлено в .env.\n"
        "Візьми ключ на https://console.anthropic.com/settings/keys і додай:\n"
        "  ANTHROPIC_API_KEY=sk-ant-...\n"
        "у .env"
    )

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
