"""
Імпорт банківських виписок CSV, XLSX і PDF.

Усі формати зводяться до сирого DataFrame без заголовків (усі клітинки — str):
CSV і XLSX читає pandas, PDF — pdfplumber (_read_pdf_statement). Далі обробка
однакова для всіх форматів:

1. Пошук рядка заголовків (він не завжди перший). Повтори заголовка на
   сторінках PDF прибираються ще під час читання файлу.
2. Зіставлення колонок з канонічними полями: нечітка схожість рядків
   (difflib) і косинусна схожість ембедінгів sentence-transformers.
3. Перевірка вмісту: колонка дати має розбиратися як дата, колонки сум і
   залишку — як числа.
4. Діагностика залишку: balance[i-1] + amount[i] == balance[i]; імпорт не блокує.

Категорія банку береться лише з однозначних відповідників (BANK_CATEGORY_MAP);
решта транзакцій проходить звичайну категоризацію за описом.
"""
import difflib
import re
from datetime import datetime
from io import BytesIO

import chardet
import pandas as pd
import pdfplumber
from dateutil import parser as dateutil_parser
from sentence_transformers import SentenceTransformer, util as st_util
 
# --- Синоніми назв колонок банківських виписок ---
 
CANONICAL_FIELDS = {
    "date": [
        "дата", "дата операції", "дата і час", "дата і час операції",
        "дата проведення", "дата транзакції", "date",
    ],
    "description": [
        "опис", "опис операції", "деталі операції", "деталі транзакції",
        "торговельна точка", "призначення платежу", "контрагент",
        "назва", "description", "details",
    ],
    "category": [
        "категорія", "category",
    ],
    "amount_signed": [
        "сума", "сума операції", "сума в валюті картки",
        "сума в валюті картки (uah)", "сума транзакції", "amount",
    ],
    "amount_expense": [
        "видаток", "дебет", "debit", "сума (дебет)",
    ],
    "amount_income": [
        "надходження", "кредит", "credit", "сума (кредит)", "поповнення",
    ],
    "balance": [
        "баланс", "залишок", "залишок після операції", "залишок на кінець періоду",
        "поточний баланс", "доступний залишок", "balance",
    ],
}
 
# Колонки, що не зіставляються з жодним канонічним полем.
IGNORED_COLUMN_HINTS = [
    "картка", "валюта", "рахунок", "сума комісій", "сума знижок",
    "сума в валюті транзакції", "валюта картки", "валюта транзакції",
    "валюта залишку",
]
 
# Однозначні відповідники категорій банку -> канонічна категорія.
# Неоднозначні категорії банку (наприклад, «Перекази», «Зарахування») сюди не
# входять: такі транзакції проходять звичайну категоризацію за описом.
BANK_CATEGORY_MAP = {
    "комуналка та інтернет": "Житло та комунальні",
    "поповнення мобільного": "Зв'язок та інтернет",
}
 
_embedding_model = None
 
 
def _get_embedding_model() -> SentenceTransformer:
    """Повертає модель sentence-transformers, спільну для імпорту й ML-категоризатора (завантажується один раз)."""
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    return _embedding_model


def _group_words_into_rows(words: list[dict], row_tolerance: float = 3.0) -> list[list[dict]]:
    """Групує слова pdfplumber у рядки за близькістю координати top.

    Використовується для розбиття сторінки на рядки і для незалежної оцінки
    кількості рядків на сторінці.
    """
    if not words:
        return []
    words_sorted = sorted(words, key=lambda w: w["top"])
    rows_words: list[list[dict]] = []
    current_row: list[dict] = []
    current_top = None
    for w in words_sorted:
        if current_top is None or abs(w["top"] - current_top) <= row_tolerance:
            current_row.append(w)
            current_top = current_top if current_top is not None else w["top"]
        else:
            rows_words.append(current_row)
            current_row = [w]
            current_top = w["top"]
    if current_row:
        rows_words.append(current_row)
    return rows_words


