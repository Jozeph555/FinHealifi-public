"""
Транзакції: ручне введення, список з фільтрами, категорії та ручні рішення щодо транзакцій.

Ручні транзакції прив'язуються до рахунку типу 'manual' (один на користувача,
створюється під час першого введення). Для невизначених надходжень є три
способи розв'язання: категорія (PATCH), повернення позики (repay-loan) і
повернення частки спільної витрати (mark-reimbursement).
"""
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.account import Account
from app.models.transaction import Transaction
from app.models.loan import Loan
from app.models.user_category import UserCategory
from app.services.forecasting import EXPENSE_EXCLUDED_CATEGORIES
from app.services.mcc_mapping import (
    INCOME_CATEGORIES, CASH_CATEGORY, TRANSFER_CATEGORIES, SERVICE_CATEGORIES, validate_manual_category_sign,
    LOAN_RECEIVED_CATEGORY, OWN_ACCOUNTS_TRANSFER_CATEGORY,
    CATEGORY_MERGE, KEYWORD_CATEGORIES, MCC_CATEGORIES,
)
from app.services.categorization import record_training_example
from app.services.dedup import find_possible_duplicate

router = APIRouter(prefix="/transactions", tags=["transactions"])

# Межі from/to у GET /transactions — календарні дні користувача (Україна).
KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Категорії для ручного вибору, узгоджені з автоматичною категоризацією
# (канонічні назви після CATEGORY_MERGE і підкатегорії доходу).
MANUAL_ENTRY_CATEGORIES = [
    "Продукти", "Кафе та ресторани", "Транспорт", "АЗС", "Здоров'я",
    "Краса та догляд", "Спорт та фітнес", "Розваги", "Цифровий контент",
    "Одяг та взуття", "Техніка та ПЗ", "Житло та комунальні",
    "Зв'язок та інтернет", "Доставка", "Готівка",
    # Витрата; не плутати з «Подарунок» (дохід).
    "Подарунки та благодійність",
    "Навчання та саморозвиток",
    *sorted(INCOME_CATEGORIES - {"Дохід"}),
    "Заощадження", "Інвестиції", "Борги та кредити",
    # Звичайна витрата; не плутати з цільовою категорією «Інше» нижче.
    "Інші витрати",
    # Перекази (TRANSFER_CATEGORIES), поза прогнозом.
    LOAN_RECEIVED_CATEGORY, OWN_ACCOUNTS_TRANSFER_CATEGORY,
]

# Цілі для POST /transactions/{id}/mark-reimbursement: категорії витрат зі
# списку ручного вибору (без готівки) і окрема категорія «Інше», якої немає в
# ручному виборі.
REIMBURSEMENT_TARGET_CATEGORIES = [
    c for c in MANUAL_ENTRY_CATEGORIES
    if c not in EXPENSE_EXCLUDED_CATEGORIES and c != CASH_CATEGORY
] + ["Інше"]


# Власні категорії витрат користувача. Прогноз, радник, перевірка знаку й ML
# обробляють їх як звичайні витрати.
CUSTOM_CATEGORY_LIMIT = 50
CUSTOM_CATEGORY_MIN_LEN = 2
CUSTOM_CATEGORY_MAX_LEN = 40

# Назви, недоступні для власних категорій (без урахування регістру): список
# ручного вибору, дохід, перекази, службові категорії, «Інше», «Дохід» і всі
# категорії правил MCC і ключових слів (сирі й канонічні).
_RULE_CATEGORIES = {*MCC_CATEGORIES.values(), *KEYWORD_CATEGORIES.values(), *CATEGORY_MERGE}
_RESERVED_CATEGORY_NAMES = {
    name.casefold()
    for name in (
        *MANUAL_ENTRY_CATEGORIES, *INCOME_CATEGORIES, *TRANSFER_CATEGORIES, *SERVICE_CATEGORIES,
        *REIMBURSEMENT_TARGET_CATEGORIES, "Дохід",
        *_RULE_CATEGORIES, *(CATEGORY_MERGE.get(c, c) for c in _RULE_CATEGORIES),
    )
    if name
}


