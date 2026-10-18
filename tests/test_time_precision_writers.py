"""
Тести значення transactions.time_precision, яке пише кожен шлях вставки.

- imports.py: CSV/XLSX — за всією колонкою дати файлу; PDF — завжди 'date';
- transactions.py (ручне введення) — завжди 'date', навіть якщо в запиті є час;
- sync.py (Monobank) — 'second'.
"""
import uuid
from datetime import datetime, timezone
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import pytest
from sqlalchemy import select

from app.api import imports as imports_module
from app.api.imports import ConfirmImportRequest, _infer_column_time_precision, confirm_statement_import
from app.api.transactions import ManualTransactionRequest, create_manual_transaction
from app.models.account import Account
from app.models.transaction import Transaction
from app.services.sync import _save_transactions


# --- _infer_column_time_precision -------------------------------------------

@pytest.mark.parametrize(
    "dates, expected",
    [
        ([datetime(2026, 3, 1), datetime(2026, 3, 2)], "date"),
        ([datetime(2026, 3, 1, 14, 5), datetime(2026, 3, 2)], "minute"),          # не всі північ
        ([datetime(2026, 3, 1, 14, 5, 33), datetime(2026, 3, 2, 9, 0)], "second"),
        ([datetime(2026, 3, 1), datetime(2026, 3, 2, 9, 0, 1)], "second"),        # змішана: північ + секунди
        ([datetime(2026, 3, 1), datetime(2026, 3, 2, 9, 0)], "minute"),           # змішана: північ + хвилини
    ],
)
def test_infer_column_time_precision(dates, expected):
    assert _infer_column_time_precision(dates) == expected


def test_infer_column_time_precision_empty_list_is_date():
    # Порожня колонка (жодна дата не розібралася): all([]) == True -> 'date'.
    # Без розібраних дат рядки не вставляються, тож значення не використовується.
    assert _infer_column_time_precision([]) == "date"


# --- confirm_statement_import ------------------------------------------------

async def _confirm(db_session, user, file_bytes: bytes, filename: str):
    import_id = str(uuid.uuid4())
    imports_module._pending_imports[import_id] = {
        "user_id": user.id, "file_bytes": file_bytes, "filename": filename,
    }
    result = await confirm_statement_import(
        ConfirmImportRequest(import_id=import_id, bank_id="privatbank"),
        current_user=user, db=db_session,
    )
    rows = (await db_session.execute(
        select(Transaction.time_precision).where(Transaction.user_id == user.id)
    )).scalars().all()
    return result, rows


def _csv(*dates: str) -> bytes:
    lines = ["дата,опис,сума,баланс"]
    for i, d in enumerate(dates):
        lines.append(f"{d},Операція {i},-{10 + i}.00,{1000 - i}.00")
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.mark.parametrize(
    "dates, expected",
    [
        (("01.03.2026", "02.03.2026", "03.03.2026"), "date"),
        (("01.03.2026 14:05", "02.03.2026 09:30", "03.03.2026"), "minute"),
        (("01.03.2026 14:05:33", "02.03.2026 09:30", "03.03.2026"), "second"),
    ],
)
async def test_csv_import_writes_column_precision(db_session, real_user, dates, expected):
    result, rows = await _confirm(db_session, real_user, _csv(*dates), "statement.csv")

    assert result["created"] == 3
    assert rows == [expected] * 3


async def test_xlsx_import_writes_column_precision(db_session, real_user):
    # Текстові дати "dd.mm.yyyy HH:MM:SS" — як у виписці ПриватБанку.
    buf = BytesIO()
    pd.DataFrame({
        "Дата": ["01.03.2026 14:05:33", "02.03.2026 09:30:00"],
        "Опис": ["Операція 1", "Операція 2"],
        "Сума": ["-10.00", "-20.00"],
        "Баланс": ["1000.00", "980.00"],
    }).to_excel(buf, index=False)

    result, rows = await _confirm(db_session, real_user, buf.getvalue(), "statement.xlsx")

    assert result["created"] == 2
    assert rows == ["second", "second"]


async def test_bad_date_row_is_skipped_and_does_not_affect_precision(db_session, real_user):
    # 4 коректні рядки на 1 некоректний: з меншою часткою коректних колонка
    # дати не визначиться (422) ще до вставки.
    data = _csv("01.03.2026 14:05", "02.03.2026 10:15", "не дата", "03.03.2026 09:30", "04.03.2026 18:45")

    result, rows = await _confirm(db_session, real_user, data, "statement.csv")

    assert result["created"] == 4
    assert result["skipped"] == 1
    assert rows == ["minute"] * 4


async def test_pdf_import_writes_date_for_all_rows(db_session, real_user):
    # Розбір блоків PDF: час HH:MM у файлі є, але в імпорт не передається — усі рядки 'date'.
    text = "\n".join([
        "Дата Сума Сума Залишок після",
        "Рахунок Деталі операції валюті валюті",
        "операції комісій знижок операції",
        "",
        "Перекази, УКРАЇНА, термінал ABC123, документ №",
        "01.03.2026 123456******7890 Оплата ТОВ Ромашка -500,00",
        "12:00 Угода № TEST0001 отримувач ТОВ Ромашка UAH -500,00 5,00 0,00 12000,00",
        "",
        "02.03.2026 123456******7890 Поповнення рахунку 20000,00",
        "09:30 Угода № TEST0002 UAH 20000,00 0,00 0,00 32000,00",
    ])
    buf = BytesIO()
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.axis("off")
    ax.text(0.02, 0.98, text, transform=ax.transAxes, va="top", fontsize=9, family="monospace")
    fig.savefig(buf, format="pdf", bbox_inches="tight")
    plt.close(fig)

    result, rows = await _confirm(db_session, real_user, buf.getvalue(), "statement.pdf")

    assert result["created"] == 2
    assert rows == ["date", "date"]


# --- Ручний ввід і Monobank sync -----------------------------------------------

async def test_manual_entry_with_full_datetime_writes_date(db_session, real_user):
    await create_manual_transaction(
        ManualTransactionRequest(
            date=datetime(2026, 3, 1, 14, 5, 33), description="Кава", amount=-100, category=None,
        ),
        current_user=real_user, db=db_session,
    )

    rows = (await db_session.execute(
        select(Transaction.time_precision).where(Transaction.user_id == real_user.id)
    )).scalars().all()
    assert rows == ["date"]


async def test_monobank_sync_save_writes_second(db_session, real_user):
    account = Account(id=f"mono-{uuid.uuid4()}", user_id=real_user.id, source_type="monobank_api")
    db_session.add(account)
    await db_session.flush()

    await _save_transactions(db_session, [{
        "id": f"mono-txn-{uuid.uuid4()}",
        "time": int(datetime(2026, 3, 1, 12, 0, 1, tzinfo=timezone.utc).timestamp()),
        "amount": -10000,
        "description": "Кава",
        "mcc": 5814,
        "currencyCode": 980,
    }], account.id, real_user.id)

    rows = (await db_session.execute(
        select(Transaction.time_precision).where(Transaction.account_id == account.id)
    )).scalars().all()
    assert rows == ["second"]