def _find_column_boundaries(words: list[dict], min_gutter_width: float = 4.0) -> list[float]:
    """Знаходить межі колонок як середини вертикальних смуг, не перекритих жодним словом сторінки.

    Враховується повна ширина слів [x0, x1], тож проміжки між словами всередині
    багатослівної клітинки не стають межами колонок.

    Returns:
        Відсортовані x-координати меж.
    """
    if not words:
        return []

    events = sorted(
        [(w["x0"], 1) for w in words] + [(w["x1"], -1) for w in words]
    )

    coverage = 0
    gutter_start = None
    boundaries: list[float] = []
    for x, delta in events:
        if coverage == 0 and delta == 1 and gutter_start is not None:
            if x - gutter_start >= min_gutter_width:
                boundaries.append((gutter_start + x) / 2)
        coverage += delta
        if coverage == 0:
            gutter_start = x
    return boundaries


def _cluster_words_into_rows_and_columns(words: list[dict]) -> list[list[str]]:
    """Витягує таблицю зі сторінки PDF без ліній розмітки, групуючи слова в рядки й колонки.

    Межі колонок рахуються один раз для всієї сторінки, тож усі рядки мають
    однакову кількість колонок.

    Returns:
        Рядки таблиці — списки текстів клітинок.
    """
    if not words:
        return []

    boundaries = _find_column_boundaries(words)

    def column_index(x0: float) -> int:
        """Повертає номер колонки для x-координати слова."""
        col = 0
        for boundary in boundaries:
            if x0 >= boundary:
                col += 1
        return col

    rows_words = _group_words_into_rows(words)

    num_cols = len(boundaries) + 1
    rows: list[list[str]] = []
    for row_words in rows_words:
        cells: list[list[str]] = [[] for _ in range(num_cols)]
        for w in sorted(row_words, key=lambda w: w["x0"]):
            cells[column_index(w["x0"])].append(w["text"])
        rows.append([" ".join(c) for c in cells])
    return rows


def _extract_pdf_page_rows(page) -> tuple[list[list], dict]:
    """Витягує рядки таблиці з однієї сторінки PDF.

    Спершу пробує таблиці за лініями розмітки (pdfplumber). Таблиця приймається,
    лише якщо рядків у ній не менше, ніж дає незалежна оцінка
    (_group_words_into_rows). Інакше слова групуються вручну
    (_cluster_words_into_rows_and_columns).

    Returns:
        Пара (rows, diagnostics): diagnostics містить назву стратегії та кількість
        колонок на сторінці.
    """
    words = page.extract_words(keep_blank_chars=False)
    expected_row_count = len(_group_words_into_rows(words))

    tables = page.extract_tables()
    if tables:
        best = max(tables, key=len)
        if len(best) > 0 and len(best) >= expected_row_count and len(best[0]) >= 2:
            return best, {"strategy": "lines", "col_count": len(best[0])}

    if not words:
        return [], {"strategy": "empty", "col_count": None}
    rows = _cluster_words_into_rows_and_columns(words)
    return rows, {"strategy": "words", "col_count": max((len(r) for r in rows), default=0)}


_MASKED_ACCOUNT_RE = r"\d{4,8}\*{4,8}\d{2,6}"
_ANCHOR_LINE_RE = re.compile(
    rf"\d{{2}}\.\d{{2}}\.\d{{4}}.*{_MASKED_ACCOUNT_RE}.*-?\d[\d\s ]*,\d{{2}}\s*$"
)
_DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
# Час операції (HH:MM) на початку рядка після якірного. Прив'язка до
# початку рядка не дає підхопити час формування виписки з шапки.
_PDF_BLOCK_TIME_RE = re.compile(r"^\s*(\d{2}):(\d{2})(?!\d|:)")
_AMOUNT_RE = re.compile(r"-?\d[\d\s ]*,\d{2}")