async def _custom_category_names(db: AsyncSession, user_id: str) -> list[str]:
    """Повертає власні категорії користувача в порядку створення."""
    result = await db.execute(
        select(UserCategory.name)
        .where(UserCategory.user_id == user_id)
        .order_by(UserCategory.created_at, UserCategory.id)
    )
    return list(result.scalars())


async def _allowed_manual_categories(db: AsyncSession, user_id: str) -> list[str]:
    """Повертає категорії ручного вибору разом з власними категоріями користувача."""
    return MANUAL_ENTRY_CATEGORIES + await _custom_category_names(db, user_id)


class CustomCategoryRequest(BaseModel):
    """Тіло запиту створення власної категорії."""
    name: str


class ManualTransactionRequest(BaseModel):
    """Тіло запиту ручного введення транзакції.

    Якщо знайдено транзакцію з тим самим часом і сумою, нова не створюється,
    доки клієнт не повторить запит з force=true.
    """
    date: datetime
    description: str
    amount: float  # від'ємна — витрата, додатна — надходження
    category: str | None = None  # None — категорію визначать правила пізніше
    force: bool = False


class TransactionResponse(BaseModel):
    """Транзакція у відповіді API."""
    id: str
    time: int
    description: str | None
    amount_uah: int
    category: str | None
    category_method: str | None
    # possible_duplicate=True: запит нічого не створив, поля описують наявну
    # схожу транзакцію. Щоб додати нову, клієнт повторює запит з force=true.
    possible_duplicate: bool = False
    message: str | None = None
    # Поля для сторінки списку транзакцій:
    loan_id: str | None = None
    account_id: str | None = None
    mcc: int | None = None
    amount: int | None = None  # копійки

    class Config:
        from_attributes = True


async def _get_or_create_manual_account(db: AsyncSession, user_id: str) -> str:
    """Повертає id рахунку ручного введення користувача, створюючи його за потреби."""
    account_id = f"manual-{user_id}"
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if account is None:
        account = Account(id=account_id, user_id=user_id, source_type="manual")
        db.add(account)
        await db.flush()
    return account_id


async def list_manual_categories():
    """Повертає категорії ручного вибору та множини, які потрібні фронтенду.

    Returns:
        categories, income_categories, non_spending_categories і групи для
        списку вибору: expense, income, transfer, service.
    """
    return {
        "categories": MANUAL_ENTRY_CATEGORIES,
        "income_categories": sorted(INCOME_CATEGORIES),
        "non_spending_categories": EXPENSE_EXCLUDED_CATEGORIES,
        # Групи для списку вибору: expense, income і transfer разом дають categories;
        # service — лише для відображення (через PATCH не встановлюються).
        "expense": [
            c for c in MANUAL_ENTRY_CATEGORIES
            if c not in INCOME_CATEGORIES and c not in TRANSFER_CATEGORIES
        ],
        "income": sorted(INCOME_CATEGORIES),
        "transfer": sorted(TRANSFER_CATEGORIES),
        "service": sorted(SERVICE_CATEGORIES),
    }


