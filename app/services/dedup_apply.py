"""
Застосування плану дедуплікації та відновлення з архіву.

- apply_dedup_plan: перераховує plan_duplicate_removal, переносить дублікати
  в transaction_dedup_archive (повна копія рядка) і видаляє їх з transactions.
- restore_dedup_run: повертає рядки запуску з архіву в transactions побайтово.

Функції НЕ роблять commit/rollback — це робить викликач (CLI-скрипти
scripts/data_fixes/dedup_apply.py і dedup_restore.py; тести працюють у
SAVEPOINT). Відмова -> DedupRefused, порушення інваріанта -> DedupInvariantError;
викликач мусить відкотити транзакцію.

Обробляються лише дублікати без зв'язків: дублікат з loan_id, з рядком
навчального пулу, з рішенням користувача про категорію (manual/reimbursement)
або з полями для перенесення (transfers) — відмова.
Усі суми й інваріанти — у копійках (Transaction.amount).
"""
import hashlib
from dataclasses import dataclass, field

from sqlalchemy import delete, func, insert, select, text, update

from app.models.account import Account
from app.models.category_training_example import CategoryTrainingExample
from app.models.loan import Loan
from app.models.transaction import Transaction
from app.models.transaction_dedup_archive import TransactionDedupArchive
from app.models.user import User
from app.services.dedup import DedupPlan, USER_DECIDED_METHODS, plan_duplicate_removal

ARCHIVE_REASON = "cross_source_duplicate"

# Пари (місцева дата за Києвом, сума в копійках) в ОДНОМУ акаунті — легітимні
# повтори, які застосування не має чіпати (інваріант: до == після).
_SAME_ACCOUNT_PAIRS = text("""
    WITH t AS (
        SELECT id, account_id, amount,
               (to_timestamp(time) AT TIME ZONE 'Europe/Kyiv')::date AS d
        FROM transactions WHERE user_id = :user_id
    )
    SELECT count(*) FROM t a JOIN t b
      ON a.account_id = b.account_id AND a.d = b.d AND a.amount = b.amount AND a.id < b.id
""")


