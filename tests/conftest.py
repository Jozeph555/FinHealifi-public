"""
Фікстури для тестів проти справжньої Postgres — окремої тестової БД
(`docker-compose up -d db_test`, порт 5434, БД `finhealifi_test`), без моків.

Захист від запуску на робочій БД:
- підключення лише до TEST_DATABASE_URL (з оточення або `.env`); DATABASE_URL
  читається лише для порівняння;
- до імпорту `app.*`: ім'я БД має закінчуватися на `_test`, URL і ім'я БД
  мають відрізнятися від DATABASE_URL — інакше pytest.exit;
- `os.environ["DATABASE_URL"]` підміняється на тестовий URL до імпорту
  `app.models.database`, тож і код, що сам відкриває AsyncSessionLocal(),
  працює з тестовою БД;
- на старті сесії `current_database()` перевіряється на живому з'єднанні, а
  `alembic_version` звіряється з head.

Ізоляція: кожен тест отримує сесію на з'єднанні з відкритою транзакцією
(join_transaction_mode="create_savepoint"). `await db.commit()` у тестованому
коді лише звільняє SAVEPOINT; зовнішня транзакція відкочується після тесту.
"""
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV = dotenv_values(_REPO_ROOT / ".env")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _DOTENV.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    pytest.exit(
        "TEST_DATABASE_URL не задано (ні в оточенні, ні в .env). Додай у .env:\n"
        "  TEST_DATABASE_URL=postgresql://finhealifi_user:finhealifi_pass@localhost:5434/finhealifi_test\n"
        "і підніми тестову БД: docker-compose up -d db_test",
        returncode=2,
    )

TEST_DB_NAME = make_url(TEST_DATABASE_URL).database or ""
if not TEST_DB_NAME.endswith("_test"):
    pytest.exit(f"Ім'я тестової БД '{TEST_DB_NAME}' не закінчується на '_test' — відмова запускати тести.", returncode=2)

# DATABASE_URL — лише для порівняння: з .env і з оточення (змінна оточення
# має пріоритет над .env).
_DEV_URLS = {u for u in (_DOTENV.get("DATABASE_URL"), os.environ.get("DATABASE_URL")) if u}
for _dev_url in _DEV_URLS:
    if _dev_url == TEST_DATABASE_URL:
        pytest.exit("TEST_DATABASE_URL збігається з DATABASE_URL — відмова запускати тести.", returncode=2)
    if (make_url(_dev_url).database or "") == TEST_DB_NAME:
        pytest.exit(f"Тестова БД '{TEST_DB_NAME}' має те саме ім'я, що й dev-БД — відмова запускати тести.", returncode=2)
DEV_DB_NAMES = {make_url(u).database for u in _DEV_URLS}

os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import ProgrammingError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

import importlib  # noqa: E402
import pkgutil  # noqa: E402

import app.models  # noqa: E402
from app.models.database import engine  # noqa: E402

# Реєстрація всіх ORM-моделей, щоб FK (accounts.bank_id -> banks,
# transactions.loan_id -> loans) розв'язувалися і під час запуску одного файлу.
for _module in pkgutil.iter_modules(app.models.__path__):
    if _module.name not in ("database", "init_db"):
        importlib.import_module(f"app.models.{_module.name}")

from app.models.user import User  # noqa: E402
from app.models.account import Account  # noqa: E402


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _guard_test_database():
    """Перевіряє на живому з'єднанні, що БД тестова й схема на head."""
    async with engine.connect() as conn:
        current_db = (await conn.execute(text("SELECT current_database()"))).scalar_one()
        try:
            db_revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
        except ProgrammingError:
            db_revision = None

    if not current_db.endswith("_test") or current_db in DEV_DB_NAMES:
        pytest.exit(f"Підключено до '{current_db}', а не до тестової БД — відмова запускати тести.", returncode=2)

    head = ScriptDirectory.from_config(Config(str(_REPO_ROOT / "alembic.ini"))).get_current_head()
    if db_revision != head:
        pytest.exit(
            f"Схема тестової БД не на head (БД: {db_revision}, head: {head}). Виконай:\n"
            f"  DATABASE_URL={TEST_DATABASE_URL} alembic upgrade head",
            returncode=2,
        )
    yield


@pytest_asyncio.fixture
async def db_session():
    """Сесія БД, ізольована SAVEPOINT; усі зміни відкочуються після тесту."""
    connection = await engine.connect()
    outer_trans = await connection.begin()
    session = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()
        await outer_trans.rollback()
        await connection.close()


@pytest_asyncio.fixture
async def real_user(db_session):
    """Створює користувача в БД (для зовнішніх ключів user_id)."""
    user = User(
        id=f"test-{uuid.uuid4()}",
        email=f"amount-uah-{uuid.uuid4()}@example.com",
        password_hash="not-a-real-hash",
    )
    db_session.add(user)
    await db_session.flush()
    return user