@router.get("/categories")
async def get_categories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Повертає вбудовані категорії разом з власними категоріями витрат користувача.

    Власні категорії входять у categories, у групу expense і окремо в custom_categories.
    """
    body = await list_manual_categories()
    custom = await _custom_category_names(db, current_user.id)
    return {
        **body,
        "categories": body["categories"] + custom,
        "expense": body["expense"] + custom,
        "custom_categories": custom,
    }


@router.post("/categories/custom", status_code=201)
async def create_custom_category(
    request: CustomCategoryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Створює власну категорію витрат.

    Пробіли по краях назви прибираються; довжина — від 2 до 40 символів, без
    керуючих символів (422). Збіг без урахування регістру з вбудованою чи власною
    категорією — 409. Не більше CUSTOM_CATEGORY_LIMIT категорій на користувача (422).
    """
    name = request.name.strip()
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        raise HTTPException(status_code=422, detail="Назва категорії містить керуючі символи.")
    if not CUSTOM_CATEGORY_MIN_LEN <= len(name) <= CUSTOM_CATEGORY_MAX_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"Назва категорії має містити від {CUSTOM_CATEGORY_MIN_LEN} до {CUSTOM_CATEGORY_MAX_LEN} символів.",
        )
    if name.casefold() in _RESERVED_CATEGORY_NAMES:
        raise HTTPException(status_code=409, detail=f"Категорія «{name}» уже є серед вбудованих.")

    existing = await _custom_category_names(db, current_user.id)
    if name.casefold() in {c.casefold() for c in existing}:
        raise HTTPException(status_code=409, detail=f"Власна категорія «{name}» уже існує.")
    if len(existing) >= CUSTOM_CATEGORY_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=f"Досягнуто ліміту власних категорій ({CUSTOM_CATEGORY_LIMIT}).",
        )

    category = UserCategory(id=str(uuid.uuid4()), user_id=current_user.id, name=name)
    db.add(category)
    try:
        await db.commit()
    except IntegrityError:  # паралельне створення тієї самої назви (унікальний індекс)
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Власна категорія «{name}» уже існує.")
    return {"id": category.id, "name": category.name}


