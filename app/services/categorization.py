"""
Правилова категоризація транзакцій (MCC і ключові слова) та наповнення пулу
навчальних прикладів для ML-категоризатора.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.models.database import AsyncSessionLocal
from app.models.transaction import Transaction
from app.models.category_training_example import CategoryTrainingExample
from app.services.mcc_mapping import (
    categorize_transaction, CATEGORY_MERGE, categorize_by_income_keyword, LOAN_REPAYMENT_CATEGORY,
)

# Службова категорія POST /transactions/{id}/repay-loan. Такі рядки мають
# category_method='manual', але це не категорія витрат чи доходу, тож у пул
# навчальних прикладів ML вони не потрапляють.
_LOAN_REPAYMENT_SENTINEL_CATEGORY = LOAN_REPAYMENT_CATEGORY


async def record_training_example(session, transaction: Transaction, source: str) -> None:
    """Записує транзакцію в пул навчальних прикладів ML (category_training_examples).

    Викликається з правилової категоризації (source='mcc'/'keyword') і з ручного
    підтвердження категорії (source='manual_confirmed'); ML-передбачення в пул
    не записуються. Повторний запис тієї самої транзакції оновлює приклад
    (upsert за source_transaction_id).

    Args:
        session: Сесія БД; коміт робить викликач.
        transaction: Транзакція з описом і категорією; без них нічого не пишеться.
        source: Джерело категорії: 'mcc', 'keyword' або 'manual_confirmed'.
    """
    if not transaction.description or not transaction.category:
        return

    stmt = pg_insert(CategoryTrainingExample).values(
        id=str(uuid.uuid4()),
        user_id=transaction.user_id,
        source_transaction_id=transaction.id,
        description=transaction.description,
        category=transaction.category,
        source=source,
    ).on_conflict_do_update(
        index_elements=["source_transaction_id"],
        set_={"category": transaction.category, "source": source},
    )
    await session.execute(stmt)


async def categorize_all_transactions():
    """Категоризує правилами всі транзакції без категорії (усіх користувачів).

    Нерозпізнані транзакції отримують категорію 'pending'. Розпізнані за MCC чи
    ключовим словом записуються в пул навчальних прикладів.

    Returns:
        {"total": кількість оброблених, "stats": {"mcc": n, "keyword": n, "pending": n}}.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction).where(Transaction.category == None)
        )
        transactions = result.scalars().all()

        stats = {"mcc": 0, "keyword": 0, "pending": 0}

        for t in transactions:
            category, method = categorize_transaction(t.mcc, t.description, t.amount)
            t.category = category if category else "pending"
            t.category_method = method
            stats[method] += 1
            if method in ("mcc", "keyword"):
                await record_training_example(session, t, method)

        await session.commit()
        return {"total": len(transactions), "stats": stats}


async def recategorize_pending():
    """Повторно застосовує правила до всіх транзакцій у статусі 'pending' (усіх користувачів).

    Returns:
        {"total": кількість оброблених, "stats": {"mcc": n, "keyword": n, "pending": n}}.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction).where(Transaction.category == "pending")
        )
        transactions = result.scalars().all()

        stats = {"mcc": 0, "keyword": 0, "pending": 0}

        for t in transactions:
            category, method = categorize_transaction(t.mcc, t.description, t.amount)
            t.category = category if category else "pending"
            t.category_method = method
            stats[method] += 1
            if method in ("mcc", "keyword"):
                await record_training_example(session, t, method)

        await session.commit()
        return {"total": len(transactions), "stats": stats}


async def backfill_training_examples():
    """Наповнює пул навчальних прикладів з уже категоризованих транзакцій.

    Бере транзакції з category_method 'mcc', 'keyword' або 'manual', крім
    службової категорії повернення позики. Повторний запуск не дублює рядки.

    Returns:
        {"total": кількість записаних прикладів}.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction).where(
                Transaction.category_method.in_(["mcc", "keyword", "manual"]),
                Transaction.category.isnot(None),
                Transaction.category != "pending",
                Transaction.category != _LOAN_REPAYMENT_SENTINEL_CATEGORY,
            )
        )
        transactions = result.scalars().all()

        for t in transactions:
            source = "manual_confirmed" if t.category_method == "manual" else t.category_method
            await record_training_example(session, t, source)

        await session.commit()
        return {"total": len(transactions)}


async def normalize_existing_categories():
    """Приводить категорії mcc/keyword-транзакцій до канонічних через CATEGORY_MERGE.

    Категорії, для яких CATEGORY_MERGE дає None (наприклад, «Магазини»),
    переводяться в 'pending'. Транзакції з іншими методами не змінюються.

    Returns:
        Лічильники total_checked, updated, moved_to_pending, unchanged.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction).where(Transaction.category_method.in_(["mcc", "keyword"]))
        )
        transactions = result.scalars().all()

        updated = 0
        unchanged = 0
        moved_to_pending = 0

        for t in transactions:
            if t.category not in CATEGORY_MERGE:
                unchanged += 1
                continue

            merged = CATEGORY_MERGE[t.category]
            if merged is None:
                t.category = "pending"
                t.category_method = "pending"
                moved_to_pending += 1
            else:
                t.category = merged
                updated += 1

        await session.commit()
        return {
            "total_checked": len(transactions),
            "updated": updated,
            "moved_to_pending": moved_to_pending,
            "unchanged": unchanged,
        }


async def recategorize_income_subcategories():
    """Розподіляє транзакції із загальною категорією «Дохід» за підкатегоріями доходу.

    Застосовуються лише правила доходу (INCOME_KEYWORD_CATEGORIES), тож дохід не
    може потрапити в категорію витрат. Транзакції без збігу отримують «Інший
    дохід» з category_method='income_backfill_fallback'.

    Returns:
        {"total": кількість оброблених, "stats": {категорія: кількість}}.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction).where(Transaction.category == "Дохід")
        )
        transactions = result.scalars().all()

        stats = {}
        for t in transactions:
            matched = categorize_by_income_keyword(t.description)
            t.category = matched or "Інший дохід"
            t.category_method = "keyword" if matched else "income_backfill_fallback"
            stats[t.category] = stats.get(t.category, 0) + 1

        await session.commit()
        return {"total": len(transactions), "stats": stats}