# Рядки шапки й реквізитів виписки, які не потрапляють в опис транзакції.
_PDF_BOILERPLATE_LINE_RE = re.compile(
    r"(?i)власник рахунку|рнокпп|паспорт громадянина|юридична адреса|"
    r"адреса для зв|телефони:|www\.|надавач платіжних послуг|кредитний ліміт|"
    r"валюта рахунку|баланс на (початок|кінець)|сума комісій:|сума знижок:|"
    r"виписка по картці|сторінка \d+ з ?\d*|акціонерне товариство|"
    r"^сума у|^дата сума|^рахунок деталі|комісій знижок|операції картки|"
    r"адреса реєстрації|виданий\b|р\. ?н\.$"
)

# Рядок заголовків для блоків з _parse_pdf_transaction_blocks; назви взяті з
# CANONICAL_FIELDS, тож мапінг колонок розпізнає їх однозначно.
_BLOCK_PARSER_HEADER = ["Дата операції", "Опис операції", "Сума операції", "Залишок"]


def _extract_block_time(amounts_line: str) -> str | None:
    """Повертає час "HH:MM" з початку рядка після якірного або None.

    None — час невідомий (зокрема поза діапазоном, як 24:61); рядок тоді
    обробляється як такий, що має лише дату.
    """
    match = _PDF_BLOCK_TIME_RE.match(amounts_line)
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


def _parse_pdf_transaction_blocks(full_text: str) -> list[list[str]]:
    """Розбирає PDF-звіт, де кожна транзакція — блок з кількох рядків тексту, а не рядок таблиці.

    Кожна транзакція має два послідовні якірні рядки:
    1. дата + замаскований номер картки чи рахунку + сума операції (_ANCHOR_LINE_RE);
    2. час + «Угода №...» + суми комісії, знижки й залишку; залишок — останнє
       число рядка, час HH:MM — з його початку (_extract_block_time).

    Опис — текст між попередньою парою якірних рядків і поточною, не більше
    MAX_DESCRIPTION_LOOKBACK рядків назад; рядки шапки й реквізитів
    (_PDF_BOILERPLATE_LINE_RE) в опис не потрапляють. Текст, що стоїть після пари
    якірних рядків, відноситься до наступної транзакції.

    Args:
        full_text: Текст усіх сторінок PDF.

    Returns:
        Рядки [дата, опис, сума, залишок, час]; час — "HH:MM" або None.
    """
    MAX_DESCRIPTION_LOOKBACK = 10

    lines = full_text.split("\n")
    anchor_indices = [i for i, ln in enumerate(lines) if _ANCHOR_LINE_RE.search(ln.strip())]
    if not anchor_indices:
        return []

    rows: list[list[str]] = []
    prev_end = 0
    for anchor_idx in anchor_indices:
        anchor_line = lines[anchor_idx].strip()
        amounts_line = lines[anchor_idx + 1].strip() if anchor_idx + 1 < len(lines) else ""

        date_match = _DATE_RE.search(anchor_line)
        anchor_amounts = _AMOUNT_RE.findall(anchor_line)
        if not date_match or not anchor_amounts:
            prev_end = anchor_idx + 1
            continue

        amount_str = anchor_amounts[-1]
        amounts_line_values = _AMOUNT_RE.findall(amounts_line)
        balance_str = amounts_line_values[-1] if amounts_line_values else amount_str
        desc_start = max(prev_end, anchor_idx - MAX_DESCRIPTION_LOOKBACK)

        description = " ".join(
            ln.strip() for ln in lines[desc_start:anchor_idx]
            if ln.strip() and not _PDF_BOILERPLATE_LINE_RE.search(ln)
        )
        description = re.sub(r"\s+", " ", description).strip()

        rows.append([
            date_match.group(0), description, amount_str, balance_str,
            _extract_block_time(amounts_line),
        ])
        prev_end = anchor_idx + 2  # пропускаємо якірний рядок і рядок сум

    return rows


