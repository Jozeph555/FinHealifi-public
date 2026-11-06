"""
Відсотки місяця, у якому стоїть якір журналу (поточна поведінка debt_series).

Якір 'plan_accepted' діє з початку дня: на кінець його місяця нараховуються
відсотки на його залишок. Звірка за випискою ('statement') — залишок на кінець
дня as_of: у її місяці closing = звірка − платежі після as_of, відсотків до
кінця місяця немає, і залишок на 1-ше число наступного місяця їх не містить.
Звірка, створена в той самий день пізніше за 'plan_accepted', перекриває його
як якір місяця. Плановий рядок (roll_month) містить відсотки opening · ставка,
тож без відсотків він лише за нульової ставки.
"""
from datetime import date

from app.services import debt_ledger as ledger
from app.services.debt_ledger import Anchor, debt_series, roll_month

NOV = date(2026, 11, 1)
DEC_1 = date(2026, 12, 1)


def test_plan_accepted_anchor_accrues_month_interest():
    """МОНО: старт 10 000, 3,1 %, без платежів — на 1 грудня 10 310."""
    series = debt_series([Anchor(date(2026, 11, 5), 1_000_000, "plan_accepted")], [], 0.031, NOV, DEC_1)
    november = series[0]
    assert (november.interest, november.closing) == (31_000, 1_031_000)
    assert series[-1].month == DEC_1 and series[-1].closing == 1_031_000


def test_statement_anchor_same_day_has_no_interest_until_month_end():
    """Позика: plan_accepted і звірка 'statement' на 5.11, без платежів — на 1 грудня рівно 15 645."""
    anchors = [Anchor(date(2026, 11, 5), 1_564_500, "plan_accepted"),
               Anchor(date(2026, 11, 5), 1_564_500, "statement")]
    series = debt_series(anchors, [], 0.05, NOV, DEC_1)
    november = series[0]
    assert november.anchor.source == "statement"
    assert (november.opening, november.interest, november.closing) == (None, None, 1_564_500)
    assert series[-1].closing == 1_564_500


def test_statement_balance_as_of_first_of_next_month_has_no_interest():
    anchors = [Anchor(date(2026, 11, 5), 1_564_500, "plan_accepted"),
               Anchor(date(2026, 11, 5), 1_564_500, "statement")]
    without_statement = debt_series(anchors[:1], [], 0.05, NOV, DEC_1)[-1].closing
    with_statement = debt_series(anchors, [], 0.05, NOV, DEC_1)[-1].closing
    assert without_statement - with_statement == ledger.interest_kop(1_564_500, 0.05)


def test_plan_row_has_no_interest_only_at_zero_rate():
    assert roll_month(1_564_500, 0.0, 390_000).closing == 1_564_500 - 390_000
    assert roll_month(1_000_000, 0.031, 40_000).closing == 1_000_000 + 31_000 - 40_000
