"""
Тести плану дедуплікації з урахуванням точності часу (dedup.match_duplicates,
plan_duplicate_removal, infer_account_precision). Лише читання.

- Дублікат — та сама подія з різних рахунків; рядки одного рахунку не
  зіставляються (дві однакові покупки в одному файлі — різні транзакції).
- Пара порівнюється за найгрубішою спільною точністю: date -> місцева дата
  (Europe/Kyiv), minute -> хвилина, second -> точний time.
- Зіставлення однозначне лише 1:1 у своєму кошику; 2 проти 3 — неоднозначна
  група у звіті, would_remove за правилом максимуму по джерелах.
- Лишається рядок з точнішим часом, далі за пріоритетом джерела й created_at;
  ручна категорія й loan_id переносяться на нього; різні ручні категорії чи
  різні loan_id — конфлікт.
- infer_account_precision: monobank_api -> second, manual -> date,
  csv_import — за всіма часами рахунку.
"""
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.models.account import Account
from app.models.transaction import Transaction
from app.services import dedup

KYIV = ZoneInfo("Europe/Kyiv")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _kyiv(y, m, d, hh=0, mm=0, ss=0):
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=KYIV).timestamp())


def _row(account_id, time, amount=-50000, precision="second", source_type="csv_import",
         bank_id="privatbank", category="pending", category_method="pending",
         loan_id=None, created_minutes=0):
    return dedup.DedupRow(
        id=str(uuid.uuid4()),
        account_id=account_id,
        source_type=source_type,
        bank_id=bank_id,
        time=time,
        amount=amount,
        precision=precision,
        category=category,
        category_method=category_method,
        loan_id=loan_id,
        created_at=T0 + timedelta(minutes=created_minutes),
    )


# --- Одне джерело: нічого не зіставляється -------------------------------

def test_two_identical_purchases_in_one_file_are_not_matched():
    a = _row("csv-A", _kyiv(2026, 3, 2, 14, 0, 5))
    b = _row("csv-A", _kyiv(2026, 3, 2, 14, 0, 5))

    result = dedup.match_duplicates([a, b])

    assert result.matches == []
    assert result.ambiguous == []
    assert result.conflicts == []


def test_atm_withdrawals_same_account_different_seconds_not_matched():
    a = _row("mono-1", _kyiv(2026, 7, 3, 11, 20, 1), amount=-1000000, source_type="monobank_api", bank_id="monobank")
    b = _row("mono-1", _kyiv(2026, 7, 3, 11, 20, 49), amount=-1000000, source_type="monobank_api", bank_id="monobank")

    result = dedup.match_duplicates([a, b])

    assert result.matches == []
    assert result.ambiguous == []


# --- Різні джерела ----------------------------------------------------------

def test_same_file_imported_twice_matches_one_to_one():
    first = [_row("csv-A", _kyiv(2026, 3, 2, 9, 0, 1)), _row("csv-A", _kyiv(2026, 3, 5, 18, 30, 0))]
    second = [_row("csv-B", r.time, created_minutes=10) for r in first]

    result = dedup.match_duplicates(first + second)

    pairs = {(m.survivor_id, m.duplicate_id) for m in result.matches}
    assert pairs == {(first[0].id, second[0].id), (first[1].id, second[1].id)}
    assert {m.kind for m in result.matches} == {"second"}


def test_exact_match_monobank_vs_csv_keeps_monobank():
    mono = _row("mono-1", _kyiv(2026, 3, 2, 9, 0, 1), source_type="monobank_api", bank_id=None, created_minutes=5)
    csv = _row("csv-A", mono.time, bank_id=None)

    result = dedup.match_duplicates([csv, mono])

    assert [(m.survivor_id, m.duplicate_id) for m in result.matches] == [(mono.id, csv.id)]


def test_incompatible_concrete_banks_not_matched():
    mono = _row("mono-1", _kyiv(2026, 3, 2, 9, 0, 1), source_type="monobank_api", bank_id="monobank")
    csv = _row("csv-A", mono.time, bank_id="privatbank")

    assert dedup.match_duplicates([mono, csv]).matches == []


