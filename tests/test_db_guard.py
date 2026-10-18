"""
Тести перевірки цілі БД (scripts/data_fixes/_db_guard.py); без підключення до БД.
"""
import pytest

from scripts.data_fixes import dedup_apply as apply_script
from scripts.data_fixes import dedup_restore as restore_script
from scripts.data_fixes._db_guard import MAIN_DB_PORT, DbGuardRefused, check_db_target

PG = "postgresql+asyncpg://u:secret@localhost:{port}/{db}"


def test_port_mismatch_refused():
    with pytest.raises(DbGuardRefused, match="не збігається"):
        check_db_target(PG.format(port=5438, db="finhealifi_snapshot"), 5437, {})


def test_main_db_without_allow_refused():
    with pytest.raises(DbGuardRefused, match="ALLOW_MAIN_DB"):
        check_db_target(PG.format(port=MAIN_DB_PORT, db="finhealifi"), MAIN_DB_PORT, {})


def test_main_db_allow_must_be_exactly_1():
    with pytest.raises(DbGuardRefused, match="ALLOW_MAIN_DB"):
        check_db_target(PG.format(port=MAIN_DB_PORT, db="finhealifi"), MAIN_DB_PORT, {"ALLOW_MAIN_DB": "yes"})


def test_main_db_with_allow_and_matching_port_ok():
    target = check_db_target(PG.format(port=MAIN_DB_PORT, db="finhealifi"), MAIN_DB_PORT, {"ALLOW_MAIN_DB": "1"})
    assert target == "localhost:5436/finhealifi"


def test_non_main_matching_port_ok_without_password():
    target = check_db_target(PG.format(port=5438, db="finhealifi_snapshot"), 5438, {})
    assert target == "localhost:5438/finhealifi_snapshot"
    assert "secret" not in target


def test_url_without_port_refused():
    with pytest.raises(DbGuardRefused, match="немає явного порту"):
        check_db_target("postgresql+asyncpg://u:secret@localhost/finhealifi", 5436, {"ALLOW_MAIN_DB": "1"})


@pytest.mark.parametrize("script, args", [
    (apply_script, ("some-user", False, None)),
    (restore_script, ("some-run", False, False)),
])
async def test_script_refuses_before_opening_session(script, args, monkeypatch, capsys):
    # Під pytest рушій підключений до db_test (5434), тож очікується інший порт.
    def _no_session(*a, **kw):
        raise AssertionError("сесію відкрито попри відмову db_guard")
    monkeypatch.setattr(script, "AsyncSessionLocal", _no_session)
    assert await script.main(*args, expect_db_port=5437) == 2
    out = capsys.readouterr().out
    assert "не збігається" in out
    assert script.engine.url.password not in out
