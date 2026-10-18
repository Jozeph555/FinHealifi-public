"""
Тести інкрементальної синхронізації Monobank: monobank.compute_chunks_since,
sync.run_periodic_sync і перевірки токена в mono.start_sync.

Мережевий виклик get_statement_chunk замінено заглушкою. run_periodic_sync
отримує db_session. Він обробляє всі рахунки monobank_api у БД, тож заглушки
повертають транзакції лише для рахунків, створених тестом, і [] для решти.
"""
import time
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select

import app.services.sync as sync_module
from app.core.security import encrypt_token
from app.models.account import Account
from app.models.transaction import Transaction
from app.services.monobank import CHUNK_SECONDS, INCREMENTAL_SYNC_OVERLAP_SECONDS, compute_chunks_since
from app.services.sync import run_periodic_sync
from app.api.mono import start_sync, SyncStartRequest


@pytest.fixture(autouse=True)
def _skip_auto_categorization(monkeypatch):
    # run_periodic_sync наприкінці викликає categorize_all_transactions(), яка
    # відкриває власну сесію поза SAVEPOINT тесту; тут вона замінена заглушкою.
    async def _noop():
        return {"skipped_for_test": True}

    monkeypatch.setattr(sync_module, "categorize_all_transactions", _noop)


@pytest.fixture(autouse=True)
def _skip_rate_limit_sleep(monkeypatch):
    # Пауза RATE_LIMIT_SLEEP_SECONDS (61 с) між запитами в тестах вимкнена.
    async def _noop_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(sync_module.asyncio, "sleep", _noop_sleep)


async def _make_account(db_session, user_id, token=None, encrypted_token=None, last_synced_at=None):
    account = Account(
        id=f"acc-{uuid.uuid4()}",
        user_id=user_id,
        source_type="monobank_api",
        mono_token_encrypted=encrypted_token or encrypt_token(token or f"tok-{uuid.uuid4()}"),
        bank_name="monobank",
        last_synced_at=last_synced_at,
    )
    db_session.add(account)
    await db_session.flush()
    return account


def _fake_chunk_response(account_id: str) -> dict:
    return {
        "id": f"mono-{account_id}-{uuid.uuid4()}",
        "time": int(time.time()),
        "description": "Тестова транзакція",
        "mcc": 5411,
        "amount": -10000,
        "currencyCode": 980,
        "cashbackAmount": 0,
        "balance": 1000000,
        "hold": False,
    }


# --- compute_chunks_since (чисті unit-тести, без БД/мережі) -----------------

def test_compute_chunks_since_recent_cursor_gives_one_window():
    now = int(time.time())
    since = now - 3600  # годину тому — набагато менше за CHUNK_SECONDS (31 день)
    windows = compute_chunks_since(since, now)
    assert len(windows) == 1
    assert windows[0] == (since, now)


def test_compute_chunks_since_old_cursor_gives_multiple_windows():
    now = int(time.time())
    since = now - 100 * 86400  # 100 днів тому
    windows = compute_chunks_since(since, now)
    assert len(windows) > 1
    # Найновіше вікно перше, найстаріше — останнє; без розривів/накладок.
    assert windows[0][1] == now
    assert windows[-1][0] == since
    for (s, e) in windows:
        assert e - s <= CHUNK_SECONDS


# --- run_periodic_sync: курсор і ізоляція помилок ---------------------------

async def test_run_periodic_sync_updates_cursor_and_saves_transactions(db_session, real_user):
    account = await _make_account(db_session, real_user.id)

    async def fake_get_statement_chunk(token, account_id, start, end):
        if account_id != account.id:
            return []
        return [_fake_chunk_response(account_id)]

    original = sync_module.get_statement_chunk
    sync_module.get_statement_chunk = fake_get_statement_chunk
    try:
        await run_periodic_sync(session=db_session)
    finally:
        sync_module.get_statement_chunk = original

    await db_session.refresh(account)
    assert account.last_sync_status == "success"
    assert account.last_sync_error is None
    assert account.last_synced_at is not None

    saved = (
        await db_session.execute(select(Transaction).where(Transaction.account_id == account.id))
    ).scalars().all()
    assert len(saved) == 1


