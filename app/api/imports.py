"""
Імпорт банківських виписок у два кроки.

1. POST /import/statement/preview — розбирає файл і повертає визначений мапінг
   колонок, перші рядки й перевірку залишку; у БД нічого не зберігається.
2. POST /import/statement/confirm — зберігає транзакції з урахуванням ручних
   виправлень мапінгу.

Вміст файлу між кроками зберігається в пам'яті процесу і не переживає
перезапуск сервера.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from dateutil import parser as dateutil_parser

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.account import Account
from app.models.transaction import Transaction
from app.services.statement_importer import (
    parse_statement,
    read_statement_file,
    find_header_row,
    normalize_category,
    _clean_number,
)
from app.services.categorization import categorize_all_transactions
from app.services.mcc_mapping import guard_positive_amount_category
from app.services.banks import get_or_create_bank
from app.services.dedup import remove_duplicate_transactions

router = APIRouter(prefix="/import", tags=["import"])

# Файли між preview і confirm: import_id -> вміст і власник.
_pending_imports: dict[str, dict] = {}


class ColumnMappingOverride(BaseModel):
    """Ручне виправлення мапінгу: канонічне поле -> назва колонки файлу."""
    date: str | None = None
    description: str | None = None
    category: str | None = None
    amount_signed: str | None = None
    amount_expense: str | None = None
    amount_income: str | None = None
    balance: str | None = None


class ConfirmImportRequest(BaseModel):
    """Тіло запиту підтвердження імпорту: банк (з довідника або власний) і виправлення мапінгу."""
    import_id: str
    bank_id: str | None = None  # з GET /banks
    custom_bank_name: str | None = None  # банк поза довідником, разом з custom_bank_mfo
    custom_bank_mfo: str | None = None
    mapping_override: ColumnMappingOverride | None = None


@router.post("/statement/preview")
async def preview_statement_import(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Розбирає файл виписки й повертає попередній перегляд разом з import_id.

    Повертає 422, якщо файл не вдалося розібрати.
    """
    file_bytes = await file.read()
    try:
        result = parse_statement(file_bytes, file.filename)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    import_id = str(uuid.uuid4())
    _pending_imports[import_id] = {
        "user_id": current_user.id,
        "file_bytes": file_bytes,
        "filename": file.filename,
    }

    return {"import_id": import_id, **result}


def _infer_column_time_precision(dates: list[datetime]) -> str:
    """Визначає точність колонки дати за всіма значеннями файлу.

    Перевіряється локальний час до перетворення в Unix-час: усі значення о
    00:00:00 — 'date', усі з нульовими секундами — 'minute', інакше 'second'.
    """
    if all((d.hour, d.minute, d.second, d.microsecond) == (0, 0, 0, 0) for d in dates):
        return "date"
    if all(d.second == 0 and d.microsecond == 0 for d in dates):
        return "minute"
    return "second"


