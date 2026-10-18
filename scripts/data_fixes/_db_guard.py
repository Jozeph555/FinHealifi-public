"""
Перевірка цілі БД для скриптів роботи з даними.

Розбирає URL без з'єднання, тож викликається до відкриття першої сесії.
Правила:
- у URL має бути явний порт;
- порт має дорівнювати --expect-db-port;
- для основної БД (MAIN_DB_PORT) додатково потрібна змінна середовища
  ALLOW_MAIN_DB=1.
"""
from collections.abc import Mapping

from sqlalchemy.engine import URL, make_url

MAIN_DB_PORT = 5436


class DbGuardRefused(Exception):
    """Ціль БД не пройшла перевірку."""
    pass


def check_db_target(url: str | URL, expect_port: int, env: Mapping[str, str]) -> str:
    """Перевіряє ціль БД перед підключенням.

    Args:
        url: URL бази даних.
        expect_port: Очікуваний порт.
        env: Змінні середовища (для ALLOW_MAIN_DB).

    Returns:
        Рядок 'хост:порт/база' без пароля.

    Raises:
        DbGuardRefused: Порт не вказано, він не збігається з очікуваним, або для
            основної БД не задано ALLOW_MAIN_DB=1.
    """
    parsed = make_url(url) if isinstance(url, str) else url
    if parsed.port is None:
        raise DbGuardRefused(
            f"у DATABASE_URL немає явного порту (хост {parsed.host}, база {parsed.database}); "
            f"вкажіть порт явно"
        )
    target = f"{parsed.host}:{parsed.port}/{parsed.database}"
    if parsed.port != expect_port:
        raise DbGuardRefused(
            f"порт БД {parsed.port} ({target}) не збігається з --expect-db-port {expect_port}"
        )
    if parsed.port == MAIN_DB_PORT and env.get("ALLOW_MAIN_DB") != "1":
        raise DbGuardRefused(
            f"{target} — основна БД; потрібна змінна середовища ALLOW_MAIN_DB=1"
        )
    return target