class DedupRefused(Exception):
    """Застосування/відновлення відмовлено ДО будь-яких змін. `reason` — код."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


class DedupInvariantError(Exception):
    """Інваріант порушено ПІСЛЯ змін у транзакції — викликач мусить відкотити."""


@dataclass
class ApplyResult:
    """Результат apply_dedup_plan: відбиток плану, кількості, суми й результати перевірок."""
    run_id: str
    fingerprint: str
    planned: int
    applied: bool = False
    removed: int = 0
    outflow_kopecks: int = 0  # сума amount видалених витрат (від'ємна)
    inflow_kopecks: int = 0   # сума amount видалених надходжень (додатна)
    checks: dict = field(default_factory=dict)


@dataclass
class RestoreResult:
    """Результат restore_dedup_run: кількість рядків запуску й результати перевірок."""
    run_id: str
    rows: int
    restored: bool = False
    missing_kept: int = 0
    checks: dict = field(default_factory=dict)


def plan_fingerprint(plan: DedupPlan) -> str:
    """Рахує відбиток плану: sha256 відсортованих (duplicate_id, kept_id, match_kind).

    Відбиток із dry-run порівнюється з відбитком перед записом, щоб застосувати
    саме той план, який переглянув користувач.
    """
    lines = sorted(f"{m.duplicate_id}|{m.survivor_id}|{m.kind}" for m in plan.matches)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _planned_amounts(plan: DedupPlan) -> tuple[int, int]:
    """(сума витрат, сума надходжень) у копійках за планом."""
    outflow = sum(m.amount for m in plan.matches if m.amount < 0)
    inflow = sum(m.amount for m in plan.matches if m.amount > 0)
    return outflow, inflow


def _transaction_columns() -> list[str]:
    """Назви всіх колонок таблиці transactions."""
    return [c.name for c in Transaction.__table__.columns]


def _check_archive_schema() -> None:
    """Перевіряє, що архів містить кожну колонку transactions.

    Raises:
        DedupRefused: В архіві бракує колонок (код archive_schema_mismatch).
    """
    missing = set(_transaction_columns()) - {c.name for c in TransactionDedupArchive.__table__.columns}
    if missing:
        raise DedupRefused("archive_schema_mismatch", f"в архіві немає колонок {sorted(missing)}")


async def _count(session, stmt) -> int:
    """Виконує запит COUNT і повертає число."""
    return (await session.execute(stmt)).scalar_one()


async def apply_dedup_plan(
    session, user_id: str, run_id: str, expect_fingerprint: str | None = None, dry_run: bool = False,
) -> ApplyResult:
    """Переносить дублікати з плану в архів і видаляє їх з transactions.

    План перераховується в межах транзакції; таблиця transactions блокується
    від вставок до її завершення. Після змін перевіряються інваріанти (суми,
    кількості, повтори в межах одного рахунку). Коміт робить викликач.

    Args:
        session: Сесія БД.
        user_id: Користувач.
        run_id: Id запуску в архіві.
        expect_fingerprint: Відбиток плану з dry-run; при розбіжності — відмова.
        dry_run: Лише порахувати план і суми, нічого не змінюючи.

    Returns:
        ApplyResult.

    Raises:
        DedupRefused: План має конфлікти, неоднозначні групи, дублікати зі
            зв'язками, або відбиток не збігся.
        DedupInvariantError: Інваріант порушено після змін.
    """
    _check_archive_schema()
    if not dry_run:
        # Блокує вставки синку/імпорту до кінця транзакції; читання не блокує.
        await session.execute(text("LOCK TABLE transactions IN SHARE ROW EXCLUSIVE MODE"))

    plan = await plan_duplicate_removal(session, user_id)
    fingerprint = plan_fingerprint(plan)
    result = ApplyResult(run_id=run_id, fingerprint=fingerprint, planned=len(plan.matches))
    result.outflow_kopecks, result.inflow_kopecks = _planned_amounts(plan)

    if plan.conflicts:
        raise DedupRefused("conflicts", f"{len(plan.conflicts)} конфліктів у плані")
    if plan.ambiguous:
        raise DedupRefused("ambiguous_groups", f"{len(plan.ambiguous)} неоднозначних груп у плані")
    if not plan.matches:
        return result  # дублікатів немає

    duplicate_ids = [m.duplicate_id for m in plan.matches]
    kept_ids = [m.survivor_id for m in plan.matches]
    duplicates = {
        t.id: t for t in (await session.execute(
            select(Transaction).where(Transaction.id.in_(duplicate_ids))
        )).scalars()
    }

    # Лише дублікати без зв'язків.
    with_loan = [i for i in duplicate_ids if duplicates[i].loan_id]
    if with_loan:
        raise DedupRefused("duplicate_has_loan_id", f"{len(with_loan)} дублікатів з loan_id")
    with_examples = await _count(session, select(func.count()).select_from(CategoryTrainingExample)
                                 .where(CategoryTrainingExample.source_transaction_id.in_(duplicate_ids)))
    if with_examples:
        raise DedupRefused("duplicate_has_training_example", f"{with_examples} дублікатів у навчальному пулі")
    decided = [i for i in duplicate_ids if duplicates[i].category_method in USER_DECIDED_METHODS]
    if decided:
        raise DedupRefused("duplicate_has_user_decided_category", f"{len(decided)} дублікатів з manual/reimbursement")
    with_transfers = [m for m in plan.matches if m.transfers]
    if with_transfers:
        raise DedupRefused("transfers_not_supported", f"{len(with_transfers)} пар з перенесенням атрибутів")

    if dry_run:
        return result
    if expect_fingerprint is None:
        raise DedupRefused("fingerprint_required", "для запису потрібен відбиток із dry-run")
    if expect_fingerprint != fingerprint:
        raise DedupRefused("fingerprint_mismatch", "план змінився після dry-run")

    # --- стан до змін ---
    user_rows_before = await _count(session, select(func.count()).select_from(Transaction)
                                    .where(Transaction.user_id == user_id))
    other_rows_before = await _count(session, select(func.count()).select_from(Transaction)
                                     .where(Transaction.user_id != user_id))
    same_account_before = await _count(session, _SAME_ACCOUNT_PAIRS.bindparams(user_id=user_id))

    # --- архів + видалення ---
    columns = _transaction_columns()
    archive_rows = [
        {
            **{c: getattr(duplicates[m.duplicate_id], c) for c in columns},
            "run_id": run_id,
            "reason": ARCHIVE_REASON,
            "match_kind": m.kind,
            "kept_id": m.survivor_id,
            "transfers": None,
        }
        for m in plan.matches
    ]
    await session.execute(insert(TransactionDedupArchive), archive_rows)
    await session.execute(delete(Transaction).where(Transaction.id.in_(duplicate_ids)))

    # --- інваріанти (порушення -> виняток, викликач відкочує) ---
    user_rows_after = await _count(session, select(func.count()).select_from(Transaction)
                                   .where(Transaction.user_id == user_id))
    other_rows_after = await _count(session, select(func.count()).select_from(Transaction)
                                    .where(Transaction.user_id != user_id))
    archived = (await session.execute(
        select(TransactionDedupArchive.amount).where(TransactionDedupArchive.run_id == run_id)
    )).scalars().all()
    archived_outflow = sum(a for a in archived if a < 0)
    archived_inflow = sum(a for a in archived if a > 0)
    planned_outflow, planned_inflow = _planned_amounts(plan)
    still_present = await _count(session, select(func.count()).select_from(Transaction)
                                 .where(Transaction.id.in_(duplicate_ids)))
    kept_present = await _count(session, select(func.count()).select_from(Transaction)
                                .where(Transaction.id.in_(kept_ids)))
    same_account_after = await _count(session, _SAME_ACCOUNT_PAIRS.bindparams(user_id=user_id))
    replan = await plan_duplicate_removal(session, user_id)

    checks = {
        "user_rows_delta": (user_rows_before - user_rows_after, len(duplicate_ids)),
        "other_users_rows": (other_rows_after, other_rows_before),
        "archived_rows": (len(archived), len(duplicate_ids)),
        "archived_outflow_kopecks": (archived_outflow, planned_outflow),
        "archived_inflow_kopecks": (archived_inflow, planned_inflow),
        "duplicates_still_present": (still_present, 0),
        "kept_present": (kept_present, len(set(kept_ids))),
        "same_account_pairs": (same_account_after, same_account_before),
        "replan_matches": (len(replan.matches), 0),
        "replan_conflicts": (len(replan.conflicts), 0),
    }
    failed = {name: pair for name, pair in checks.items() if pair[0] != pair[1]}
    if failed:
        raise DedupInvariantError(f"порушено інваріанти (отримано, очікувалось): {failed}")

    result.applied = True
    result.removed = len(duplicate_ids)
    result.checks = checks
    return result


async def restore_dedup_run(
    session, run_id: str, allow_missing_kept: bool = False, dry_run: bool = False,
) -> RestoreResult:
    """Повертає рядки запуску з архіву в transactions без змін.

    Коміт робить викликач.

    Args:
        session: Сесія БД.
        run_id: Id запуску.
        allow_missing_kept: Відновлювати, навіть якщо рядок, що лишався при
            застосуванні, уже видалено.
        dry_run: Лише перевірити, нічого не змінюючи.

    Returns:
        RestoreResult.

    Raises:
        DedupRefused: Запуск не знайдено або вже відновлено, id уже є в
            transactions, або зникли пов'язані записи (рядок, що лишався,
            користувач, рахунок, позика).
        DedupInvariantError: Інваріант порушено після змін.
    """
    _check_archive_schema()
    archived = (await session.execute(
        select(TransactionDedupArchive).where(TransactionDedupArchive.run_id == run_id)
    )).scalars().all()
    if not archived:
        raise DedupRefused("run_not_found", run_id)
    result = RestoreResult(run_id=run_id, rows=len(archived))
    if any(a.restored_at is not None for a in archived):
        raise DedupRefused("already_restored", run_id)

    ids = [a.id for a in archived]
    collisions = await _count(session, select(func.count()).select_from(Transaction).where(Transaction.id.in_(ids)))
    if collisions:
        raise DedupRefused("id_collision", f"{collisions} id вже є в transactions")

    kept_ids = {a.kept_id for a in archived}
    kept_present = await _count(session, select(func.count()).select_from(Transaction)
                                .where(Transaction.id.in_(kept_ids)))
    result.missing_kept = len(kept_ids) - kept_present
    if result.missing_kept and not allow_missing_kept:
        raise DedupRefused("kept_missing", f"{result.missing_kept} збережених копій зникли")

    users = {a.user_id for a in archived}
    accounts = {a.account_id for a in archived}
    loans = {a.loan_id for a in archived if a.loan_id}
    if await _count(session, select(func.count()).select_from(User).where(User.id.in_(users))) != len(users):
        raise DedupRefused("user_missing")
    if await _count(session, select(func.count()).select_from(Account).where(Account.id.in_(accounts))) != len(accounts):
        raise DedupRefused("account_missing")
    if loans and await _count(session, select(func.count()).select_from(Loan).where(Loan.id.in_(loans))) != len(loans):
        raise DedupRefused("loan_missing")

    if dry_run:
        return result

    columns = _transaction_columns()
    await session.execute(insert(Transaction), [{c: getattr(a, c) for c in columns} for a in archived])
    await session.execute(
        update(TransactionDedupArchive)
        .where(TransactionDedupArchive.run_id == run_id)
        .values(restored_at=func.clock_timestamp())
    )

    restored_present = await _count(session, select(func.count()).select_from(Transaction)
                                    .where(Transaction.id.in_(ids)))
    marked = await _count(session, select(func.count()).select_from(TransactionDedupArchive)
                          .where(TransactionDedupArchive.run_id == run_id,
                                 TransactionDedupArchive.restored_at.isnot(None)))
    checks = {
        "restored_present": (restored_present, len(ids)),
        "archive_marked_restored": (marked, len(ids)),
    }
    failed = {name: pair for name, pair in checks.items() if pair[0] != pair[1]}
    if failed:
        raise DedupInvariantError(f"порушено інваріанти відновлення (отримано, очікувалось): {failed}")
    result.restored = True
    result.checks = checks
    return result
