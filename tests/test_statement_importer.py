"""
Тести імпорту виписок (app/services/statement_importer.py), передусім PDF.

БД не потрібна. Синтетичні PDF генеруються через matplotlib (pdf-бекенд дає
векторний текст, який pdfplumber вилучає). Вибір стратегії в
_extract_pdf_page_rows перевіряється на двійнику сторінки pdfplumber
(лише extract_tables і extract_words), тож результат детермінований.
"""
from io import BytesIO

import matplotlib
matplotlib.use("pdf")
import matplotlib.backends.backend_pdf as pdf_backend
import matplotlib.pyplot as plt
import pytest

from app.services.statement_importer import (
    _cluster_words_into_rows_and_columns,
    _extract_pdf_page_rows,
    _parse_pdf_transaction_blocks,
    _read_pdf_statement,
    find_header_row,
    map_columns,
    parse_statement,
    read_statement_file,
)

# --- Хелпери для синтетичних PDF (matplotlib) -------------------------------

def _render_table_page(rows, headers, bordered):
    fig, ax = plt.subplots(figsize=(8, 0.4 * (len(rows) + 1) + 1))
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    if not bordered:
        for cell in table.get_celld().values():
            cell.visible_edges = ""
            cell.set_linewidth(0)
    return fig


def _make_pdf_bytes(rows, headers, bordered=True):
    buf = BytesIO()
    fig = _render_table_page(rows, headers, bordered)
    fig.savefig(buf, format="pdf", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _make_multipage_pdf_bytes(pages_rows, headers, bordered=False):
    buf = BytesIO()
    with pdf_backend.PdfPages(buf) as pp:
        for rows in pages_rows:
            fig = _render_table_page(rows, headers, bordered)
            pp.savefig(fig, bbox_inches="tight")
            plt.close(fig)
    buf.seek(0)
    return buf.read()


def _make_blank_pdf_bytes():
    buf = BytesIO()
    fig, ax = plt.subplots(figsize=(4, 4))
    ax.axis("off")
    fig.savefig(buf, format="pdf")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


SAMPLE_HEADERS = ["Дата операції", "Опис операції", "Сума операції", "Залишок"]
SAMPLE_ROWS = [
    ["01.03.2026", "Оплата ТОВ Ромашка", "-500,00", "12000,00"],
    ["02.03.2026", "Зарплата", "20000,00", "32000,00"],
    ["03.03.2026", "АТБ Маркет", "-350,50", "31649,50"],
]


# --- _extract_pdf_page_rows: вибір стратегії (fake Page, детерміновано) -----

class _FakePage:
    """Двійник pdfplumber.Page з методами extract_tables і extract_words."""

    def __init__(self, tables_by_settings, words):
        self._tables_by_settings = tables_by_settings
        self._words = words

    def extract_tables(self, table_settings=None):
        key = None if table_settings is None else tuple(sorted(table_settings.items()))
        return self._tables_by_settings.get(key, [])

    def extract_words(self, **kwargs):
        return self._words


def _fake_words(rows_of_texts):
    """Генерує слова у форматі pdfplumber з синтетичними координатами — по одному слову на клітинку."""
    words = []
    for r_idx, row in enumerate(rows_of_texts):
        top = r_idx * 20.0
        for c_idx, text in enumerate(row):
            x0 = c_idx * 100.0
            words.append({"text": text, "x0": x0, "x1": x0 + max(len(text) * 5, 10), "top": top})
    return words


def test_extract_pdf_page_rows_prefers_lines_tier_when_complete():
    words = _fake_words([["h1", "h2"], ["a", "b"]])
    page = _FakePage({None: [[["h1", "h2"], ["a", "b"]]]}, words)
    rows, diag = _extract_pdf_page_rows(page)
    assert diag == {"strategy": "lines", "col_count": 2}
    assert rows == [["h1", "h2"], ["a", "b"]]


def test_extract_pdf_page_rows_falls_back_to_words_when_lines_empty():
    words = _fake_words([["h1", "h2"], ["a", "b"], ["c", "d"]])
    page = _FakePage({None: []}, words)
    rows, diag = _extract_pdf_page_rows(page)
    assert diag["strategy"] == "words"
    assert len(rows) == 3


def test_extract_pdf_page_rows_rejects_incomplete_lines_table_falls_to_words():
    """Таблиця за лініями розмітки з меншою кількістю рядків, ніж незалежна оцінка, відхиляється; рядки береться з групування слів."""
    words = _fake_words([["h1", "h2"], ["a", "b"], ["c", "d"]])  # 3 рядки очікується
    page = _FakePage({None: [[["a", "b"], ["c", "d"]]]}, words)  # лише 2 — неповна
    rows, diag = _extract_pdf_page_rows(page)
    assert diag["strategy"] == "words"
    assert len(rows) == 3


def test_extract_pdf_page_rows_empty_when_no_words():
    page = _FakePage({}, [])
    rows, diag = _extract_pdf_page_rows(page)
    assert diag == {"strategy": "empty", "col_count": None}
    assert rows == []


# --- _cluster_words_into_rows_and_columns: gutter-based колонки -------------

def test_cluster_words_handles_multiword_cells():
    """Багатослівні клітинки не розбиваються на кілька колонок: розпізнається рівно 2 колонки, а не 4."""
    words = [
        {"text": "Дата", "x0": 0, "x1": 20, "top": 0},
        {"text": "операції", "x0": 22, "x1": 55, "top": 0},
        {"text": "Опис", "x0": 100, "x1": 120, "top": 0},
        {"text": "операції", "x0": 122, "x1": 155, "top": 0},
        {"text": "01.03.2026", "x0": 0, "x1": 50, "top": 20},
        {"text": "Оплата", "x0": 100, "x1": 130, "top": 20},
    ]
    rows = _cluster_words_into_rows_and_columns(words)
    assert rows == [["Дата операції", "Опис операції"], ["01.03.2026", "Оплата"]]


def test_cluster_words_empty_input():
    assert _cluster_words_into_rows_and_columns([]) == []


# --- _read_pdf_statement / read_statement_file: наскрізно на синтетичних PDF --

def test_read_pdf_statement_bordered_table():
    data = _make_pdf_bytes(SAMPLE_ROWS, SAMPLE_HEADERS, bordered=True)
    df = read_statement_file(data, "statement.pdf")
    diag = df.attrs.get("pdf_diagnostics")
    assert diag["page_strategies"] == {1: "lines"}
    header_row = find_header_row(df)
    mapping = map_columns(df, header_row)
    assert set(mapping) == {"date", "description", "amount_signed", "balance"}


def test_read_pdf_statement_borderless_table_no_ruling_lines():
    """Таблиця без ліній розмітки читається через групування слів."""
    data = _make_pdf_bytes(SAMPLE_ROWS, SAMPLE_HEADERS, bordered=False)
    df = read_statement_file(data, "statement.pdf")
    diag = df.attrs.get("pdf_diagnostics")
    assert diag["page_strategies"][1] == "words"
    header_row = find_header_row(df)
    mapping = map_columns(df, header_row)
    assert mapping["date"] == 0
    assert mapping["amount_signed"] == 2
    assert mapping["balance"] == 3


def test_read_pdf_statement_multi_page_strips_repeated_header():
    page1 = SAMPLE_ROWS
    page2 = [
        ["04.03.2026", "Комунальні послуги", "-1200,00", "30449,50"],
        ["05.03.2026", "Переказ на картку", "-2000,00", "28449,50"],
    ]
    data = _make_multipage_pdf_bytes([page1, page2], SAMPLE_HEADERS, bordered=False)
    df = _read_pdf_statement(data)
    diag = df.attrs["pdf_diagnostics"]
    assert diag["pages_processed"] == 2
    assert diag["repeated_header_rows_dropped"] >= 1

    header_row = find_header_row(df)
    data_rows = df.iloc[header_row + 1:]
    # 5 транзакцій (3 + 2); повтор заголовка на сторінці 2 не стає транзакцією.
    assert len(data_rows) == len(page1) + len(page2)


def test_read_pdf_statement_scanned_pdf_raises_ukrainian_error():
    data = _make_blank_pdf_bytes()
    with pytest.raises(ValueError, match="скан"):
        read_statement_file(data, "statement.pdf")


def test_read_statement_file_unsupported_extension_mentions_pdf():
    with pytest.raises(ValueError, match=r"\.pdf"):
        read_statement_file(b"whatever", "statement.docx")


# --- parse_statement: наскрізний виклик через PDF ---------------------------

def test_parse_statement_pdf_end_to_end():
    data = _make_pdf_bytes(SAMPLE_ROWS, SAMPLE_HEADERS, bordered=True)
    result = parse_statement(data, "statement.pdf")

    assert result["total_rows"] == len(SAMPLE_ROWS)
    assert result["mapping"]["date"] == "Дата операції"
    assert result["balance_check"]["checked"] is True
    assert result["balance_check"]["match_rate"] == 1.0
    # PDF-виписки зазвичай не мають колонки «Категорія» — спрацьовує те саме
    # попередження, що й для CSV/XLSX.
    assert any(w["code"] == "missing_category_column" for w in result["warnings"])


def test_parse_statement_pdf_column_count_mismatch_warns():
    page1_headers = SAMPLE_HEADERS
    page1 = SAMPLE_ROWS
    # Сторінка 2 без колонки "Залишок" -> інша кількість колонок
    page2_headers = SAMPLE_HEADERS[:-1]
    page2 = [[row[0], row[1], row[2]] for row in [
        ["04.03.2026", "Комунальні послуги", "-1200,00"],
    ]]

    buf = BytesIO()
    with pdf_backend.PdfPages(buf) as pp:
        fig1 = _render_table_page(page1, page1_headers, bordered=True)
        pp.savefig(fig1, bbox_inches="tight")
        plt.close(fig1)
        fig2 = _render_table_page(page2, page2_headers, bordered=True)
        pp.savefig(fig2, bbox_inches="tight")
        plt.close(fig2)
    buf.seek(0)
    data = buf.read()

    result = parse_statement(data, "statement.pdf")
    assert any(w["code"] == "pdf_page_column_mismatch" for w in result["warnings"])
    assert result["page_diagnostics"] == [
        {"page": 2, "expected_cols": len(page1_headers), "found_cols": len(page2_headers)}
    ]


# --- find_header_row і _count_header_matches -------------------------------

def test_find_header_row_still_works_after_refactor():
    import pandas as pd

    raw = pd.DataFrame(
        [
            ["Виписка за період 01.01.2026-01.02.2026", None, None, None],
            SAMPLE_HEADERS,
            *SAMPLE_ROWS,
        ]
    )
    header_row = find_header_row(raw)
    assert header_row == 1


# --- _parse_pdf_transaction_blocks: PDF-звіт з блоками транзакцій -----------
# Заголовок розбитий на кілька рядків, кожна транзакція — блок тексту навколо
# двох якірних рядків; find_header_row не знаходить заголовка.

def test_parse_pdf_transaction_blocks_basic():
    text = "\n".join([
        "Якийсь службовий текст перед транзакціями",
        "Перекази, УКРАЇНА, термінал ABC123, документ №",
        "01.03.2026 123456******7890 Оплата ТОВ Ромашка -500,00",
        "12:00 Угода № TEST0001 отримувач ТОВ Ромашка UAH -500,00 5,00 0,00 12000,00",
        "Коментар до платежу: за послуги",
        "02.03.2026 123456******7890 Поповнення рахунку 20000,00",
        "09:30 Угода № TEST0002 UAH 20000,00 0,00 0,00 32000,00",
    ])
    rows = _parse_pdf_transaction_blocks(text)
    assert len(rows) == 2
    assert rows[0][0] == "01.03.2026"
    assert "Перекази, УКРАЇНА, термінал ABC123, документ №" in rows[0][1]
    assert rows[0][2] == "-500,00"
    assert rows[0][3] == "12000,00"
    assert rows[1][0] == "02.03.2026"
    assert rows[1][2] == "20000,00"
    assert rows[1][3] == "32000,00"


def test_parse_pdf_transaction_blocks_no_anchors_returns_empty():
    assert _parse_pdf_transaction_blocks("просто текст без жодної транзакції") == []


def test_parse_pdf_transaction_blocks_filters_boilerplate():
    """Дані власника рахунку з титульної сторінки (ПІБ, РНОКПП, паспорт) не потрапляють в опис першої транзакції."""
    text = "\n".join([
        "Власник рахунку: Тест Тестович",
        "РНОКПП: 1234567890, 01.01.1990 р. н.",
        "Дата Сума Сума Залишок після",
        "Рахунок Деталі операції валюті валюті",
        "операції комісій знижок операції",
        "Оплата ТОВ Ромашка",
        "01.03.2026 123456******7890 -500,00",
        "12:00 Угода № TEST0001 UAH -500,00 5,00 0,00 12000,00",
    ])
    rows = _parse_pdf_transaction_blocks(text)
    assert len(rows) == 1
    description = rows[0][1]
    assert "РНОКПП" not in description
    assert "Власник рахунку" not in description
    assert "Оплата ТОВ Ромашка" in description


def test_read_pdf_statement_falls_back_to_block_parser():
    """Якщо жоден рядок сторінки не схожий на заголовок таблиці, _read_pdf_statement розбирає файл як блоки транзакцій."""
    text = "\n".join([
        "Дата Сума Сума Залишок після",
        "Рахунок Деталі операції валюті валюті",
        "операції комісій знижок операції",
        "",
        "Перекази, УКРАЇНА, термінал ABC123, документ №",
        "01.03.2026 123456******7890 Оплата ТОВ Ромашка -500,00",
        "12:00 Угода № TEST0001 отримувач ТОВ Ромашка UAH -500,00 5,00 0,00 12000,00",
        "",
        "02.03.2026 123456******7890 Поповнення рахунку 20000,00",
        "09:30 Угода № TEST0002 UAH 20000,00 0,00 0,00 32000,00",
    ])

    buf = BytesIO()
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.axis("off")
    ax.text(0.02, 0.98, text, transform=ax.transAxes, va="top", fontsize=9, family="monospace")
    fig.savefig(buf, format="pdf", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    data = buf.read()

    df = _read_pdf_statement(data)
    assert df.attrs["pdf_diagnostics"]["extraction_strategy"] == "blocks"

    header_row = find_header_row(df)
    mapping = map_columns(df, header_row)
    assert set(mapping) == {"date", "description", "amount_signed", "balance"}


# --- Час операції HH:MM у розборі блоків PDF --------------------------------
# Дата — на початку якірного рядка, час — на початку наступного. П'ятий
# елемент рядка — час.

_ANCHOR = "01.03.2026 123456******7890 Оплата ТОВ Ромашка -500,00"


def _single_block_time(next_line: str, anchor: str = _ANCHOR):
    rows = _parse_pdf_transaction_blocks("\n".join(["Опис операції", anchor, next_line]))
    assert len(rows) == 1
    return rows[0]


def test_block_time_extracted_from_start_of_next_line():
    row = _single_block_time("12:05 Угода № TEST0001 UAH -500,00 5,00 0,00 12000,00")

    assert row[4] == "12:05"
    # перші чотири значення не змінилися
    assert row[:4] == ["01.03.2026", "Опис операції", "-500,00", "12000,00"]


def test_block_without_time_is_none_not_midnight():
    row = _single_block_time("Угода № TEST0001 UAH -500,00 5,00 0,00 12000,00")

    assert row[4] is None


@pytest.mark.parametrize("bad_time", ["24:61", "24:00", "23:60"])
def test_block_time_out_of_range_is_none(bad_time):
    row = _single_block_time(f"{bad_time} Угода № TEST0001 UAH -500,00 5,00 0,00 12000,00")

    assert row[4] is None


def test_block_time_with_seconds_is_not_supported():
    row = _single_block_time("12:05:33 Угода № TEST0001 UAH -500,00 5,00 0,00 12000,00")

    assert row[4] is None


def test_time_on_the_same_line_as_date_stays_none():
    # Інший формат (час у якірному рядку) — не підтримується, лишається None.
    row = _single_block_time(
        "Угода № TEST0001 UAH -500,00 5,00 0,00 12000,00",
        anchor="01.03.2026 12:05 123456******7890 Оплата ТОВ Ромашка -500,00",
    )

    assert row[0] == "01.03.2026"
    assert row[4] is None


def test_statement_header_date_time_line_gives_no_false_time():
    text = "\n".join([
        "05.08.2026 21:51 № TESTDOC0001 сторінка 1 з 2",   # шапка: час формування виписки
        "Опис операції",
        _ANCHOR,
        "Угода № TEST0001 UAH -500,00 5,00 0,00 12000,00",  # у самого блоку часу немає
        "Опис другої операції",
        "02.03.2026 123456******7890 Поповнення рахунку 20000,00",
        "05.08.2026 21:51 № TESTDOC0001 сторінка 2 з 2",   # шапка нової сторінки одразу після якоря
    ])

    rows = _parse_pdf_transaction_blocks(text)

    assert [r[0] for r in rows] == ["01.03.2026", "02.03.2026"]  # шапка не стала блоком
    assert [r[4] for r in rows] == [None, None]                  # і не дала хибного часу