def test_two_vs_three_is_ambiguous_and_report_only():
    day = [_kyiv(2026, 4, 10)] * 2
    a = [_row("csv-pdf", t, precision="date") for t in day]
    b = [_row("csv-xlsx", _kyiv(2026, 4, 10, h, 5, 7)) for h in (9, 13, 20)]

    result = dedup.match_duplicates(a + b)

    assert result.matches == []
    assert len(result.ambiguous) == 1
    group = result.ambiguous[0]
    assert group.local_date == "2026-04-10"
    assert group.amount == -50000
    assert group.counts_by_account == {"csv-pdf": 2, "csv-xlsx": 3}
    assert group.would_remove == 2  # max по джерелах: лишаються 3 події


def test_three_vs_two_is_ambiguous_regardless_of_source_order():
    # Те саме, але більша кількість — з боку джерела з грубішою точністю, і
    # рядки подані в іншому порядку: результат не залежить від порядку.
    a = [_row("csv-pdf", _kyiv(2026, 4, 10), precision="date") for _ in range(3)]
    b = [_row("csv-xlsx", _kyiv(2026, 4, 10, h, 5, 7)) for h in (9, 20)]

    result = dedup.match_duplicates(b + a)

    assert result.matches == []
    assert len(result.ambiguous) == 1
    assert result.ambiguous[0].counts_by_account == {"csv-pdf": 3, "csv-xlsx": 2}
    assert result.ambiguous[0].would_remove == 2


@pytest.mark.xfail(strict=True, reason="залежить від кроку 2 (external_ref)")
def test_different_cards_of_same_bank_same_date_and_amount_not_matched():
    # Дві виписки різних карток одного банку (обидві csv_import, тож банк-сумісні)
    # без номерів карток не розрізнити.
    card_1111 = _row("csv-card-1111", _kyiv(2026, 4, 10), precision="date")
    card_2222 = _row("csv-card-2222", _kyiv(2026, 4, 10, 14, 23, 51))

    assert dedup.match_duplicates([card_1111, card_2222]).matches == []


# --- Різна точність ---------------------------------------------------------

def test_date_vs_second_precision_matches_and_keeps_precise_row():
    pdf = _row("csv-pdf", _kyiv(2026, 4, 10), precision="date")
    xlsx = _row("csv-xlsx", _kyiv(2026, 4, 10, 14, 23, 51), created_minutes=30)

    result = dedup.match_duplicates([pdf, xlsx])

    assert len(result.matches) == 1
    match = result.matches[0]
    assert (match.survivor_id, match.duplicate_id) == (xlsx.id, pdf.id)
    assert match.kind == "date"


def test_minute_vs_second_precision():
    pdf = _row("csv-pdf", _kyiv(2026, 4, 10, 12, 0), precision="minute")
    same_minute = _row("csv-xlsx", _kyiv(2026, 4, 10, 12, 0, 37))
    next_minute = _row("csv-other", _kyiv(2026, 4, 10, 12, 1, 5))

    assert [m.kind for m in dedup.match_duplicates([pdf, same_minute]).matches] == ["minute"]
    assert dedup.match_duplicates([pdf, next_minute]).matches == []


def test_date_precision_uses_kyiv_local_date_not_utc():
    # 00:00 Kyiv 2 березня = 22:00 UTC 1 березня
    pdf = _row("csv-pdf", _kyiv(2026, 3, 2), precision="date")
    same_kyiv_day = _row("csv-xlsx", _kyiv(2026, 3, 2, 1, 30, 0))     # 23:30 UTC 1 березня
    previous_kyiv_day = _row("csv-xlsx", _kyiv(2026, 3, 1, 23, 30, 0))

    assert len(dedup.match_duplicates([pdf, same_kyiv_day]).matches) == 1
    assert dedup.match_duplicates([pdf, previous_kyiv_day]).matches == []


# --- Конфлікти й перенесення атрибутів --------------------------------------

def test_conflicting_manual_categories_are_not_matched():
    a = _row("csv-pdf", _kyiv(2026, 4, 10), precision="date", category="Продукти", category_method="manual")
    b = _row("csv-xlsx", _kyiv(2026, 4, 10, 14, 23, 51), category="Кафе та ресторани", category_method="manual")

    result = dedup.match_duplicates([a, b])

    assert result.matches == []
    assert len(result.conflicts) == 1
    assert set(result.conflicts[0].row_ids) == {a.id, b.id}
    assert result.conflicts[0].reason == "manual_category"