@router.post("/manual", response_model=TransactionResponse, status_code=201)
async def create_manual_transaction(
    request: ManualTransactionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Створює транзакцію з ручного введення.

    Категорія має бути дозволеною й відповідати знаку суми (422). Без категорії
    транзакція категоризується правилами пізніше. Якщо знайдено можливий дублікат
    і force=false, нічого не створюється: повертається наявна транзакція з
    possible_duplicate=true. Обрана категорія записується в пул навчальних
    прикладів ML.
    """
    if request.category is not None:
        allowed = await _allowed_manual_categories(db, current_user.id)
        if request.category not in allowed:
            raise HTTPException(status_code=422, detail=f"Невідома категорія. Дозволені значення: {allowed}")

    amount_kopecks = int(round(request.amount * 100))
    sign_error = validate_manual_category_sign(amount_kopecks, request.category)
    if sign_error:
        raise HTTPException(status_code=422, detail=sign_error)
    txn_time = int(request.date.timestamp())

    if not request.force:
        existing = await find_possible_duplicate(
            db, current_user.id, txn_time, amount_kopecks, bank_id=None
        )
        if existing is not None:
            return TransactionResponse(
                id=existing.id,
                time=existing.time,
                description=existing.description,
                amount_uah=existing.amount_uah,
                category=existing.category,
                category_method=existing.category_method,
                loan_id=existing.loan_id,
                account_id=existing.account_id,
                mcc=existing.mcc,
                amount=existing.amount,
                possible_duplicate=True,
                message="Схожу транзакцію (та сама дата і сума) вже внесено. Повтори запит з force=true, щоб додати попри це.",
            )

    account_id = await _get_or_create_manual_account(db, current_user.id)

    transaction = Transaction(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        account_id=account_id,
        time=int(request.date.timestamp()),
        # Завжди 'date': форма передає лише дату; час, якщо його надіслали,
        # точності не підвищує.
        time_precision="date",
        description=request.description,
        mcc=None,
        amount=amount_kopecks,
        # amount_uah = amount // 100, як у sync.py і imports.py: int() округлює
        # від'ємні дробові суми до нуля, а не вниз.
        amount_uah=amount_kopecks // 100,
        currency_code=980,
        cashback_amount=0,
        balance=None,
        hold=False,
        category=request.category,
        category_method="manual" if request.category else None,
    )
    db.add(transaction)
    if request.category:
        # Категорія, обрана користувачем, — навчальний приклад для ML.
        await record_training_example(db, transaction, "manual_confirmed")
    await db.commit()
    await db.refresh(transaction)
    return transaction


@router.get("", response_model=list[TransactionResponse])
async def list_transactions(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=0, le=1000),
    offset: int = Query(0, ge=0),
    category: str | None = None,
    sign: Literal["in", "out"] | None = None,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    q: str | None = None,
    sort: Literal["time_desc", "amount_desc", "amount_asc"] = "time_desc",
    min_amount: int | None = Query(None, ge=0),
):
    """Повертає транзакції користувача з фільтрами, сортуванням і пагінацією.

    Фільтри:
    - category, зокрема "pending";
    - sign: in (amount > 0) або out (amount < 0);
    - from/to: календарні дні за Києвом, обидві межі включно;
    - q: підрядок опису без урахування регістру (% і _ — звичайні символи);
    - min_amount: цілі гривні, |amount| >= min_amount * 100 копійок.

    sort: time_desc (за замовчуванням), amount_desc або amount_asc — за модулем
    суми; за рівності — за id, щоб сторінки були стабільні. Заголовки
    X-Total-Count і X-Total-Amount (сума |amount| у копійках) рахуються після
    фільтрів, до limit/offset.
    """
    conditions = [Transaction.user_id == current_user.id]
    if category is not None:
        conditions.append(Transaction.category == category)
    if sign == "in":
        conditions.append(Transaction.amount > 0)
    elif sign == "out":
        conditions.append(Transaction.amount < 0)
    if date_from is not None:
        start = datetime.combine(date_from, datetime.min.time(), tzinfo=KYIV_TZ)
        conditions.append(Transaction.time >= int(start.timestamp()))
    if date_to is not None:
        end = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=KYIV_TZ)
        conditions.append(Transaction.time < int(end.timestamp()))
    if q:
        pattern = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(Transaction.description.ilike(f"%{pattern}%", escape="\\"))
    if min_amount is not None:
        conditions.append(func.abs(Transaction.amount) >= min_amount * 100)

    total, total_amount = (await db.execute(
        select(func.count(), func.coalesce(func.sum(func.abs(Transaction.amount)), 0))
        .select_from(Transaction).where(*conditions)
    )).one()
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Total-Amount"] = str(total_amount)

    if sort == "amount_desc":
        order = [func.abs(Transaction.amount).desc().nulls_last()]
    elif sort == "amount_asc":
        order = [func.abs(Transaction.amount).asc().nulls_last()]
    else:
        order = [Transaction.time.desc()]

    result = await db.execute(
        select(Transaction)
        .where(*conditions)
        .order_by(*order, Transaction.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return result.scalars().all()


@router.get("/income-review", response_model=list[TransactionResponse])
async def income_review(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Повертає невизначені надходження (category='pending', amount > 0) для ручного рішення."""
    result = await db.execute(
        select(Transaction)
        .where(
            Transaction.user_id == current_user.id,
            Transaction.category == "pending",
            Transaction.amount_uah > 0,
        )
        .order_by(Transaction.time.desc())
    )
    return result.scalars().all()


class UpdateCategoryRequest(BaseModel):
    """Тіло запиту зміни категорії."""
    category: str


@router.patch("/{transaction_id}", response_model=TransactionResponse)
async def update_transaction_category(
    transaction_id: str,
    request: UpdateCategoryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Змінює категорію транзакції вручну.

    Повертає 404, якщо транзакцію не знайдено; 409 для службових рядків
    (повернення позики, повернення спільної витрати); 422 для недозволеної
    категорії або категорії, що не відповідає знаку суми. Нова категорія
    записується в пул навчальних прикладів ML.
    """
    allowed = await _allowed_manual_categories(db, current_user.id)
    if request.category not in allowed:
        raise HTTPException(status_code=422, detail=f"Невідома категорія. Дозволені значення: {allowed}")

    result = await db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.user_id == current_user.id,
        )
    )
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status_code=404, detail="Транзакцію не знайдено")

    # Службові рядки не змінюються через PATCH: це зламало б зв'язок із позикою
    # або віднімання спільної витрати.
    if transaction.loan_id is not None or transaction.category_method == "reimbursement":
        raise HTTPException(
            status_code=409,
            detail=(
                "Цю транзакцію прив'язано до повернення позики або позначено як повернення "
                "спільної витрати — звичайна зміна категорії зламала б цей зв'язок."
            ),
        )
    sign_error = validate_manual_category_sign(transaction.amount, request.category)
    if sign_error:
        raise HTTPException(status_code=422, detail=sign_error)

    transaction.category = request.category
    transaction.category_method = "manual"
    await record_training_example(db, transaction, "manual_confirmed")
    await db.commit()
    await db.refresh(transaction)
    return transaction


class RepayLoanRequest(BaseModel):
    """Тіло запиту: позика, яку повертають."""
    loan_id: str


@router.post("/{transaction_id}/repay-loan", response_model=TransactionResponse)
async def repay_loan(
    transaction_id: str,
    request: RepayLoanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Позначає невизначене надходження як повернення виданої позики.

    Зменшує залишок позики й ставить службову категорію «Повернення позики»,
    яка не рахується ні доходом, ні витратою. Повертає 404, якщо транзакцію чи
    позику не знайдено, і 422 для недодатної суми або суми, більшої за залишок.
    """
    result = await db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.user_id == current_user.id,
        )
    )
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status_code=404, detail="Транзакцію не знайдено")

    if transaction.amount_uah is None or transaction.amount_uah <= 0:
        raise HTTPException(
            status_code=422,
            detail="Повернення позики має бути додатною сумою",
        )

    loan_result = await db.execute(
        select(Loan).where(Loan.id == request.loan_id, Loan.user_id == current_user.id)
    )
    loan = loan_result.scalar_one_or_none()
    if loan is None:
        raise HTTPException(status_code=404, detail="Позику не знайдено")

    if transaction.amount_uah > loan.remaining_amount:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Сума транзакції ({transaction.amount_uah} грн) перевищує "
                f"залишок позики ({loan.remaining_amount} грн)"
            ),
        )

    loan.remaining_amount -= transaction.amount_uah
    loan.status = "repaid" if loan.remaining_amount == 0 else "partially_repaid"

    transaction.loan_id = loan.id
    transaction.category = "Повернення позики"
    transaction.category_method = "manual"

    await db.commit()
    await db.refresh(transaction)
    return transaction


