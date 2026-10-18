"""
Пошук і видалення дублікатів транзакцій між джерелами (Monobank, імпорт
виписок, ручне введення).

- remove_duplicate_transactions — видаляє точні збіги (user_id, time, amount)
  після масової вставки: імпорт виписки, ручна й періодична синхронізація.
- find_possible_duplicate — перевірка перед ручним створенням транзакції;
  нічого не змінює, рішення додати транзакцію попри збіг приймає користувач
  (ManualTransactionRequest.force).
- match_duplicates / plan_duplicate_removal — план дедуплікації з урахуванням
  точності часу; застосовується окремо (app/services/dedup_apply.py).

Сумісність за банком: рахунок без банку сумісний з будь-яким; два різні
конкретні банки несумісні, якщо хоча б одна сторона — monobank_api; два
рядки з csv_import сумісні завжди.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, func, delete

from app.models.transaction import Transaction
from app.models.account import Account

# Пріоритет джерела при виборі рядка, що лишається (менше — вище).
SOURCE_TYPE_PRIORITY = {"monobank_api": 0, "csv_import": 1, "manual": 2}
_UNKNOWN_SOURCE_PRIORITY = 99
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class DedupResult:
    """Результат remove_duplicate_transactions: кількість і id видалених транзакцій."""
    removed_count: int = 0
    removed_ids: set[str] = field(default_factory=set)


def _bank_compatible(bank_id_a, source_a, bank_id_b, source_b) -> bool:
    """Перевіряє, чи можуть два рядки бути однією транзакцією з огляду на банк.

    Два рядки csv_import сумісні завжди: їхній bank_id обирає користувач під
    час імпорту. Інакше рядки сумісні, якщо хоча б в одного банк не вказано
    або банки збігаються.
    """
    if source_a == "csv_import" and source_b == "csv_import":
        return True
    return bank_id_a is None or bank_id_b is None or bank_id_a == bank_id_b


async def remove_duplicate_transactions(session, user_id: str | None = None) -> DedupResult:
    """Видаляє точні дублікати (user_id, time, amount) з сумісних банків.

    У кожній групі лишається рядок з найвищим пріоритетом джерела
    (SOURCE_TYPE_PRIORITY), за рівності — найраніше створений. Коміт робить
    викликач.

    Args:
        session: Сесія БД.
        user_id: Користувач; None — усі користувачі (групування завжди йде в
            межах одного користувача).

    Returns:
        DedupResult з кількістю та id видалених транзакцій.
    """
    group_query = select(
        Transaction.user_id, Transaction.time, Transaction.amount
    ).group_by(Transaction.user_id, Transaction.time, Transaction.amount).having(
        func.count() > 1
    )
    if user_id is not None:
        group_query = group_query.where(Transaction.user_id == user_id)

    groups = (await session.execute(group_query)).all()

    result = DedupResult()
    for group_user_id, group_time, group_amount in groups:
        rows_query = (
            select(Transaction, Account.bank_id, Account.source_type)
            .join(Account, Transaction.account_id == Account.id)
            .where(
                Transaction.user_id == group_user_id,
                Transaction.time == group_time,
                Transaction.amount == group_amount,
            )
        )
        rows = (await session.execute(rows_query)).all()
        if len(rows) < 2:
            continue  # група змінилася між запитами

        def sort_key(row):
            """Ключ порядку: пріоритет джерела, потім час створення."""
            txn, _bank_id, source_type = row
            priority = SOURCE_TYPE_PRIORITY.get(source_type, _UNKNOWN_SOURCE_PRIORITY)
            created = txn.created_at or _EPOCH
            return (priority, created)

        sorted_rows = sorted(rows, key=sort_key)
        _canonical_txn, canonical_bank_id, canonical_source = sorted_rows[0]

        for txn, bank_id, source_type in sorted_rows[1:]:
            compatible = _bank_compatible(bank_id, source_type, canonical_bank_id, canonical_source)
            if not compatible:
                continue  # різні конкретні банки — не дублікат, лишаємо обидва
            await session.execute(delete(Transaction).where(Transaction.id == txn.id))
            result.removed_ids.add(txn.id)
            result.removed_count += 1

    return result


async def find_possible_duplicate(
    session, user_id: str, time: int, amount: int, bank_id: str | None
) -> Transaction | None:
    """Шукає наявну транзакцію з тим самим часом і сумою з сумісного банку.

    Нічого не змінює в БД.

    Returns:
        Знайдену транзакцію або None.
    """
    rows_query = (
        select(Transaction, Account.bank_id)
        .join(Account, Transaction.account_id == Account.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.time == time,
            Transaction.amount == amount,
        )
    )
    rows = (await session.execute(rows_query)).all()
    for txn, existing_bank_id in rows:
        compatible = bank_id is None or existing_bank_id is None or bank_id == existing_bank_id
        if compatible:
            return txn
    return None


# --- План дедуплікації з урахуванням точності часу -------------------------
#
# - Дублікат — та сама подія з різних рахунків (кожен імпорт — окремий
#   рахунок). Рядки одного рахунку ніколи не зіставляються.
# - Пара порівнюється за найгрубішою спільною точністю: date -> місцева дата
#   Europe/Kyiv, minute -> хвилина, second -> точний time.
# - Однозначне лише 1:1 у своєму кошику. Якщо хоч з одного боку кошика >1
#   рядка (2 проти 3) — це лише у звіт (ambiguous), правило max по джерелах.
# - Лишається рядок з точнішим часом, далі пріоритет джерела, далі created_at.
#   Ручна категорія / loan_id переносяться на нього; різні ручні категорії
#   чи різні loan_id — конфлікт, пару не зливати.

KYIV = ZoneInfo("Europe/Kyiv")
PRECISION_RANK = {"date": 0, "minute": 1, "second": 2}
USER_DECIDED_METHODS = {"manual", "reimbursement"}
_UNCATEGORIZED = {None, "pending"}


@dataclass(frozen=True)
class DedupRow:
    """Транзакція з полями, потрібними для зіставлення."""
    id: str
    account_id: str
    source_type: str
    bank_id: str | None
    time: int
    amount: int
    precision: str  # 'date' | 'minute' | 'second'
    category: str | None = None
    category_method: str | None = None
    loan_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class DedupMatch:
    """Однозначна пара: рядок, що лишається, і дублікат; transfers — поля, які переносяться."""
    survivor_id: str
    duplicate_id: str
    kind: str  # точність, за якою зіставлено: 'date' | 'minute' | 'second'
    amount: int
    transfers: dict


@dataclass(frozen=True)
class AmbiguousGroup:
    """Група з кількома кандидатами з одного боку; лише для звіту."""
    local_date: str
    amount: int
    counts_by_account: dict
    would_remove: int  # сума − max по джерелах


@dataclass(frozen=True)
class DedupConflict:
    """Пара з різними ручними категоріями або різними loan_id; не зливається."""
    row_ids: tuple
    reason: str  # 'manual_category' | 'loan_id'


@dataclass
class DedupPlan:
    """План дедуплікації: однозначні пари, неоднозначні групи й конфлікти."""
    matches: list = field(default_factory=list)
    ambiguous: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)


def infer_account_precision(source_type: str, times: list[int]) -> str:
    """Визначає точність часу рахунку за правилом міграції fbef547587b9.

    monobank_api — 'second'; manual — 'date'. Для csv_import — за всіма рядками
    рахунку: усі о 00:00:00 за Києвом — 'date', усі з нульовими секундами —
    'minute', інакше 'second'.
    """
    if source_type == "monobank_api":
        return "second"
    if source_type == "manual":
        return "date"
    local = [datetime.fromtimestamp(t, KYIV) for t in times]
    if all((d.hour, d.minute, d.second) == (0, 0, 0) for d in local):
        return "date"
    if all(d.second == 0 for d in local):
        return "minute"
    return "second"


def _local_date(time: int) -> str:
    """Повертає дату за Києвом у форматі ISO."""
    return datetime.fromtimestamp(time, KYIV).date().isoformat()


def _key(row: DedupRow, precision: str):
    """Ключ кошика для порівняння з заданою точністю."""
    if precision == "date":
        return _local_date(row.time)
    if precision == "minute":
        return row.time // 60
    return row.time


def _coarser(a: str, b: str) -> str:
    """Повертає грубішу з двох точностей."""
    return a if PRECISION_RANK[a] <= PRECISION_RANK[b] else b


def _survivor_key(row: DedupRow):
    """Ключ вибору рядка, що лишається: точніший час, пріоритет джерела, created_at, id."""
    return (
        -PRECISION_RANK[row.precision],
        SOURCE_TYPE_PRIORITY.get(row.source_type, _UNKNOWN_SOURCE_PRIORITY),
        row.created_at or _EPOCH,
        row.id,
    )


def _transfers(survivor: DedupRow, duplicate: DedupRow) -> dict:
    """Поля дубліката (ручна категорія, категорія, loan_id), які переносяться на рядок, що лишається."""
    transfers = {}
    survivor_decided = survivor.category_method in USER_DECIDED_METHODS
    if duplicate.category_method in USER_DECIDED_METHODS and not survivor_decided:
        transfers.update(category=duplicate.category, category_method=duplicate.category_method)
    elif survivor.category in _UNCATEGORIZED and duplicate.category not in _UNCATEGORIZED:
        transfers.update(category=duplicate.category, category_method=duplicate.category_method)
    if duplicate.loan_id and not survivor.loan_id:
        transfers["loan_id"] = duplicate.loan_id
    return transfers


def _conflict_reason(component: list[DedupRow]) -> str | None:
    """Повертає причину конфлікту ('manual_category' або 'loan_id') або None."""
    decided = {r.category for r in component if r.category_method in USER_DECIDED_METHODS}
    if len(decided) > 1:
        return "manual_category"
    if len({r.loan_id for r in component if r.loan_id}) > 1:
        return "loan_id"
    return None


def match_duplicates(rows: list[DedupRow]) -> DedupPlan:
    """Будує план дедуплікації для рядків одного користувача.

    Args:
        rows: Транзакції користувача.

    Returns:
        DedupPlan з однозначними парами, неоднозначними групами й конфліктами.
    """
    plan = DedupPlan()
    groups: dict[tuple, list[DedupRow]] = {}
    for row in rows:
        groups.setdefault((_local_date(row.time), row.amount), []).append(row)

    for (local_date, amount), group in sorted(groups.items()):
        by_account: dict[str, list[DedupRow]] = {}
        for row in group:
            by_account.setdefault(row.account_id, []).append(row)
        if len(by_account) < 2:
            continue

        accounts = sorted(by_account)
        candidate_pairs = []
        ambiguous_rows: set[str] = set()
        ambiguous_accounts: set[str] = set()
        for i, acc_a in enumerate(accounts):
            for acc_b in accounts[i + 1:]:
                rows_a, rows_b = by_account[acc_a], by_account[acc_b]
                sample_a, sample_b = rows_a[0], rows_b[0]
                if not _bank_compatible(sample_a.bank_id, sample_a.source_type,
                                        sample_b.bank_id, sample_b.source_type):
                    continue
                precision = _coarser(min(rows_a, key=lambda r: PRECISION_RANK[r.precision]).precision,
                                     min(rows_b, key=lambda r: PRECISION_RANK[r.precision]).precision)
                buckets: dict = {}
                for row in rows_a + rows_b:
                    buckets.setdefault(_key(row, precision), []).append(row)
                for bucket in buckets.values():
                    in_a = [r for r in bucket if r.account_id == acc_a]
                    in_b = [r for r in bucket if r.account_id == acc_b]
                    if not in_a or not in_b:
                        continue
                    if len(in_a) == 1 and len(in_b) == 1:
                        candidate_pairs.append((in_a[0], in_b[0], precision))
                    else:
                        ambiguous_rows.update(r.id for r in bucket)
                        ambiguous_accounts.update((acc_a, acc_b))

        if ambiguous_accounts:
            counts = {acc: len(by_account[acc]) for acc in sorted(ambiguous_accounts)}
            plan.ambiguous.append(AmbiguousGroup(
                local_date=local_date, amount=amount, counts_by_account=counts,
                would_remove=sum(counts.values()) - max(counts.values()),
            ))

        # Компоненти зв'язності однозначних пар (union-find), без рядків,
        # що потрапили в неоднозначні кошики.
        parent: dict[str, str] = {}

        def find(x):
            """Корінь компоненти зі стисненням шляху."""
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        kinds: dict[frozenset, str] = {}
        row_by_id = {r.id: r for r in group}
        for a, b, precision in candidate_pairs:
            if a.id in ambiguous_rows or b.id in ambiguous_rows:
                continue
            parent[find(a.id)] = find(b.id)
            kinds[frozenset((a.id, b.id))] = precision

        components: dict[str, list[DedupRow]] = {}
        for row_id in parent:
            components.setdefault(find(row_id), []).append(row_by_id[row_id])

        for component in components.values():
            if len({r.account_id for r in component}) != len(component):
                # ланцюжок звів два рядки одного рахунку — не однозначно
                counts = {}
                for r in component:
                    counts[r.account_id] = counts.get(r.account_id, 0) + 1
                plan.ambiguous.append(AmbiguousGroup(
                    local_date=local_date, amount=amount, counts_by_account=counts,
                    would_remove=sum(counts.values()) - max(counts.values()),
                ))
                continue
            reason = _conflict_reason(component)
            if reason:
                plan.conflicts.append(DedupConflict(
                    row_ids=tuple(sorted(r.id for r in component)), reason=reason))
                continue
            survivor = min(component, key=_survivor_key)
            for duplicate in sorted(component, key=_survivor_key)[1:]:
                kind = kinds.get(frozenset((survivor.id, duplicate.id)),
                                 _coarser(survivor.precision, duplicate.precision))
                plan.matches.append(DedupMatch(
                    survivor_id=survivor.id, duplicate_id=duplicate.id, kind=kind,
                    amount=amount, transfers=_transfers(survivor, duplicate),
                ))

    return plan


async def plan_duplicate_removal(session, user_id: str) -> DedupPlan:
    """Будує план дедуплікації всієї історії користувача; лише читає БД.

    Returns:
        DedupPlan (див. match_duplicates).
    """
    result = await session.execute(
        select(Transaction, Account.bank_id, Account.source_type)
        .join(Account, Transaction.account_id == Account.id)
        .where(Transaction.user_id == user_id)
    )
    loaded = result.all()

    # Точність береться з колонки transactions.time_precision (NOT NULL).
    rows = [
        DedupRow(
            id=txn.id, account_id=txn.account_id, source_type=source_type, bank_id=bank_id,
            time=txn.time, amount=txn.amount, precision=txn.time_precision,
            category=txn.category, category_method=txn.category_method,
            loan_id=txn.loan_id, created_at=txn.created_at,
        )
        for txn, bank_id, source_type in loaded
    ]
    return match_duplicates(rows)
