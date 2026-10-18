"""
Тести застосування плану дедуплікації з архівом і відновлення (app/services/dedup_apply.py).

Сервісні функції не комітять, тест працює в SAVEPOINT. Моків немає, крім
одного monkeypatch для перевірки інваріанта сум.
"""
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

import app.services.dedup_apply as dedup_apply
from app.models.account import Account
from app.models.category_training_example import CategoryTrainingExample
from app.models.loan import Loan
from app.models.transaction import Transaction
from app.models.transaction_dedup_archive import TransactionDedupArchive
from app.services.dedup_apply import (
    DedupInvariantError, DedupRefused, apply_dedup_plan, restore_dedup_run,
)

KYIV = ZoneInfo("Europe/Kyiv")
TXN_COLUMNS = [c.name for c in Transaction.__table__.columns]


def _kyiv(y, m, d, hh=0, mm=0, ss=0):
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=KYIV).timestamp())


@pytest.fixture
async def accounts(db_session, real_user):
    pdf = Account(id=f"csv-pdf-{uuid.uuid4()}", user_id=real_user.id, source_type="csv_import", bank_id="privatbank")
    xlsx = Account(id=f"csv-xlsx-{uuid.uuid4()}", user_id=real_user.id, source_type="csv_import", bank_id=None)
    db_session.add_all([pdf, xlsx])
    await db_session.flush()
    return pdf, xlsx


async def _txn(db_session, user, account, time, amount, precision, **extra):
    values = dict(
        id=str(uuid.uuid4()), user_id=user.id, account_id=account.id, time=time,
        time_precision=precision, amount=amount, amount_uah=amount // 100,
        description="Тестова операція", category="pending", category_method="pending",
    )
    values.update(extra)
    txn = Transaction(**values)
    db_session.add(txn)
    await db_session.flush()
    return txn


async def _pair(db_session, user, accounts, day=(2025, 4, 10), amount=-12300, hhmmss=(14, 23, 51), **pdf_extra):
    """Створює дві копії однієї події: з PDF (лише дата, 00:00 за Києвом) і з XLSX (до секунди)."""
    pdf, xlsx = accounts
    dup = await _txn(db_session, user, pdf, _kyiv(*day), amount, "date", **pdf_extra)
    kept = await _txn(db_session, user, xlsx, _kyiv(*day, *hhmmss), amount, "second")
    return dup, kept


async def _snapshot(db_session, user_id):
    rows = (await db_session.execute(
        select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.id)
    )).scalars().all()
    return [tuple(getattr(r, c) for c in TXN_COLUMNS) for r in rows]