def test_conflicting_loan_ids_are_not_matched():
    a = _row("csv-pdf", _kyiv(2026, 4, 10), precision="date", loan_id="loan-1")
    b = _row("csv-xlsx", _kyiv(2026, 4, 10, 14, 23, 51), loan_id="loan-2")

    result = dedup.match_duplicates([a, b])

    assert result.matches == []
    assert result.conflicts[0].reason == "loan_id"


def test_manual_category_and_loan_transfer_to_survivor():
    pdf = _row("csv-pdf", _kyiv(2026, 4, 10), precision="date",
               category="Продукти", category_method="manual", loan_id="loan-1")
    xlsx = _row("csv-xlsx", _kyiv(2026, 4, 10, 14, 23, 51), category="Житло та комунальні",
                category_method="bank_provided")

    match = dedup.match_duplicates([pdf, xlsx]).matches[0]

    assert match.survivor_id == xlsx.id
    assert match.transfers == {"category": "Продукти", "category_method": "manual", "loan_id": "loan-1"}


def test_category_fills_pending_survivor():
    pdf = _row("csv-pdf", _kyiv(2026, 4, 10), precision="date", category="Соціальна допомога",
               category_method="keyword", amount=207700)
    xlsx = _row("csv-xlsx", _kyiv(2026, 4, 10, 9, 1, 2), amount=207700)

    match = dedup.match_duplicates([pdf, xlsx]).matches[0]

    assert match.survivor_id == xlsx.id
    assert match.transfers == {"category": "Соціальна допомога", "category_method": "keyword"}


# --- Точність по рахунку (infer_account_precision) ------------------------

@pytest.mark.parametrize(
    "source_type, times, expected",
    [
        ("monobank_api", [_kyiv(2026, 1, 1)], "second"),
        ("manual", [_kyiv(2026, 1, 1, 13, 5, 9)], "date"),
        ("csv_import", [_kyiv(2026, 1, 1), _kyiv(2026, 1, 9)], "date"),
        ("csv_import", [_kyiv(2026, 1, 1, 9, 30), _kyiv(2026, 1, 9)], "minute"),
        ("csv_import", [_kyiv(2026, 1, 1, 9, 30, 1), _kyiv(2026, 1, 9)], "second"),
    ],
)
def test_infer_account_precision(source_type, times, expected):
    assert dedup.infer_account_precision(source_type, times) == expected


# --- План на БД: лише читання -----------------------------------------------

async def test_plan_is_read_only_and_finds_cross_source_pair(db_session, real_user):
    uid = real_user.id
    pdf_acc = Account(id=f"csv-{uuid.uuid4()}", user_id=uid, source_type="csv_import", bank_id="privatbank")
    xlsx_acc = Account(id=f"csv-{uuid.uuid4()}", user_id=uid, source_type="csv_import", bank_id=None)
    db_session.add_all([pdf_acc, xlsx_acc])
    await db_session.flush()

    def txn(acc, time, amount, precision):
        return Transaction(id=str(uuid.uuid4()), user_id=uid, account_id=acc.id, time=time,
                           time_precision=precision, amount=amount, amount_uah=amount // 100,
                           category="pending", category_method="pending")

    # PDF-копія — лише дата (00:00); XLSX — точний час до секунди.
    pdf_row = txn(pdf_acc, _kyiv(2026, 4, 10), -12300, "date")
    xlsx_row = txn(xlsx_acc, _kyiv(2026, 4, 10, 14, 23, 51), -12300, "second")
    # Дві однакові покупки в одному рахунку — не чіпати.
    same_acc = [txn(xlsx_acc, _kyiv(2026, 4, 11, 10, 0, 1), -5000, "second"),
                txn(xlsx_acc, _kyiv(2026, 4, 11, 10, 0, 40), -5000, "second")]
    db_session.add_all([pdf_row, xlsx_row, *same_acc])
    await db_session.commit()

    count_before = (await db_session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.user_id == uid))).scalar_one()

    plan = await dedup.plan_duplicate_removal(db_session, uid)

    count_after = (await db_session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.user_id == uid))).scalar_one()
    assert count_after == count_before == 4
    assert [(m.survivor_id, m.duplicate_id, m.kind) for m in plan.matches] == [(xlsx_row.id, pdf_row.id, "date")]
    assert plan.ambiguous == []
