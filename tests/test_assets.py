"""
Тести CRUD активів: створення, список з фільтром is_active, часткове
оновлення, валідація liquidity_level, ізоляція між користувачами, список типів.

Функції ендпоінтів викликаються напряму, без HTTP-шару.
"""
import uuid

import pytest
from fastapi import HTTPException

from app.api.assets import (
    create_asset,
    AssetCreateRequest,
    list_assets,
    update_asset,
    AssetUpdateRequest,
    list_asset_types,
    ASSET_TYPES,
)


async def _make_asset(db_session, real_user, **overrides):
    defaults = dict(
        name="Депозит ПриватБанк",
        asset_type="Депозит",
        current_amount=10000,
        liquidity_level="medium",
    )
    defaults.update(overrides)
    return await create_asset(
        AssetCreateRequest(**defaults),
        current_user=real_user, db=db_session,
    )


# --- create ---------------------------------------------------------------

async def test_create_asset_round_trips_fields(db_session, real_user):
    asset = await _make_asset(
        db_session, real_user,
        annual_rate=2.0, rate_note="плаваюча ставка", liquidity_days=30,
    )
    assert asset.is_active is True
    assert asset.current_amount == 10000
    assert asset.name == "Депозит ПриватБанк"
    assert asset.asset_type == "Депозит"
    assert asset.liquidity_level == "medium"
    assert asset.liquidity_days == 30
    assert asset.annual_rate == 2.0
    assert asset.rate_note == "плаваюча ставка"


async def test_create_asset_invalid_liquidity_level_rejected(db_session, real_user):
    with pytest.raises(HTTPException) as exc_info:
        await _make_asset(db_session, real_user, liquidity_level="super-high")
    assert exc_info.value.status_code == 422


async def test_create_asset_accepts_custom_asset_type(db_session, real_user):
    asset = await _make_asset(db_session, real_user, asset_type="Криптовалюта")
    assert asset.asset_type == "Криптовалюта"


# --- list -------------------------------------------------------------------

async def test_list_assets_excludes_inactive_by_default(db_session, real_user):
    active = await _make_asset(db_session, real_user, name="Активний")
    inactive = await _make_asset(db_session, real_user, name="Архівний")
    await update_asset(
        inactive.id, AssetUpdateRequest(is_active=False),
        current_user=real_user, db=db_session,
    )

    default_list = await list_assets(current_user=real_user, db=db_session)
    assert {a.id for a in default_list} == {active.id}

    full_list = await list_assets(
        current_user=real_user, db=db_session, include_inactive=True,
    )
    assert {a.id for a in full_list} == {active.id, inactive.id}


# --- update -------------------------------------------------------------------

async def test_update_asset_partial_update(db_session, real_user):
    asset = await _make_asset(db_session, real_user)

    updated = await update_asset(
        asset.id, AssetUpdateRequest(current_amount=15000),
        current_user=real_user, db=db_session,
    )

    assert updated.current_amount == 15000
    assert updated.name == "Депозит ПриватБанк"
    assert updated.asset_type == "Депозит"


async def test_update_asset_invalid_liquidity_level_rejected(db_session, real_user):
    asset = await _make_asset(db_session, real_user)

    with pytest.raises(HTTPException) as exc_info:
        await update_asset(
            asset.id, AssetUpdateRequest(liquidity_level="super-high"),
            current_user=real_user, db=db_session,
        )
    assert exc_info.value.status_code == 422

    await db_session.refresh(asset)
    assert asset.liquidity_level == "medium"


async def test_update_asset_foreign_asset_returns_404(db_session, real_user):
    from app.models.user import User

    other_user = User(
        id=f"test-{uuid.uuid4()}",
        email=f"other-{uuid.uuid4()}@example.com",
        password_hash="not-a-real-hash",
    )
    db_session.add(other_user)
    await db_session.flush()

    other_asset = await _make_asset(db_session, other_user)

    with pytest.raises(HTTPException) as exc_info:
        await update_asset(
            other_asset.id, AssetUpdateRequest(current_amount=1),
            current_user=real_user, db=db_session,
        )
    assert exc_info.value.status_code == 404


async def test_update_asset_missing_returns_404(db_session, real_user):
    with pytest.raises(HTTPException) as exc_info:
        await update_asset(
            f"missing-{uuid.uuid4()}", AssetUpdateRequest(current_amount=1),
            current_user=real_user, db=db_session,
        )
    assert exc_info.value.status_code == 404


# --- types --------------------------------------------------------------------

async def test_list_asset_types_returns_expected_list():
    result = await list_asset_types()
    assert result["asset_types"] == ASSET_TYPES
