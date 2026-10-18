"""
Тести довідника банків: get_or_create_bank (app/services/banks.py),
GET /banks і обов'язковість bank_id або custom_bank_name в confirm_statement_import.

Сідові банки (privatbank, monobank) створює міграція 7b2adc1e0d59.
"""
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.bank import Bank
from app.services.banks import get_or_create_bank
from app.api.banks import list_banks
from app.api import imports as imports_module
from app.api.imports import confirm_statement_import, ConfirmImportRequest


# --- get_or_create_bank -----------------------------------------------------

async def test_get_or_create_bank_by_id_returns_seed_bank(db_session):
    bank = await get_or_create_bank(db_session, "privatbank", None, None)
    assert bank.id == "privatbank"
    assert bank.mfo == "305299"
    assert bank.is_seed is True


async def test_get_or_create_bank_unknown_id_raises(db_session):
    with pytest.raises(ValueError):
        await get_or_create_bank(db_session, "no-such-bank", None, None)


async def test_get_or_create_bank_neither_arg_raises(db_session):
    with pytest.raises(ValueError):
        await get_or_create_bank(db_session, None, None, None)


async def test_get_or_create_bank_custom_creates_new_row(db_session):
    unique_name = f"Тестовий Банк {uuid.uuid4().hex[:8]}"
    bank = await get_or_create_bank(db_session, None, unique_name, "999999")
    assert bank.id.startswith("custom-")
    assert bank.name == unique_name
    assert bank.mfo == "999999"
    assert bank.is_seed is False


async def test_get_or_create_bank_custom_reuses_existing_mfo(db_session):
    # Другий виклик з іншою назвою, але тим самим МФО повертає той самий
    # запис.
    first = await get_or_create_bank(db_session, None, "Мій Банк написання А", "888888")
    second = await get_or_create_bank(db_session, None, "мій банк написання Б", "888888")
    assert first.id == second.id


async def test_get_or_create_bank_custom_no_mfo_matches_by_normalized_name(db_session):
    name = f"Без Мфо Банк {uuid.uuid4().hex[:8]}"
    first = await get_or_create_bank(db_session, None, name, None)
    second = await get_or_create_bank(db_session, None, f"  {name.upper()}  ", None)
    assert first.id == second.id


# --- GET /banks --------------------------------------------------------------

async def test_list_banks_includes_seed_top20(db_session, real_user):
    result = await list_banks(current_user=real_user, db=db_session)
    ids = {b["id"] for b in result}
    assert "privatbank" in ids
    assert "monobank" in ids
    assert len(ids) >= 20

    monobank_entry = next(b for b in result if b["id"] == "monobank")
    assert monobank_entry["name"] == "monobank"
    assert monobank_entry["mfo"] == "322001"


async def test_list_banks_includes_custom_banks(db_session, real_user):
    unique_name = f"Список Банк {uuid.uuid4().hex[:8]}"
    await get_or_create_bank(db_session, None, unique_name, None)
    await db_session.flush()

    result = await list_banks(current_user=real_user, db=db_session)
    names = {b["name"] for b in result}
    assert unique_name in names


# --- confirm_statement_import вимагає bank_id/custom_bank_name --------------

@pytest.fixture(autouse=True)
def _skip_auto_categorization(monkeypatch):
    async def _noop_categorize():
        return {"skipped_for_test": True}

    monkeypatch.setattr(imports_module, "categorize_all_transactions", _noop_categorize)


def _pending_csv(real_user_id: str) -> str:
    import_id = str(uuid.uuid4())
    imports_module._pending_imports[import_id] = {
        "user_id": real_user_id,
        "file_bytes": (
            "дата,опис,сума,баланс\n01.03.2026,Тест,-10.00,1000.00\n"
        ).encode("utf-8"),
        "filename": "statement.csv",
    }
    return import_id


async def test_confirm_without_bank_info_raises_422(db_session, real_user):
    import_id = _pending_csv(real_user.id)
    with pytest.raises(HTTPException) as exc_info:
        await confirm_statement_import(
            ConfirmImportRequest(import_id=import_id),
            current_user=real_user, db=db_session,
        )
    assert exc_info.value.status_code == 422


async def test_confirm_with_bank_id_sets_account_bank_fields(db_session, real_user):
    import_id = _pending_csv(real_user.id)
    result = await confirm_statement_import(
        ConfirmImportRequest(import_id=import_id, bank_id="privatbank"),
        current_user=real_user, db=db_session,
    )
    from app.models.account import Account

    account = await db_session.get(Account, result["account_id"])
    assert account.bank_id == "privatbank"
    assert account.bank_name == "ПриватБанк"


async def test_confirm_with_custom_bank_creates_bank_row(db_session, real_user):
    import_id = _pending_csv(real_user.id)
    unique_name = f"Виписка Банк {uuid.uuid4().hex[:8]}"
    result = await confirm_statement_import(
        ConfirmImportRequest(
            import_id=import_id, custom_bank_name=unique_name, custom_bank_mfo="777777",
        ),
        current_user=real_user, db=db_session,
    )
    from app.models.account import Account

    account = await db_session.get(Account, result["account_id"])
    bank = await db_session.get(Bank, account.bank_id)
    assert bank.name == unique_name
    assert bank.mfo == "777777"
    assert bank.is_seed is False