def _read_pdf_statement(file_bytes: bytes) -> pd.DataFrame:
    """Читає PDF-виписку в сирий DataFrame без заголовків (усі клітинки — str).

    Спершу витягує таблицю посторінково (_extract_pdf_page_rows) і прибирає
    повтори рядка заголовків на сторінках 2+. Якщо в результаті не знаходиться
    рядок заголовків, файл розбирається як звіт з блоками транзакцій
    (_parse_pdf_transaction_blocks).

    Діагностика (стратегія на кожній сторінці, розбіжності кількості колонок,
    використання розбору блоків) кладеться в df.attrs["pdf_diagnostics"].

    Raises:
        ValueError: Не вдалося розпізнати ні таблицю, ні блоки транзакцій.
    """
    all_rows: list[list] = []
    page_texts: list[str] = []
    page_strategies: dict[int, str] = {}
    column_count_mismatches: list[dict] = []
    repeated_header_rows_dropped = 0
    first_page_col_count = None

    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            rows, diag = _extract_pdf_page_rows(page)
            page_strategies[page_num] = diag["strategy"]
            page_texts.append(page.extract_text() or "")

            if diag["col_count"] is not None:
                if first_page_col_count is None:
                    first_page_col_count = diag["col_count"]
                elif diag["col_count"] != first_page_col_count:
                    column_count_mismatches.append({
                        "page": page_num,
                        "expected_cols": first_page_col_count,
                        "found_cols": diag["col_count"],
                    })

            if page_num > 1:
                kept_rows = []
                for row in rows[:3]:
                    if _looks_like_header_row([str(c or "") for c in row]):
                        repeated_header_rows_dropped += 1
                        continue
                    kept_rows.append(row)
                rows = kept_rows + rows[3:]

            all_rows.extend(rows)

    if page_strategies and all(s == "empty" for s in page_strategies.values()):
        raise ValueError(
            "PDF не містить розпізнаваного тексту — схоже, це скан або фото, "
            "а не текстова виписка. Розпізнавання сканів (OCR) поки не підтримується."
        )

    extraction_strategy = "grid"
    try:
        find_header_row(pd.DataFrame(all_rows, dtype=str))
    except ValueError:
        block_rows = _parse_pdf_transaction_blocks("\n".join(page_texts))
        if not block_rows:
            raise ValueError(
                "Не вдалося розпізнати структуру транзакцій у PDF — ні як таблицю, "
                "ні як звіт з блоками транзакцій. Спробуй інший формат виписки "
                "(CSV/XLSX) або зверніться до підтримки."
            )
        # Лише перші 4 значення рядка, під 4-колонковий заголовок; час (п'яте
        # значення) до DataFrame не потрапляє.
        all_rows = [_BLOCK_PARSER_HEADER] + [row[: len(_BLOCK_PARSER_HEADER)] for row in block_rows]
        extraction_strategy = "blocks"

    max_cols = max((len(row) for row in all_rows), default=0)
    padded_rows = [list(row) + [None] * (max_cols - len(row)) for row in all_rows]
    rows_padded = sum(1 for row in all_rows if len(row) < max_cols)

    df = pd.DataFrame(padded_rows, dtype=str)
    df.attrs["pdf_diagnostics"] = {
        "pages_processed": len(page_strategies),
        "page_strategies": page_strategies,
        "column_count_mismatches": column_count_mismatches,
        "repeated_header_rows_dropped": repeated_header_rows_dropped,
        "rows_padded": rows_padded,
        "extraction_strategy": extraction_strategy,
    }
    return df


