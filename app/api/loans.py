"""
Позики, видані користувачем іншим людям.
"""
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.loan import Loan

router = APIRouter(prefix="/loans", tags=["loans"])


class LoanCreateRequest(BaseModel):
    """Тіло запиту створення позики."""
    borrower_name: str
    principal_amount: int
    issued_at: date


class LoanUpdateRequest(BaseModel):
    """Тіло часткового оновлення позики; передані null очищують поле."""
    borrower_name: Optional[str] = None
    principal_amount: Optional[int] = None
    remaining_amount: Optional[int] = None
    issued_at: Optional[date] = None
    status: Optional[str] = None  # 'not_repaid' | 'partially_repaid' | 'repaid'


class LoanResponse(BaseModel):
    """Позика в відповіді API."""
    id: str
    borrower_name: str
    principal_amount: int
    remaining_amount: int
    issued_at: date
    status: str

    class Config:
        from_attributes = True


@router.post("", response_model=LoanResponse, status_code=201)
async def create_loan(
    request: LoanCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Створює позику зі статусом 'not_repaid'."""
    loan = Loan(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        borrower_name=request.borrower_name,
        principal_amount=request.principal_amount,
        remaining_amount=request.principal_amount,
        issued_at=request.issued_at,
        status="not_repaid",
    )
    db.add(loan)
    await db.commit()
    await db.refresh(loan)
    return loan


@router.get("", response_model=list[LoanResponse])
async def list_loans(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    include_repaid: bool = False,
):
    """Повертає неповернені позики користувача; з include_repaid — також повернені."""
    query = select(Loan).where(Loan.user_id == current_user.id)
    if not include_repaid:
        query = query.where(Loan.status != "repaid")
    result = await db.execute(query)
    return result.scalars().all()


@router.patch("/{loan_id}", response_model=LoanResponse)
async def update_loan(
    loan_id: str,
    request: LoanUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Частково оновлює позику (лише передані поля).

    Поля встановлюються без перевірки узгодженості між собою. Повертає 404, якщо
    позику не знайдено, і 422 для недопустимого status.
    """
    result = await db.execute(
        select(Loan).where(Loan.id == loan_id, Loan.user_id == current_user.id)
    )
    loan = result.scalar_one_or_none()
    if loan is None:
        raise HTTPException(status_code=404, detail="Позику не знайдено")

    if request.status is not None and request.status not in (
        "not_repaid", "partially_repaid", "repaid",
    ):
        raise HTTPException(
            status_code=422,
            detail="status має бути 'not_repaid', 'partially_repaid' або 'repaid'",
        )

    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(loan, field, value)

    await db.commit()
    await db.refresh(loan)
    return loan
