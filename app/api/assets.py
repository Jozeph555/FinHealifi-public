"""
Активи користувача (заощадження, інвестиції, нерухомість, готівка): довідкові записи.
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
from app.models.asset import Asset

router = APIRouter(prefix="/assets", tags=["assets"])

# Рекомендовані типи для списку на фронтенді. asset_type приймає будь-який
# непорожній рядок.
ASSET_TYPES = ["Депозит", "Інвестиції", "Нерухомість", "Готівка", "Інше"]

LIQUIDITY_LEVELS = ("high", "medium", "low")


class AssetCreateRequest(BaseModel):
    """Тіло запиту створення активу."""
    name: str
    asset_type: str
    current_amount: int
    liquidity_level: str  # 'high' | 'medium' | 'low'
    liquidity_days: Optional[int] = None
    annual_rate: Optional[float] = None
    rate_note: Optional[str] = None
    opened_at: Optional[date] = None


class AssetUpdateRequest(BaseModel):
    """Тіло часткового оновлення активу; передані null очищують поле."""
    name: Optional[str] = None
    asset_type: Optional[str] = None
    current_amount: Optional[int] = None
    liquidity_level: Optional[str] = None
    liquidity_days: Optional[int] = None
    annual_rate: Optional[float] = None
    rate_note: Optional[str] = None
    opened_at: Optional[date] = None
    is_active: Optional[bool] = None


class AssetResponse(BaseModel):
    """Актив у відповіді API."""
    id: str
    name: str
    asset_type: str
    current_amount: int
    liquidity_level: str
    liquidity_days: Optional[int]
    annual_rate: Optional[float]
    rate_note: Optional[str]
    opened_at: Optional[date]
    is_active: bool

    class Config:
        from_attributes = True


@router.post("", response_model=AssetResponse, status_code=201)
async def create_asset(
    request: AssetCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Створює актив; 422, якщо liquidity_level не 'high', 'medium' чи 'low'."""
    if request.liquidity_level not in LIQUIDITY_LEVELS:
        raise HTTPException(
            status_code=422,
            detail="liquidity_level має бути 'high', 'medium' або 'low'",
        )

    asset = Asset(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        name=request.name,
        asset_type=request.asset_type,
        current_amount=request.current_amount,
        liquidity_level=request.liquidity_level,
        liquidity_days=request.liquidity_days,
        annual_rate=request.annual_rate,
        rate_note=request.rate_note,
        opened_at=request.opened_at,
        is_active=True,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


@router.get("", response_model=list[AssetResponse])
async def list_assets(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    include_inactive: bool = False,
):
    """Повертає активні активи користувача; з include_inactive — також архівні."""
    query = select(Asset).where(Asset.user_id == current_user.id)
    if not include_inactive:
        query = query.where(Asset.is_active == True)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/types")
async def list_asset_types():
    """Повертає рекомендований список типів активів; інші типи теж допускаються."""
    return {"asset_types": ASSET_TYPES}


@router.patch("/{asset_id}", response_model=AssetResponse)
async def update_asset(
    asset_id: str,
    request: AssetUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Частково оновлює актив (лише передані поля).

    Повертає 404, якщо актив не знайдено, і 422 для недопустимого liquidity_level.
    """
    result = await db.execute(
        select(Asset).where(Asset.id == asset_id, Asset.user_id == current_user.id)
    )
    asset = result.scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Актив не знайдено")

    if request.liquidity_level is not None and request.liquidity_level not in LIQUIDITY_LEVELS:
        raise HTTPException(
            status_code=422,
            detail="liquidity_level має бути 'high', 'medium' або 'low'",
        )

    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(asset, field, value)

    await db.commit()
    await db.refresh(asset)
    return asset