@router.post("/statement/confirm")
async def confirm_statement_import(
    request: ConfirmImportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Зберігає транзакції з файлу, підготовленого на кроці preview.

    Створює рахунок з source_type='csv_import', вставляє транзакції, видаляє
    дублікати між джерелами й запускає категоризацію. Потрібен bank_id або
    custom_bank_name. Повертає 404, якщо import_id не знайдено, 403 — якщо він
    чужий, 422 — якщо мапінг чи дані непридатні.

    Returns:
        account_id, кількості створених, пропущених і видалених дублікатів,
        статистика категоризації.
    """
    cached = _pending_imports.get(request.import_id)
    if cached is None:
        raise HTTPException(
            status_code=404,
            detail="Import не знайдено або застарів. Завантаж файл повторно через /statement/preview",
        )
    if cached["user_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="Цей import належить іншому користувачу")

    raw_df = read_statement_file(cached["file_bytes"], cached["filename"])
    header_row = find_header_row(raw_df)
    headers = raw_df.iloc[header_row].astype(str).str.lower().str.strip().tolist()
    data = raw_df.iloc[header_row + 1:].reset_index(drop=True)

    # Мапінг за назвами колонок з ручними виправленнями поверх визначеного.
    from app.services.statement_importer import map_columns

    auto_mapping = map_columns(raw_df, header_row)  # {field: col_idx}
    mapping = dict(auto_mapping)

    if request.mapping_override:
        override_dict = request.mapping_override.model_dump(exclude_none=True)
        for field, column_name in override_dict.items():
            try:
                col_idx = headers.index(column_name.lower().strip())
                mapping[field] = col_idx
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"Колонка '{column_name}' для поля '{field}' не знайдена у файлі",
                )

    if "date" not in mapping:
        raise HTTPException(status_code=422, detail="Колонку дати не визначено — імпорт неможливий")
    has_single_amount = "amount_signed" in mapping
    has_split_amount = "amount_expense" in mapping and "amount_income" in mapping
    if not has_single_amount and not has_split_amount:
        raise HTTPException(
            status_code=422,
            detail="Колонку суми не визначено (ні єдину, ні пару дебет/кредит) — імпорт неможливий",
        )

    if not request.bank_id and not request.custom_bank_name:
        raise HTTPException(
            status_code=422,
            detail="Потрібно обрати банк (bank_id) або вказати власний (custom_bank_name)",
        )
    try:
        bank = await get_or_create_bank(
            db, request.bank_id, request.custom_bank_name, request.custom_bank_mfo
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    account_id = f"csv-{uuid.uuid4()}"
    account = Account(
        id=account_id,
        user_id=current_user.id,
        source_type="csv_import",
        bank_name=bank.name,
        bank_id=bank.id,
    )
    db.add(account)

    created_count = 0
    skipped_count = 0

    # Дати розбираються окремим проходом до вставки: точність часу визначається
    # по всій колонці. Для PDF точність — завжди 'date'.
    parsed_dates = []
    for _, row in data.iterrows():
        try:
            parsed_dates.append(dateutil_parser.parse(str(row.iloc[mapping["date"]]), dayfirst=True))
        except (ValueError, TypeError):
            parsed_dates.append(None)
    if cached["filename"].lower().endswith(".pdf"):
        time_precision = "date"
    else:
        time_precision = _infer_column_time_precision([d for d in parsed_dates if d is not None])

    for (_, row), date_value in zip(data.iterrows(), parsed_dates):
        if date_value is None:
            skipped_count += 1
            continue

        if has_single_amount:
            try:
                amount = _clean_number(row.iloc[mapping["amount_signed"]])
            except (ValueError, TypeError):
                skipped_count += 1
                continue
        else:
            try:
                income = _clean_number(row.iloc[mapping["amount_income"]]) if not pd_isna(row.iloc[mapping["amount_income"]]) else 0.0
                expense = _clean_number(row.iloc[mapping["amount_expense"]]) if not pd_isna(row.iloc[mapping["amount_expense"]]) else 0.0
                amount = income - expense
            except (ValueError, TypeError):
                skipped_count += 1
                continue

        description = (
            str(row.iloc[mapping["description"]]) if "description" in mapping else None
        )

        amount_kopecks = int(round(amount * 100))

        category = None
        category_method = None
        if "category" in mapping:
            bank_category_raw = str(row.iloc[mapping["category"]])
            normalized = normalize_category(bank_category_raw)
            # Категорія витрат банку для додатної суми не застосовується; транзакція
            # проходить звичайну категоризацію за описом.
            if normalized and guard_positive_amount_category(amount_kopecks, normalized) != "pending":
                category = normalized
                category_method = "bank_provided"

        transaction = Transaction(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            account_id=account_id,
            time=int(date_value.timestamp()),
            time_precision=time_precision,
            description=description,
            mcc=None,
            amount=amount_kopecks,  # у копійках
            # amount_uah = amount // 100, як у sync.py і transactions.py: int() округлює
            # від'ємні дробові суми до нуля, а не вниз.
            amount_uah=amount_kopecks // 100,
            currency_code=980,  # UAH за ISO 4217
            cashback_amount=0,
            balance=None,
            hold=False,
            category=category,
            category_method=category_method,
        )
        db.add(transaction)
        created_count += 1

    await db.commit()
    del _pending_imports[request.import_id]

    # Видалення дублікатів з уже наявними транзакціями користувача.
    dedup_result = await remove_duplicate_transactions(db, current_user.id)
    await db.commit()

    # Правилова категоризація нових транзакцій.
    categorization_stats = await categorize_all_transactions()

    return {
        "account_id": account_id,
        "created": created_count,
        "skipped": skipped_count,
        "duplicates_removed": dedup_result.removed_count,
        "categorization": categorization_stats,
    }


def pd_isna(value) -> bool:
    """Перевіряє значення на NaN/None через pandas."""
    import pandas as pd
    return pd.isna(value)
