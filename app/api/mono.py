"""
Підключення Monobank, синхронізація транзакцій і категоризація.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.account import Account
from app.models.sync_job import SyncJob
from app.models.transaction import Transaction
from app.core.security import encrypt_token
from app.services.monobank import get_client_info
from app.services.sync import run_sync_job
from app.services.categorization import categorize_all_transactions, recategorize_pending
from app.services.mcc_mapping import guard_positive_amount_category
from app.services.ml_categorizer import (
    get_classifier_for_user,
    predict_category,
    is_ml_assignable,
    LOW_CONFIDENCE_THRESHOLD,
)

router = APIRouter(prefix="/mono", tags=["monobank"])


class ConnectRequest(BaseModel):
    """Тіло запиту підключення: токен Monobank."""
    token: str


class SyncStartRequest(BaseModel):
    """Тіло запиту синхронізації: рахунки й глибина історії в місяцях."""
    account_ids: list[str]
    months: int = 1


@router.post("/connect")
async def connect_monobank(
    request: ConnectRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Перевіряє токен Monobank і створює рахунки користувача.

    Токен зберігається зашифрованим; для вже підключених рахунків він
    оновлюється. Повертає 400, якщо токен не пройшов перевірку.
    """
    try:
        client_info = await get_client_info(request.token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не вдалося перевірити токен: {e}")

    encrypted = encrypt_token(request.token)
    created_accounts = []
    existing_accounts = []

    for acc in client_info.get("accounts", []):
        account_id = acc["id"]
        result = await db.execute(select(Account).where(Account.id == account_id))
        existing = result.scalar_one_or_none()
        if existing:
            # Токен міг бути перевипущений — оновлюємо
            existing.mono_token_encrypted = encrypted
            existing_accounts.append(account_id)
            continue

        account = Account(
            id=account_id,
            user_id=current_user.id,
            source_type="monobank_api",
            mono_token_encrypted=encrypted,
            bank_name="monobank",
            bank_id="monobank",  # id банку з довідника
            masked_pan=(acc.get("maskedPan") or [None])[0],
            currency_code=str(acc.get("currencyCode")),
        )
        db.add(account)
        created_accounts.append(account_id)

    await db.commit()
    return {
        "client_name": client_info.get("name"),
        "accounts_created": created_accounts,
        "accounts_existing": existing_accounts,
        "account_ids": created_accounts + existing_accounts,  # усі рахунки токена — для /mono/sync/start
        "accounts_total": len(client_info.get("accounts", [])),
    }


@router.post("/sync/start")
async def start_sync(
    request: SyncStartRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускає фонову синхронізацію обраних рахунків.

    Усі рахунки мають належати користувачу (404) і одному токену Monobank (422).

    Returns:
        {"job_id": ..., "status": "pending"}; прогрес — GET /mono/sync/status/{job_id}.
    """
    if not request.account_ids:
        raise HTTPException(status_code=422, detail="Потрібно обрати хоча б один рахунок")

    result = await db.execute(
        select(Account).where(
            Account.id.in_(request.account_ids),
            Account.user_id == current_user.id,
        )
    )
    accounts = result.scalars().all()
    if len(accounts) != len(request.account_ids):
        raise HTTPException(
            status_code=404,
            detail="Один або кілька account_ids не знайдено серед твоїх рахунків",
        )

    encrypted_token = accounts[0].mono_token_encrypted
    if not encrypted_token:
        raise HTTPException(
            status_code=400,
            detail="Обраний рахунок не має збереженого токена — виклич /mono/connect спочатку",
        )

    # Один SyncJob використовує один токен.
    if any(a.mono_token_encrypted != encrypted_token for a in accounts):
        raise HTTPException(
            status_code=422,
            detail="Обрані рахунки належать різним Monobank-токенам — синхронізуй їх окремими запитами",
        )

    job_id = str(uuid.uuid4())
    job = SyncJob(
        id=job_id,
        user_id=current_user.id,
        account_ids=request.account_ids,
        status="pending",
    )
    db.add(job)
    await db.commit()

    background_tasks.add_task(
        run_sync_job, job_id, current_user.id, encrypted_token, request.months
    )

    return {"job_id": job_id, "status": "pending"}


@router.get("/sync/status/{job_id}")
async def sync_status(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Повертає стан задачі синхронізації; 404, якщо її немає, 403 — якщо вона чужа."""
    result = await db.execute(select(SyncJob).where(SyncJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job не знайдено")
    if job.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Цей job належить іншому користувачу")

    return {
        "job_id": job.id,
        "status": job.status,
        "current_account_id": job.current_account_id,
        "chunks_done": job.chunks_done,
        "chunks_total": job.chunks_total,
        "duplicates_removed": job.duplicates_removed,
        "error_message": job.error_message,
    }


@router.post("/categorize")
async def categorize():
    """Категоризує правилами всі транзакції без категорії (усіх користувачів)."""
    try:
        return await categorize_all_transactions()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/recategorize-pending")
async def recategorize():
    """Повторно застосовує правила до всіх pending-транзакцій (усіх користувачів)."""
    try:
        return await recategorize_pending()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/recategorize-pending-ml")
async def recategorize_pending_ml(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Категоризує ML-моделлю pending-транзакції поточного користувача.

    Використовує персональну модель користувача або спільну default-модель.
    Передбачення забороненого класу або категорії витрат для додатної суми не
    застосовується (blocked_by_policy). Повертає 400, якщо модель ще не навчено.

    Returns:
        {"total": ..., "stats": {"ml", "low_confidence", "still_pending", "blocked_by_policy"}}.
    """
    try:
        clf = get_classifier_for_user(current_user.id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=400,
            detail="ML-модель ще не натренована — запусти `python train.py`",
        )

    result = await db.execute(
        select(Transaction).where(
            Transaction.user_id == current_user.id,
            Transaction.category == "pending",
        )
    )
    transactions = result.scalars().all()

    # blocked_by_policy — частина still_pending: модель упевнено передбачила
    # заборонений клас (ML_FORBIDDEN_CATEGORIES) або категорію витрат для додатної
    # суми; транзакція лишається pending.
    stats = {"ml": 0, "low_confidence": 0, "still_pending": 0, "blocked_by_policy": 0}

    for t in transactions:
        if not t.description:
            stats["still_pending"] += 1
            continue

        category, confidence, method = predict_category(t.description, clf)
        if method == "ml" and (
            not is_ml_assignable(category)
            or guard_positive_amount_category(t.amount, category) == "pending"
        ):
            stats["blocked_by_policy"] += 1
            stats["still_pending"] += 1
        elif method == "ml":
            t.category = category
            t.category_method = "ml"
            t.category_confidence = confidence
            stats["ml"] += 1
            if confidence < LOW_CONFIDENCE_THRESHOLD:
                stats["low_confidence"] += 1
        else:
            stats["still_pending"] += 1

    await db.commit()
    return {"total": len(transactions), "stats": stats}