async def _count(db_session, model, *where):
    return (await db_session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _apply(db_session, user_id, run_id=None):
    run_id = run_id or str(uuid.uuid4())
    dry = await apply_dedup_plan(db_session, user_id, run_id, dry_run=True)
    return await apply_dedup_plan(db_session, user_id, run_id, expect_fingerprint=dry.fingerprint)


# --- Схема ------------------------------------------------------------------

def test_archive_columns_are_superset_of_transactions_columns():
    archive_columns = {c.name for c in TransactionDedupArchive.__table__.columns}
    assert set(TXN_COLUMNS) <= archive_columns


# --- Застосування ------------------------------------------------------------

async def test_dry_run_changes_nothing(db_session, real_user, accounts):
    await _pair(db_session, real_user, accounts)
    before = await _snapshot(db_session, real_user.id)

    result = await apply_dedup_plan(db_session, real_user.id, str(uuid.uuid4()), dry_run=True)

    assert result.planned == 1 and result.applied is False
    assert len(result.fingerprint) == 64
    assert await _snapshot(db_session, real_user.id) == before
    assert await _count(db_session, TransactionDedupArchive, TransactionDedupArchive.user_id == real_user.id) == 0


async def test_apply_archives_full_copy_and_deletes_duplicate(db_session, real_user, accounts):
    dup, kept = await _pair(db_session, real_user, accounts)
    original = {c: getattr(dup, c) for c in TXN_COLUMNS}
    run_id = str(uuid.uuid4())

    result = await _apply(db_session, real_user.id, run_id)

    assert result.applied and result.removed == 1
    assert result.outflow_kopecks == -12300 and result.inflow_kopecks == 0
    assert await db_session.get(Transaction, kept.id) is not None
    assert await _count(db_session, Transaction, Transaction.id == dup.id) == 0
    archived = (await db_session.execute(
        select(TransactionDedupArchive).where(TransactionDedupArchive.run_id == run_id)
    )).scalars().all()
    assert len(archived) == 1
    row = archived[0]
    assert {c: getattr(row, c) for c in TXN_COLUMNS} == original
    assert (row.kept_id, row.match_kind, row.reason, row.transfers, row.restored_at) == (
        kept.id, "date", "cross_source_duplicate", None, None)
    assert row.archived_at is not None


async def test_apply_then_restore_gives_identical_rows(db_session, real_user, accounts):
    await _pair(db_session, real_user, accounts, day=(2025, 4, 10), amount=-12300)
    await _pair(db_session, real_user, accounts, day=(2025, 5, 3), amount=207700, hhmmss=(9, 1, 2))
    before = await _snapshot(db_session, real_user.id)
    run_id = str(uuid.uuid4())

    await _apply(db_session, real_user.id, run_id)
    assert len(await _snapshot(db_session, real_user.id)) == len(before) - 2
    restored = await restore_dedup_run(db_session, run_id)

    assert restored.restored and restored.rows == 2
    user_id = real_user.id  # expire_all() нижче позначає застарілим і сам real_user
    db_session.expire_all()  # перечитати рядки з БД, а не з identity map
    assert await _snapshot(db_session, user_id) == before
    assert await _count(db_session, TransactionDedupArchive, TransactionDedupArchive.run_id == run_id,
                        TransactionDedupArchive.restored_at.isnot(None)) == 2


async def test_apply_does_not_touch_same_account_pairs(db_session, real_user, accounts):
    pdf, xlsx = accounts
    dup, kept = await _pair(db_session, real_user, accounts)
    # Дві однакові покупки в одному файлі + два зняття в банкоматі з різними секундами.
    same_file = [await _txn(db_session, real_user, xlsx, _kyiv(2025, 4, 11, 10, 0, 1), -5000, "second") for _ in range(2)]
    atm = [await _txn(db_session, real_user, xlsx, _kyiv(2025, 7, 3, 11, 20, s), -1000000, "second") for s in (1, 49)]

    result = await _apply(db_session, real_user.id)

    assert result.removed == 1
    assert result.checks["same_account_pairs"] == (2, 2)
    for txn in [kept, *same_file, *atm]:
        assert await _count(db_session, Transaction, Transaction.id == txn.id) == 1
    assert await _count(db_session, Transaction, Transaction.id == dup.id) == 0


async def test_repeat_apply_is_noop(db_session, real_user, accounts):
    await _pair(db_session, real_user, accounts)
    await _apply(db_session, real_user.id)
    before = await _snapshot(db_session, real_user.id)

    result = await _apply(db_session, real_user.id)

    assert result.planned == 0 and result.applied is False and result.removed == 0
    assert await _snapshot(db_session, real_user.id) == before


# --- Відмови (нічого не змінюється) ------------------------------------------

async def _assert_refused(db_session, user_id, reason, expect_fingerprint="from-dry-run"):
    before = await _snapshot(db_session, user_id)
    run_id = str(uuid.uuid4())
    with pytest.raises(DedupRefused) as exc_info:
        if expect_fingerprint == "from-dry-run":
            await _apply(db_session, user_id, run_id)
        else:
            await apply_dedup_plan(db_session, user_id, run_id, expect_fingerprint=expect_fingerprint)
    assert exc_info.value.reason == reason
    assert await _snapshot(db_session, user_id) == before
    assert await _count(db_session, TransactionDedupArchive, TransactionDedupArchive.run_id == run_id) == 0


async def test_refuses_conflict(db_session, real_user, accounts):
    pdf, xlsx = accounts
    await _txn(db_session, real_user, pdf, _kyiv(2025, 4, 10), -12300, "date",
               category="Продукти", category_method="manual")
    await _txn(db_session, real_user, xlsx, _kyiv(2025, 4, 10, 14, 23, 51), -12300, "second",
               category="Кафе та ресторани", category_method="manual")

    await _assert_refused(db_session, real_user.id, "conflicts")


async def test_refuses_ambiguous_group(db_session, real_user, accounts):
    pdf, xlsx = accounts
    for _ in range(2):
        await _txn(db_session, real_user, pdf, _kyiv(2025, 4, 10), -50000, "date")
    for hh in (9, 13, 20):
        await _txn(db_session, real_user, xlsx, _kyiv(2025, 4, 10, hh, 5, 7), -50000, "second")

    await _assert_refused(db_session, real_user.id, "ambiguous_groups")


async def test_refuses_when_plan_changed_after_dry_run(db_session, real_user, accounts):
    await _pair(db_session, real_user, accounts)
    dry = await apply_dedup_plan(db_session, real_user.id, str(uuid.uuid4()), dry_run=True)
    # Між dry-run і записом з'явилася ще одна пара.
    await _pair(db_session, real_user, accounts, day=(2025, 6, 1), amount=-4500)

    await _assert_refused(db_session, real_user.id, "fingerprint_mismatch", expect_fingerprint=dry.fingerprint)


async def test_refuses_apply_without_fingerprint(db_session, real_user, accounts):
    await _pair(db_session, real_user, accounts)

    await _assert_refused(db_session, real_user.id, "fingerprint_required", expect_fingerprint=None)


async def test_refuses_duplicate_with_loan_id(db_session, real_user, accounts):
    loan = Loan(id=str(uuid.uuid4()), user_id=real_user.id, borrower_name="Тест", principal_amount=500,
                remaining_amount=500, issued_at=date(2025, 1, 1), status="not_repaid")
    db_session.add(loan)
    await db_session.flush()
    await _pair(db_session, real_user, accounts, loan_id=loan.id)

    await _assert_refused(db_session, real_user.id, "duplicate_has_loan_id")


async def test_refuses_duplicate_with_training_example(db_session, real_user, accounts):
    dup, _ = await _pair(db_session, real_user, accounts)
    db_session.add(CategoryTrainingExample(
        id=str(uuid.uuid4()), user_id=real_user.id, source_transaction_id=dup.id,
        description="Тестова операція", category="Продукти", source="keyword",
    ))
    await db_session.flush()

    await _assert_refused(db_session, real_user.id, "duplicate_has_training_example")


async def test_refuses_duplicate_with_reimbursement(db_session, real_user, accounts):
    await _pair(db_session, real_user, accounts, amount=25000,
                category="Кафе та ресторани", category_method="reimbursement")

    await _assert_refused(db_session, real_user.id, "duplicate_has_user_decided_category")


# --- Інваріант сум --------------------------------------------------------

async def test_invariant_violation_raises_after_changes(db_session, real_user, accounts, monkeypatch):
    await _pair(db_session, real_user, accounts)
    dry = await apply_dedup_plan(db_session, real_user.id, str(uuid.uuid4()), dry_run=True)
    # План «очікує» інші суми, ніж реально заархівовано -> інваріант сум.
    monkeypatch.setattr(dedup_apply, "_planned_amounts", lambda plan: (-1, 0))

    with pytest.raises(DedupInvariantError) as exc_info:
        await apply_dedup_plan(db_session, real_user.id, str(uuid.uuid4()), expect_fingerprint=dry.fingerprint)

    assert "archived_outflow_kopecks" in str(exc_info.value)


# --- Відновлення: відмови ------------------------------------------------

async def test_restore_refuses_unknown_run(db_session):
    with pytest.raises(DedupRefused) as exc_info:
        await restore_dedup_run(db_session, f"no-such-run-{uuid.uuid4()}")
    assert exc_info.value.reason == "run_not_found"


async def test_restore_refuses_id_collision(db_session, real_user, accounts):
    dup, _ = await _pair(db_session, real_user, accounts)
    run_id = str(uuid.uuid4())
    await _apply(db_session, real_user.id, run_id)
    # Рядок з тим самим id знову з'явився в transactions.
    db_session.add(Transaction(id=dup.id, user_id=real_user.id, account_id=accounts[0].id, time=dup.time,
                               time_precision="date", amount=dup.amount, amount_uah=dup.amount // 100))
    await db_session.flush()

    with pytest.raises(DedupRefused) as exc_info:
        await restore_dedup_run(db_session, run_id)
    assert exc_info.value.reason == "id_collision"


async def test_restore_refuses_missing_kept_unless_allowed(db_session, real_user, accounts):
    dup, kept = await _pair(db_session, real_user, accounts)
    run_id = str(uuid.uuid4())
    await _apply(db_session, real_user.id, run_id)
    await db_session.delete(await db_session.get(Transaction, kept.id))
    await db_session.flush()

    with pytest.raises(DedupRefused) as exc_info:
        await restore_dedup_run(db_session, run_id)
    assert exc_info.value.reason == "kept_missing"

    result = await restore_dedup_run(db_session, run_id, allow_missing_kept=True)
    assert result.restored and result.missing_kept == 1
    assert await _count(db_session, Transaction, Transaction.id == dup.id) == 1


async def test_restore_refuses_second_restore(db_session, real_user, accounts):
    await _pair(db_session, real_user, accounts)
    run_id = str(uuid.uuid4())
    await _apply(db_session, real_user.id, run_id)
    await restore_dedup_run(db_session, run_id)

    with pytest.raises(DedupRefused) as exc_info:
        await restore_dedup_run(db_session, run_id)
    assert exc_info.value.reason == "already_restored"


async def test_restore_dry_run_changes_nothing(db_session, real_user, accounts):
    await _pair(db_session, real_user, accounts)
    run_id = str(uuid.uuid4())
    await _apply(db_session, real_user.id, run_id)
    before = await _snapshot(db_session, real_user.id)

    result = await restore_dedup_run(db_session, run_id, dry_run=True)

    assert result.restored is False and result.rows == 1
    assert await _snapshot(db_session, real_user.id) == before