async def test_run_periodic_sync_isolates_failures_between_accounts(db_session, real_user):
    good_account = await _make_account(db_session, real_user.id)
    bad_account = await _make_account(db_session, real_user.id)

    async def fake_get_statement_chunk(token, account_id, start, end):
        if account_id == bad_account.id:
            raise RuntimeError("Monobank API впав (тест)")
        if account_id == good_account.id:
            return [_fake_chunk_response(account_id)]
        return []

    original = sync_module.get_statement_chunk
    sync_module.get_statement_chunk = fake_get_statement_chunk
    try:
        await run_periodic_sync(session=db_session)
    finally:
        sync_module.get_statement_chunk = original

    await db_session.refresh(good_account)
    await db_session.refresh(bad_account)

    assert good_account.last_sync_status == "success", (
        "Падіння bad_account не мало б завадити good_account успішно синхронізуватись"
    )
    assert bad_account.last_sync_status == "failed"
    assert "Monobank API впав" in bad_account.last_sync_error

    good_saved = (
        await db_session.execute(select(Transaction).where(Transaction.account_id == good_account.id))
    ).scalars().all()
    assert len(good_saved) == 1


async def test_run_periodic_sync_new_account_uses_bounded_default_window(db_session, real_user):
    """Рахунок без last_synced_at отримує лише останні CHUNK_SECONDS, а не всю історію."""
    account = await _make_account(db_session, real_user.id, last_synced_at=None)
    seen_windows = []

    async def fake_get_statement_chunk(token, account_id, start, end):
        if account_id == account.id:
            seen_windows.append((start, end))
        return []

    original = sync_module.get_statement_chunk
    sync_module.get_statement_chunk = fake_get_statement_chunk
    try:
        await run_periodic_sync(session=db_session)
    finally:
        sync_module.get_statement_chunk = original

    assert len(seen_windows) == 1
    start, end = seen_windows[0]
    assert end - start <= CHUNK_SECONDS + 5  # невеликий запас на час виконання тесту


async def test_run_periodic_sync_existing_cursor_uses_overlap_margin(db_session, real_user):
    last_synced = datetime.now(timezone.utc)
    account = await _make_account(db_session, real_user.id, last_synced_at=last_synced)
    seen_windows = []

    async def fake_get_statement_chunk(token, account_id, start, end):
        if account_id == account.id:
            seen_windows.append((start, end))
        return []

    original = sync_module.get_statement_chunk
    sync_module.get_statement_chunk = fake_get_statement_chunk
    try:
        await run_periodic_sync(session=db_session)
    finally:
        sync_module.get_statement_chunk = original

    assert len(seen_windows) == 1
    start, _ = seen_windows[0]
    expected_start = int(last_synced.timestamp()) - INCREMENTAL_SYNC_OVERLAP_SECONDS
    assert abs(start - expected_start) <= 5


# --- start_sync: рахунки одного job мають один токен ------------------------

async def test_start_sync_rejects_accounts_from_different_tokens(db_session, real_user):
    account_a = await _make_account(db_session, real_user.id, token="token-a")
    account_b = await _make_account(db_session, real_user.id, token="token-b")

    with pytest.raises(HTTPException) as exc_info:
        await start_sync(
            SyncStartRequest(account_ids=[account_a.id, account_b.id], months=1),
            BackgroundTasks(),
            current_user=real_user,
            db=db_session,
        )
    assert exc_info.value.status_code == 422


async def test_start_sync_allows_accounts_from_same_token(db_session, real_user):
    # Fernet-шифрування недетерміноване (випадковий IV): той самий токен,
    # зашифрований двічі, дає різні рядки. connect_monobank шифрує токен один раз
    # і записує той самий рядок у всі рахунки — тест робить так само.
    shared_encrypted = encrypt_token("shared-token")
    account_a = await _make_account(db_session, real_user.id, encrypted_token=shared_encrypted)
    account_b = await _make_account(db_session, real_user.id, encrypted_token=shared_encrypted)

    result = await start_sync(
        SyncStartRequest(account_ids=[account_a.id, account_b.id], months=1),
        BackgroundTasks(),
        current_user=real_user,
        db=db_session,
    )
    assert result["status"] == "pending"