class MarkReimbursementRequest(BaseModel):
    """Тіло запиту: категорія витрати, частку якої повернули."""
    category: str


@router.post("/{transaction_id}/mark-reimbursement", response_model=TransactionResponse)
async def mark_reimbursement(
    transaction_id: str,
    request: MarkReimbursementRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Позначає надходження як повернення частки спільної витрати.

    Транзакція отримує category_method='reimbursement' і категорію витрати; прогноз
    і радник віднімають її суму від витрат цієї категорії за той самий місяць.
    Початкова витрата не змінюється. Повертає 404, якщо транзакцію не знайдено, і
    422 для недодатної суми або недопустимої категорії.
    """
    result = await db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.user_id == current_user.id,
        )
    )
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status_code=404, detail="Транзакцію не знайдено")

    if transaction.amount_uah is None or transaction.amount_uah <= 0:
        raise HTTPException(
            status_code=422,
            detail="Повернення спільної витрати має бути додатною сумою",
        )

    if request.category not in REIMBURSEMENT_TARGET_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"Невідома категорія. Дозволені значення: {REIMBURSEMENT_TARGET_CATEGORIES}",
        )

    transaction.category = request.category
    transaction.category_method = "reimbursement"

    await db.commit()
    await db.refresh(transaction)
    return transaction