def read_statement_file(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Читає CSV, XLSX або PDF у сирий DataFrame без заголовків.

    Кодування CSV визначається автоматично (UTF-8, Windows-1251 та інші типові).

    Args:
        file_bytes: Вміст файлу.
        filename: Ім'я файлу; формат визначається за розширенням.

    Returns:
        DataFrame з header=None.
    """
    ext = filename.lower().rsplit(".", 1)[-1]
 
    if ext in ("xlsx", "xls"):
        return pd.read_excel(BytesIO(file_bytes), header=None, dtype=str)

    if ext == "pdf":
        return _read_pdf_statement(file_bytes)

    if ext == "csv":
        # Типові кодування українських банківських CSV.
        detected = chardet.detect(file_bytes)
        encoding = detected.get("encoding") or "utf-8"
        try:
            return pd.read_csv(BytesIO(file_bytes), header=None, dtype=str, encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            for fallback_encoding in ("cp1251", "utf-8", "utf-8-sig"):
                try:
                    return pd.read_csv(
                        BytesIO(file_bytes), header=None, dtype=str, encoding=fallback_encoding
                    )
                except (UnicodeDecodeError, LookupError):
                    continue
            raise ValueError(f"Не вдалося визначити кодування файлу {filename}")
 
    raise ValueError(f"Непідтримуваний формат файлу: .{ext}. Очікується .csv, .xlsx, .xls або .pdf")
 
 
def _count_header_matches(row_values: list[str]) -> int:
    """Рахує клітинки рядка, що нечітко збігаються (>= 0.75) з синонімом із CANONICAL_FIELDS."""
    all_synonyms = [syn for synonyms in CANONICAL_FIELDS.values() for syn in synonyms]
    matches = 0
    for cell in row_values:
        cell = str(cell or "").lower().strip()
        if not cell or cell == "nan":
            continue
        best_cell_score = max(
            difflib.SequenceMatcher(None, cell, syn).ratio() for syn in all_synonyms
        )
        if best_cell_score >= 0.75:
            matches += 1
    return matches


def _looks_like_header_row(row_values: list[str]) -> bool:
    """Перевіряє, чи рядок схожий на заголовок таблиці (щонайменше 3 збіги)."""
    return _count_header_matches(row_values) >= 3


def find_header_row(df: pd.DataFrame, max_scan_rows: int = 15) -> int:
    """Шукає рядок заголовків серед перших max_scan_rows рядків.

    Обирає рядок з найбільшою кількістю клітинок, що збігаються зі словником
    синонімів; потрібно щонайменше 3 збіги.

    Returns:
        Індекс рядка заголовків.

    Raises:
        ValueError: Рядок заголовків не знайдено.
    """
    best_row_idx = -1
    best_score = 2  # поріг: менше 3 збігів — не заголовок

    scan_limit = min(max_scan_rows, len(df))
    for row_idx in range(scan_limit):
        row_values = df.iloc[row_idx].fillna("").astype(str).str.lower().str.strip().tolist()
        matches = _count_header_matches(row_values)
        if matches > best_score:
            best_score = matches
            best_row_idx = row_idx

    if best_row_idx == -1:
        raise ValueError(
            "Не вдалося знайти рядок заголовків у перших "
            f"{scan_limit} рядках файлу. Перевір, чи файл дійсно є банківською "
            "випискою з рядком заголовків колонок."
        )
    return best_row_idx
 
 
def _content_matches_date(series: pd.Series, sample_size: int = 10) -> bool:
    """Перевіряє, чи щонайменше 80 % вибірки значень колонки розбираються як дати."""
    sample = series.dropna().astype(str).head(sample_size)
    if len(sample) == 0:
        return False
    ok = 0
    for value in sample:
        try:
            dateutil_parser.parse(value, dayfirst=True)
            ok += 1
        except (ValueError, TypeError, OverflowError):
            continue
    return (ok / len(sample)) >= 0.8
 
 
def _clean_number(value: str) -> float:
    """Перетворює число банківського формату ('1 254,66') на float."""
    cleaned = str(value).replace("\xa0", " ").replace(" ", "").replace(",", ".")
    return float(cleaned)
 
 
def _content_matches_number(series: pd.Series, sample_size: int = 10) -> bool:
    """Перевіряє, чи щонайменше 80 % вибірки значень колонки розбираються як числа."""
    sample = series.dropna().astype(str).head(sample_size)
    if len(sample) == 0:
        return False
    ok = 0
    for value in sample:
        try:
            _clean_number(value)
            ok += 1
        except (ValueError, TypeError):
            continue
    return (ok / len(sample)) >= 0.8
 
 
def map_columns(df: pd.DataFrame, header_row: int) -> dict:
    """Зіставляє колонки таблиці з канонічними полями.

    Returns:
        Словник {canonical_field: column_index} лише для полів, визначених
        упевнено (за назвою і вмістом колонки).
    """
    headers = df.iloc[header_row].fillna("").astype(str).str.lower().str.strip().tolist()
    data = df.iloc[header_row + 1:].reset_index(drop=True)
 
    model = _get_embedding_model()
    header_embeddings = model.encode(headers, convert_to_tensor=True)
 
    mapping = {}
    used_columns = set()
 
    # Спершу поля, які визначаються найнадійніше (date, balance), щоб вони не
    # зайняли колонку, потрібну іншому полю.
    field_priority = ["date", "balance", "amount_signed", "amount_expense",
                       "amount_income", "category", "description"]
 
    for field in field_priority:
        synonyms = CANONICAL_FIELDS[field]
        synonym_embeddings = model.encode(synonyms, convert_to_tensor=True)
 
        best_col_idx = None
        best_score = 0.0
 
        for col_idx, header in enumerate(headers):
            if col_idx in used_columns or not header or header == "nan":
                continue
            if any(hint in header for hint in IGNORED_COLUMN_HINTS):
                # Колонки сум у валюті транзакції чи картки не зіставляються з жодним полем.
                continue
 
            fuzzy_score = max(
                difflib.SequenceMatcher(None, header, syn).ratio() for syn in synonyms
            )
            semantic_score = st_util.cos_sim(
                header_embeddings[col_idx], synonym_embeddings
            ).max().item()
            combined_score = max(fuzzy_score, semantic_score)
 
            if combined_score > best_score:
                best_score = combined_score
                best_col_idx = col_idx
 
        if best_col_idx is None or best_score < 0.55:
            continue
 
        # Перевірка за вмістом колонки.
        column_data = data.iloc[:, best_col_idx]
        if field == "date" and not _content_matches_date(column_data):
            continue
        if field in ("amount_signed", "amount_expense", "amount_income", "balance"):
            if not _content_matches_number(column_data):
                continue
 
        mapping[field] = best_col_idx
        used_columns.add(best_col_idx)
 
    return mapping
 
 
def validate_balance(data: pd.DataFrame, mapping: dict) -> dict:
    """Перевіряє безперервність залишку: balance[i-1] + amount[i] == balance[i].

    Рядки впорядковуються за датою за зростанням. Результат лише
    діагностичний і не блокує імпорт.

    Returns:
        Словник з ключем checked; якщо перевірка неможлива — reason.
    """
    if "balance" not in mapping or "date" not in mapping:
        return {"checked": False, "reason": "Немає колонки balance або date для перевірки"}
 
    amount_field = "amount_signed" if "amount_signed" in mapping else None
    if amount_field is None:
        return {"checked": False, "reason": "Немає єдиної колонки amount_signed для перевірки"}
 
    try:
        dates = data.iloc[:, mapping["date"]].apply(
            lambda v: dateutil_parser.parse(str(v), dayfirst=True)
        )
        amounts = data.iloc[:, mapping[amount_field]].apply(_clean_number)
        balances = data.iloc[:, mapping["balance"]].apply(_clean_number)
    except (ValueError, TypeError):
        return {"checked": False, "reason": "Не вдалося розпарсити дані для перевірки"}
 
    order = dates.argsort()
    amounts_sorted = amounts.iloc[order].reset_index(drop=True)
    balances_sorted = balances.iloc[order].reset_index(drop=True)
 
    matches = 0
    total = len(balances_sorted) - 1
    for i in range(1, len(balances_sorted)):
        expected = balances_sorted[i - 1] + amounts_sorted[i]
        if abs(expected - balances_sorted[i]) < 0.05:
            matches += 1
 
    match_rate = matches / total if total > 0 else None
    return {
        "checked": True,
        "match_rate": round(match_rate, 3) if match_rate is not None else None,
        "matched_rows": matches,
        "total_checked_rows": total,
    }
 
 
def normalize_category(bank_category: str | None) -> str | None:
    """Переводить категорію банку в канонічну за BANK_CATEGORY_MAP.

    Returns:
        Канонічну категорію або None для неоднозначних категорій банку.
    """
    if not bank_category:
        return None
    key = str(bank_category).strip().lower()
    return BANK_CATEGORY_MAP.get(key)
 
 
def parse_statement(file_bytes: bytes, filename: str) -> dict:
    """Розбирає файл виписки для попереднього перегляду.

    Читає файл, знаходить заголовки, зіставляє колонки й перевіряє залишок.

    Args:
        file_bytes: Вміст файлу.
        filename: Ім'я файлу; формат визначається за розширенням.

    Returns:
        Словник: header_row, column_headers, mapping (поле -> назва колонки),
        mapping_column_indices, preview_rows, balance_check, warnings (об'єкти
        {code, message, severity, pages?}), page_diagnostics (для PDF), total_rows.
    """
    raw_df = read_statement_file(file_bytes, filename)
    # .attrs читається з raw_df до будь-якого зрізу: pandas не гарантує
    # перенесення .attrs на похідні DataFrame.
    pdf_diagnostics = raw_df.attrs.get("pdf_diagnostics")
    header_row = find_header_row(raw_df)
    mapping = map_columns(raw_df, header_row)
    data = raw_df.iloc[header_row + 1:].reset_index(drop=True)

    # Попередження — об'єкти {code, message, severity, pages?}. severity
    # ("blocking" | "info") визначає, чи блокує фронтенд підтвердження імпорту;
    # pages прив'язує попередження до сторінок PDF.
    warnings = []
    page_diagnostics = (pdf_diagnostics or {}).get("column_count_mismatches") or []
    if page_diagnostics:
        affected_pages = ", ".join(str(m["page"]) for m in page_diagnostics)
        warnings.append({
            "code": "pdf_page_column_mismatch",
            "message": (
                f"Кількість колонок відрізняється між сторінками PDF (сторінки: {affected_pages}) — "
                "уважно перевір рядки в preview перед підтвердженням, дані на цих сторінках можуть бути зміщені"
            ),
            "severity": "info",
            "pages": [m["page"] for m in page_diagnostics],
        })
    if "date" not in mapping:
        warnings.append({
            "code": "missing_date_column",
            "message": "Не вдалося визначити колонку дати — імпорт неможливий без ручного мапінгу",
            "severity": "blocking",
        })
    if "amount_signed" not in mapping and not (
        "amount_expense" in mapping and "amount_income" in mapping
    ):
        warnings.append({
            "code": "missing_amount_column",
            "message": (
                "Не вдалося визначити колонку суми (ні єдину, ні пару дебет/кредит) — "
                "імпорт неможливий без ручного мапінгу"
            ),
            "severity": "blocking",
        })
    if "description" not in mapping:
        warnings.append({
            "code": "missing_description_column",
            "message": "Не вдалося визначити колонку опису — опис транзакцій буде порожнім",
            "severity": "info",
        })
    if "category" not in mapping:
        warnings.append({
            "code": "missing_category_column",
            "message": (
                "Колонку категорії не знайдено — усі транзакції підуть на автоматичну "
                "ML-категоризацію за описом"
            ),
            "severity": "info",
        })
 
    balance_check = validate_balance(data, mapping)
 
    headers = raw_df.iloc[header_row].fillna("").astype(str).tolist()
    preview_rows = []
    for _, row in data.head(5).iterrows():
        preview_rows.append({headers[i]: row.iloc[i] for i in range(len(headers))})
 
    return {
        "header_row": header_row,
        "column_headers": headers,
        "mapping": {field: headers[idx] for field, idx in mapping.items()},
        "mapping_column_indices": mapping,
        "preview_rows": preview_rows,
        "balance_check": balance_check,
        "warnings": warnings,
        "page_diagnostics": page_diagnostics,
        "total_rows": len(data),
    }
 