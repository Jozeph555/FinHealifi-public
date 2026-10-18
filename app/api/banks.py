"""
Довідник банків для вибору під час імпорту виписки.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.bank import Bank

router = APIRouter(prefix="/banks", tags=["banks"])


@router.get("")
async def list_banks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Повертає банки з довідника (топ-20 українських) і власні банки, додані користувачем під час імпорту."""
    result = await db.execute(select(Bank).order_by(Bank.is_seed.desc(), Bank.name))
    banks = result.scalars().all()
    return [{"id": b.id, "name": b.name, "mfo": b.mfo} for b in banks]
