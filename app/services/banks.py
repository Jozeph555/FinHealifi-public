"""Пошук або створення запису банку з довідника для рахунку імпортованої виписки."""
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank import Bank


def _slugify(name: str) -> str:
    """Перетворює назву банку на ідентифікатор з латиниці, кирилиці, цифр і дефісів."""
    slug = re.sub(r"[^a-zа-яіїєґ0-9]+", "-", name.strip().lower()).strip("-")
    return slug or str(uuid.uuid4())


async def get_or_create_bank(
    session: AsyncSession,
    bank_id: str | None,
    custom_name: str | None,
    custom_mfo: str | None,
) -> Bank:
    """Повертає банк для рахунку імпортованої виписки.

    Банк із довідника береться за bank_id. Для власного банку спершу шукається
    наявний запис за МФО, потім за назвою без урахування регістру й пробілів по
    краях; якщо нічого не знайдено, створюється новий запис (is_seed=False).

    Args:
        session: Сесія БД.
        bank_id: Id банку з довідника (GET /banks).
        custom_name: Назва банку, якого немає в довіднику.
        custom_mfo: МФО такого банку, опційно.

    Returns:
        Знайдений або створений Bank.

    Raises:
        ValueError: bank_id не знайдено або не задано ні bank_id, ні custom_name.
    """
    if bank_id:
        result = await session.execute(select(Bank).where(Bank.id == bank_id))
        bank = result.scalar_one_or_none()
        if bank is None:
            raise ValueError(f"Банк з id '{bank_id}' не знайдено")
        return bank

    if not custom_name:
        raise ValueError("Потрібно вказати bank_id або custom_bank_name")

    if custom_mfo:
        result = await session.execute(select(Bank).where(Bank.mfo == custom_mfo))
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

    normalized_name = custom_name.strip().lower()
    result = await session.execute(select(Bank).where(Bank.is_seed == False))
    for candidate in result.scalars().all():
        if candidate.name.strip().lower() == normalized_name:
            return candidate

    new_bank = Bank(
        id=f"custom-{_slugify(custom_name)}-{uuid.uuid4().hex[:8]}",
        name=custom_name.strip(),
        mfo=custom_mfo,
        is_seed=False,
    )
    session.add(new_bank)
    await session.flush()
    return new_bank
