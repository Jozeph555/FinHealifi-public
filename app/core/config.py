"""
Налаштування застосунку зі змінних середовища (.env).

JWT_SECRET_KEY, FERNET_KEY і ANTHROPIC_API_KEY обов'язкові: без них імпорт
модуля завершується RuntimeError. APP_TODAY_OVERRIDE (YYYY-MM-DD, необов'язкова)
підміняє «сьогодні» для плану й план-факту; неправильний формат — RuntimeError.
"""
import os
from datetime import date, datetime

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

# Ліміт часу MILP-розв'язувача CBC, секунди.
MILP_TIME_LIMIT_SEC = float(os.getenv("MILP_TIME_LIMIT_SEC", "10"))

# Змінна середовища з демонстраційною датою «сьогодні» (YYYY-MM-DD); через API не змінюється.
APP_TODAY_OVERRIDE_ENV = "APP_TODAY_OVERRIDE"


def today_override() -> date | None:
    """Повертає дату з APP_TODAY_OVERRIDE або None, якщо змінну не задано чи вона порожня.

    Змінна читається з оточення під час кожного виклику.

    Raises:
        RuntimeError: значення не у форматі YYYY-MM-DD.
    """
    raw = (os.getenv(APP_TODAY_OVERRIDE_ENV) or "").strip()
    if not raw:
        return None
    try:
        if len(raw) != 10:
            raise ValueError(raw)
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise RuntimeError(f"{APP_TODAY_OVERRIDE_ENV}={raw!r}: очікується дата у форматі YYYY-MM-DD") from exc


today_override()
