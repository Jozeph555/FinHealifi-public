"""
Тести видалення дублікатів між джерелами (dedup.remove_duplicate_transactions,
_bank_compatible) і його точок виклику: confirm_statement_import,
create_manual_transaction, run_sync_job, run_periodic_sync.

Використовуються сідові банки (privatbank, monobank).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from dateutil import parser as dateutil_parser

from app.models.account import Account
from app.models.transaction import Transaction
from app.services.dedup import remove_duplicate_transactions, _bank_compatible
from app.api import imports as imports_module
from app.api.imports import confirm_statement_import, ConfirmImportRequest
from app.api.transactions import create_manual_transaction, ManualTransactionRequest


async def _make_account(db_session, user_id, source_type, bank_id=None):
    account = Account(
        id=f"{source_type}-{uuid.uuid4()}",
        user_id=user_id,
        source_type=source_type,
        bank_id=bank_id,
    )
    db_session.add(account)
    await db_session.flush()
    return account


# Точність часу — як для джерела рахунку: monobank_api -> 'second',
# manual -> 'date', csv_import (XLSX з точним часом) -> 'second'.
# remove_duplicate_transactions порівнює точний time.
_PRECISION_BY_SOURCE = {"monobank_api": "second", "manual": "date", "csv_import": "second"}


async def _make_txn(db_session, user_id, account_id, time, amount, created_at=None):
    account = await db_session.get(Account, account_id)
    txn = Transaction(
        id=str(uuid.uuid4()),
        user_id=user_id,
        account_id=account_id,
        time=time,
        time_precision=_PRECISION_BY_SOURCE[account.source_type],
        amount=amount,
        amount_uah=amount // 100,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


# --- Ядро алгоритму ----------------------------------------------------------

async def test_exact_duplicate_same_bank_deletes_lower_priority(db_session, real_user):
    mono_acc = await _make_account(db_session, real_user.id, "monobank_api", "privatbank")
    csv_acc = await _make_account(db_session, real_user.id, "csv_import", "privatbank")
    mono_txn = await _make_txn(db_session, real_user.id, mono_acc.id, 1000, -5000)
    csv_txn = await _make_txn(db_session, real_user.id, csv_acc.id, 1000, -5000)
    await db_session.commit()

    result = await remove_duplicate_transactions(db_session, real_user.id)
    await db_session.commit()

    assert result.removed_count == 1
    assert csv_txn.id in result.removed_ids
    assert mono_txn.id not in result.removed_ids
    assert await db_session.get(Transaction, mono_txn.id) is not None
    assert await db_session.get(Transaction, csv_txn.id) is None


async def test_different_concrete_banks_not_merged(db_session, real_user):
    privat_acc = await _make_account(db_session, real_user.id, "csv_import", "privatbank")
    mono_acc = await _make_account(db_session, real_user.id, "monobank_api", "monobank")
    t1 = await _make_txn(db_session, real_user.id, privat_acc.id, 2000, -1000)
    t2 = await _make_txn(db_session, real_user.id, mono_acc.id, 2000, -1000)
    await db_session.commit()

    result = await remove_duplicate_transactions(db_session, real_user.id)
    await db_session.commit()

    assert result.removed_count == 0
    assert await db_session.get(Transaction, t1.id) is not None
    assert await db_session.get(Transaction, t2.id) is not None


async def test_manual_no_bank_merges_with_bank_sourced(db_session, real_user):
    manual_acc = await _make_account(db_session, real_user.id, "manual", None)
    csv_acc = await _make_account(db_session, real_user.id, "csv_import", "pumb")
    manual_txn = await _make_txn(db_session, real_user.id, manual_acc.id, 3000, -750)
    csv_txn = await _make_txn(db_session, real_user.id, csv_acc.id, 3000, -750)
    await db_session.commit()

    result = await remove_duplicate_transactions(db_session, real_user.id)
    await db_session.commit()

    assert result.removed_count == 1
    assert manual_txn.id in result.removed_ids
    assert await db_session.get(Transaction, csv_txn.id) is not None


async def test_same_source_type_tie_break_keeps_earlier_created(db_session, real_user):
    manual_acc = await _make_account(db_session, real_user.id, "manual", None)
    now = datetime.now(timezone.utc)
    older = await _make_txn(
        db_session, real_user.id, manual_acc.id, 4000, -200, created_at=now - timedelta(hours=1)
    )
    newer = await _make_txn(
        db_session, real_user.id, manual_acc.id, 4000, -200, created_at=now
    )
    await db_session.commit()

    result = await remove_duplicate_transactions(db_session, real_user.id)
    await db_session.commit()

    assert result.removed_count == 1
    assert newer.id in result.removed_ids
    assert await db_session.get(Transaction, older.id) is not None


async def test_three_way_transitivity_case(db_session, real_user):
    """Група з трьох рядків з однаковими time і amount: manual (без банку), privatbank і monobank.

    Лишається monobank (найвищий пріоритет джерела); manual видаляється
    (сумісний з будь-яким банком); privatbank лишається (інший конкретний банк).
    """
    manual_acc = await _make_account(db_session, real_user.id, "manual", None)
    privat_acc = await _make_account(db_session, real_user.id, "csv_import", "privatbank")
    mono_acc = await _make_account(db_session, real_user.id, "monobank_api", "monobank")

    manual_txn = await _make_txn(db_session, real_user.id, manual_acc.id, 5000, -333)
    privat_txn = await _make_txn(db_session, real_user.id, privat_acc.id, 5000, -333)
    mono_txn = await _make_txn(db_session, real_user.id, mono_acc.id, 5000, -333)
    await db_session.commit()

    result = await remove_duplicate_transactions(db_session, real_user.id)
    await db_session.commit()

    assert result.removed_count == 1
    assert manual_txn.id in result.removed_ids
    assert await db_session.get(Transaction, privat_txn.id) is not None
    assert await db_session.get(Transaction, mono_txn.id) is not None


async def test_no_false_positive_different_time_or_amount(db_session, real_user):
    acc = await _make_account(db_session, real_user.id, "csv_import", "privatbank")
    t1 = await _make_txn(db_session, real_user.id, acc.id, 6000, -100)
    t2 = await _make_txn(db_session, real_user.id, acc.id, 6001, -100)  # інший час
    t3 = await _make_txn(db_session, real_user.id, acc.id, 6000, -101)  # інша сума
    await db_session.commit()

    result = await remove_duplicate_transactions(db_session, real_user.id)
    await db_session.commit()

    assert result.removed_count == 0
    for txn in (t1, t2, t3):
        assert await db_session.get(Transaction, txn.id) is not None


async def test_unscoped_call_covers_all_users(db_session, real_user):
    other_user_id = f"test-{uuid.uuid4()}"
    from app.models.user import User

    other_user = User(id=other_user_id, email=f"{uuid.uuid4()}@example.com", password_hash="x")
    db_session.add(other_user)
    await db_session.flush()

    acc1 = await _make_account(db_session, real_user.id, "csv_import", "privatbank")
    acc1b = await _make_account(db_session, real_user.id, "monobank_api", "privatbank")
    acc2 = await _make_account(db_session, other_user_id, "csv_import", "privatbank")
    acc2b = await _make_account(db_session, other_user_id, "monobank_api", "privatbank")

    txn1a = await _make_txn(db_session, real_user.id, acc1.id, 7000, -50)
    txn1b = await _make_txn(db_session, real_user.id, acc1b.id, 7000, -50)
    txn2a = await _make_txn(db_session, other_user_id, acc2.id, 7000, -50)
    txn2b = await _make_txn(db_session, other_user_id, acc2b.id, 7000, -50)
    await db_session.commit()

    # Лише real_user — дублікат іншого користувача не змінюється
    result = await remove_duplicate_transactions(db_session, real_user.id)
    await db_session.commit()
    assert result.removed_count == 1
    assert txn1a.id in result.removed_ids
    assert await db_session.get(Transaction, txn2a.id) is not None
    assert await db_session.get(Transaction, txn2b.id) is not None

    # Прохід без user_id охоплює й іншого користувача
    result2 = await remove_duplicate_transactions(db_session, None)
    await db_session.commit()
    assert result2.removed_count == 1
    assert txn2a.id in result2.removed_ids


# --- _bank_compatible (чиста функція, без БД) --------------------------------
# bank_id для csv_import обирає користувач під час імпорту; для monobank_api
# він приходить з API банку.

def test_csv_import_both_sides_compatible_despite_different_bank_id():
    assert _bank_compatible("privatbank", "csv_import", "pravexbank", "csv_import") is True


def test_csv_import_both_sides_compatible_same_bank_id():
    assert _bank_compatible("privatbank", "csv_import", "privatbank", "csv_import") is True


def test_monobank_vs_csv_different_bank_still_incompatible():
    assert _bank_compatible("monobank", "monobank_api", "privatbank", "csv_import") is False


def test_monobank_vs_monobank_different_bank_still_incompatible():
    assert _bank_compatible("monobank", "monobank_api", "privatbank", "monobank_api") is False


def test_none_bank_id_always_compatible_regardless_of_source():
    assert _bank_compatible(None, "manual", "privatbank", "csv_import") is True
    assert _bank_compatible("privatbank", "csv_import", None, "manual") is True


def test_same_bank_id_compatible_regardless_of_source():
    assert _bank_compatible("privatbank", "csv_import", "privatbank", "monobank_api") is True


# --- Інтеграція: два csv_import-акаунти з різним bank_id ---------------------

async def test_two_csv_imports_different_bank_id_now_merge(db_session, real_user):
    """Повторний імпорт тієї самої виписки з іншим обраним банком зливається з першим імпортом."""
    first_acc = await _make_account(db_session, real_user.id, "csv_import", "privatbank")
    second_acc = await _make_account(db_session, real_user.id, "csv_import", "pravexbank")
    first_txn = await _make_txn(
        db_session, real_user.id, first_acc.id, 8000, -2050,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    second_txn = await _make_txn(db_session, real_user.id, second_acc.id, 8000, -2050)
    await db_session.commit()

    result = await remove_duplicate_transactions(db_session, real_user.id)
    await db_session.commit()

    assert result.removed_count == 1
    assert second_txn.id in result.removed_ids  # пізніший (по created_at) видаляється
    assert await db_session.get(Transaction, first_txn.id) is not None


# --- Інтеграція: confirm_statement_import -----------------------------------

@pytest.fixture(autouse=True)
def _skip_auto_categorization(monkeypatch):
    async def _noop_categorize():
        return {"skipped_for_test": True}

    monkeypatch.setattr(imports_module, "categorize_all_transactions", _noop_categorize)


async def test_confirm_statement_import_dedups_against_existing(db_session, real_user):
    existing_acc = await _make_account(db_session, real_user.id, "monobank_api", "privatbank")
    # Той самий розбір дати, що й у confirm (dateutil, локальний час без зони),
    # щоб timestamp збігся з рядком CSV нижче.
    existing_time = int(dateutil_parser.parse("01.03.2026", dayfirst=True).timestamp())
    await _make_txn(db_session, real_user.id, existing_acc.id, existing_time, -1000)
    await db_session.commit()

    import_id = str(uuid.uuid4())
    imports_module._pending_imports[import_id] = {
        "user_id": real_user.id,
        "file_bytes": (
            "дата,опис,сума,баланс\n01.03.2026,Тест,-10.00,1000.00\n"
        ).encode("utf-8"),
        "filename": "statement.csv",
    }

    result = await confirm_statement_import(
        ConfirmImportRequest(import_id=import_id, bank_id="privatbank"),
        current_user=real_user, db=db_session,
    )

    assert result["created"] == 1
    assert result["duplicates_removed"] == 1

    remaining = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == real_user.id, Transaction.time == existing_time
            )
        )
    ).scalars().all()
    assert len(remaining) == 1


# --- Інтеграція: create_manual_transaction (pre-check + force) -------------

async def test_manual_entry_possible_duplicate_blocks_without_force(db_session, real_user):
    first = await create_manual_transaction(
        ManualTransactionRequest(
            date="2026-03-05T10:00:00", description="Кава", amount=-65.0, category=None,
        ),
        current_user=real_user, db=db_session,
    )

    second = await create_manual_transaction(
        ManualTransactionRequest(
            date="2026-03-05T10:00:00", description="Кава (дубль)", amount=-65.0, category=None,
        ),
        current_user=real_user, db=db_session,
    )

    assert second.possible_duplicate is True
    assert second.id == first.id  # повертає наявну транзакцію, нічого не створено
    assert second.message is not None

    result = await db_session.execute(
        select(Transaction).where(
            Transaction.user_id == real_user.id, Transaction.time == first.time,
            Transaction.amount_uah == -65,
        )
    )
    assert len(result.scalars().all()) == 1  # друга не створена


async def test_manual_entry_force_creates_despite_duplicate(db_session, real_user):
    """Три ручні транзакції з однаковими часом і сумою (повернення часток спільного рахунку) створюються з force=True."""
    created = []
    for _ in range(3):
        txn = await create_manual_transaction(
            ManualTransactionRequest(
                date="2026-03-07T10:00:00", description="Від друга", amount=250.0,
                category=None, force=True,
            ),
            current_user=real_user, db=db_session,
        )
        created.append(txn)

    assert len(created) == 3
    assert len({t.id for t in created}) == 3  # три різні id, жоден не позначений possible_duplicate

    result = await db_session.execute(
        select(Transaction).where(
            Transaction.user_id == real_user.id, Transaction.amount_uah == 250,
        )
    )
    assert len(result.scalars().all()) == 3


async def test_manual_entry_no_duplicate_returns_normal_response(db_session, real_user):
    # Прямий виклик обходить response_model FastAPI, тож перевіряємо контракт
    # HTTP-відповіді: TransactionResponse.model_validate(orm_obj) підставляє
    # possible_duplicate=False і message=None за замовчуванням.
    from app.api.transactions import TransactionResponse

    response = await create_manual_transaction(
        ManualTransactionRequest(
            date="2026-03-06T10:00:00", description="Продукти", amount=-320.0, category=None,
        ),
        current_user=real_user, db=db_session,
    )
    wire_response = TransactionResponse.model_validate(response)
    assert wire_response.possible_duplicate is False
    assert wire_response.message is None
