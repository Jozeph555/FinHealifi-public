"""
Тести інваріанту amount_uah == amount // 100 (amount — цілі копійки) для трьох
шляхів введення транзакцій: синхронізація Monobank (app/services/sync.py),
імпорт виписки (app/api/imports.py) і ручне введення (app/api/transactions.py).

Тести викликають ті самі функції, що й робочий код (_save_transactions,
create_manual_transaction, confirm_statement_import).
"""
import uuid

import pytest
from sqlalchemy import select

from app.models.account import Account
from app.models.transaction import Transaction
from app.services.sync import _save_transactions
from app.api.transactions import create_manual_transaction, ManualTransactionRequest
from app.api import imports as imports_module
from app.api.imports import confirm_statement_import, ConfirmImportRequest


# Типові суми в гривнях, зокрема з копійками.
REAL_AMOUNTS_UAH = [-75.50, 32000.00, -1243.99, -450.50, -8.33]


def assert_amount_uah_invariant(transaction: Transaction):
    assert transaction.amount_uah == transaction.amount // 100, (
        f"amount_uah={transaction.amount_uah} не збігається з "
        f"amount // 100 (={transaction.amount // 100}) для amount={transaction.amount} "
        f"(транзакція {transaction.id})"
    )


# --- Синхронізація Monobank (app/services/sync.py) ------------------------

@pytest.mark.parametrize("amount_uah", REAL_AMOUNTS_UAH)
async def test_sync_amount_uah_invariant(db_session, real_user, amount_uah):
    account = Account(id=f"mono-{uuid.uuid4()}", user_id=real_user.id, source_type="monobank_api")
    db_session.add(account)
    await db_session.flush()

    # Формат елементів виписки Monobank API (суми в копійках).
    kopecks = int(round(amount_uah * 100))
    txn_id = str(uuid.uuid4())
    monobank_item = {
        "id": txn_id,
        "time": 1772550000,
        "description": "Тестова транзакція Monobank",
        "mcc": 5411,
        "amount": kopecks,
        "currencyCode": 980,
        "cashbackAmount": 0,
        "balance": 1000000,
        "hold": False,
    }

    await _save_transactions(db_session, [monobank_item], account.id, real_user.id)
    await db_session.commit()

    saved = await db_session.get(Transaction, txn_id)
    assert saved is not None
    assert saved.amount == kopecks
    assert_amount_uah_invariant(saved)


# --- Ручний ввід (app/api/transactions.py) ----------------------------------

@pytest.mark.parametrize("amount_uah", REAL_AMOUNTS_UAH)
async def test_manual_entry_amount_uah_invariant(db_session, real_user, amount_uah):
    request = ManualTransactionRequest(
        date="2026-03-01T12:00:00",
        description="Тестова ручна транзакція",
        amount=amount_uah,
        category=None,
    )

    saved = await create_manual_transaction(request, current_user=real_user, db=db_session)

    assert saved.amount == int(amount_uah * 100)
    assert_amount_uah_invariant(saved)


# --- CSV/XLSX import (app/api/imports.py) -----------------------------------

def _build_statement_csv() -> bytes:
    # Заголовки виписки ПриватБанку: "дата", "опис", "сума", "баланс" — точні
    # синоніми з CANONICAL_FIELDS, тож мапінг колонок однозначний.
    lines = [
        "дата,опис,сума,баланс",
        "01.03.2026,Кав'ярня Аромакава,-75.50,12000.00",
        "02.03.2026,Зарплата,32000.00,44000.00",
        "03.03.2026,АТБ Маркет,-1243.99,42756.01",
        "04.03.2026,Погашення частини кредиту,-450.50,42305.51",
        "05.03.2026,Парковка,-8.33,42297.18",
    ]
    return ("\n".join(lines)).encode("utf-8")


@pytest.fixture(autouse=True)
def _skip_auto_categorization(monkeypatch):
    # confirm_statement_import викликає categorize_all_transactions(), яка
    # відкриває власну сесію поза SAVEPOINT тесту. Категоризація тут не
    # перевіряється, тож її замінено заглушкою.
    async def _noop_categorize():
        return {"skipped_for_test": True}

    monkeypatch.setattr(imports_module, "categorize_all_transactions", _noop_categorize)


async def test_csv_import_amount_uah_invariant(db_session, real_user):
    import_id = str(uuid.uuid4())
    imports_module._pending_imports[import_id] = {
        "user_id": real_user.id,
        "file_bytes": _build_statement_csv(),
        "filename": "statement.csv",
    }

    request = ConfirmImportRequest(import_id=import_id, bank_id="privatbank")
    result = await confirm_statement_import(request, current_user=real_user, db=db_session)

    assert result["created"] == len(REAL_AMOUNTS_UAH)
    assert result["skipped"] == 0

    rows = (
        await db_session.execute(
            select(Transaction).where(Transaction.account_id == result["account_id"])
        )
    ).scalars().all()
    assert len(rows) == len(REAL_AMOUNTS_UAH)

    saved_uah = sorted(t.amount / 100 for t in rows)
    assert saved_uah == sorted(REAL_AMOUNTS_UAH)

    for txn in rows:
        assert_amount_uah_invariant(txn)


# --- Три шляхи одночасно: та сама сума мусить дати той самий amount_uah ----

async def test_all_three_paths_agree_on_same_amount(db_session, real_user):
    """Та сама сума (-1243.99 грн) через усі три шляхи дає однаковий amount_uah."""
    amount_uah = -1243.99
    kopecks = int(round(amount_uah * 100))

    # Синхронізація Monobank
    account = Account(id=f"mono-{uuid.uuid4()}", user_id=real_user.id, source_type="monobank_api")
    db_session.add(account)
    await db_session.flush()
    mono_id = str(uuid.uuid4())
    await _save_transactions(
        db_session,
        [{
            "id": mono_id, "time": 1772550000, "description": "x", "mcc": 5411,
            "amount": kopecks, "currencyCode": 980, "cashbackAmount": 0,
            "balance": 1000000, "hold": False,
        }],
        account.id, real_user.id,
    )
    await db_session.commit()
    mono_txn = await db_session.get(Transaction, mono_id)

    # Ручний ввід
    manual_txn = await create_manual_transaction(
        ManualTransactionRequest(
            date="2026-03-01T12:00:00", description="x", amount=amount_uah, category=None,
        ),
        current_user=real_user, db=db_session,
    )

    # CSV import
    import_id = str(uuid.uuid4())
    imports_module._pending_imports[import_id] = {
        "user_id": real_user.id,
        "file_bytes": ("дата,опис,сума,баланс\n01.03.2026,x,-1243.99,1000.00\n").encode("utf-8"),
        "filename": "statement.csv",
    }
    csv_result = await confirm_statement_import(
        ConfirmImportRequest(import_id=import_id, bank_id="privatbank"),
        current_user=real_user, db=db_session,
    )
    csv_txn = (
        await db_session.execute(
            select(Transaction).where(Transaction.account_id == csv_result["account_id"])
        )
    ).scalars().one()

    assert mono_txn.amount == manual_txn.amount == csv_txn.amount == kopecks
    assert mono_txn.amount_uah == manual_txn.amount_uah == csv_txn.amount_uah, (
        f"Три шляхи розійшлись на однаковій сумі {amount_uah} грн: "
        f"sync={mono_txn.amount_uah}, manual={manual_txn.amount_uah}, csv={csv_txn.amount_uah} "
        f"(очікується {kopecks // 100})"
    )
