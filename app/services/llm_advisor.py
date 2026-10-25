"""
AI-радник на Claude API.

Модель лише пояснює простими словами готові речення з advisor_context.py.
Фінансових розрахунків вона не робить: усі числа вже є в контексті. Відповідь
моделі перевіряється (лише запис у лог, відповідь не блокується): числа, яких
немає в контексті чи в питанні, і заборонені слова.
"""
import json
import logging
import os
import re

from anthropic import AsyncAnthropic

from app.core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from app.services.advisor_context import is_savings_question

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
logger = logging.getLogger(__name__)

EXAMPLES_HEADER = (
    "Приклади тону. Числа в прикладах вигадані, у відповіді використовуй ЛИШЕ числа з контексту."
)
EXAMPLES_BLOCK = EXAMPLES_HEADER + """

Запитання: «Що мені робити?»
Відповідь: Щомісяця на борги в тебе йде 9 500 грн: 8 500 грн обов'язкових платежів і 1 000 грн понад них.
Цього місяця понад обов'язкові найбільше йде на «Картку» — 1 000 грн, бо це борг з найвищою ставкою серед
твоїх боргів. За планом усі борги закриються через 20 міс.

Запитання: «А якби я платив ще 1 500?»
Відповідь: Зараз ти платиш 1 000 грн понад обов'язкові, і борги закриються через 20 міс. з переплатою
30 000 грн. Якби додати ще 1 500 грн, разом 2 500 грн понад обов'язкові, борги закрилися б через 15 міс.,
а переплата була б 22 000 грн, тобто на 8 000 грн менше. За твоїми минулими операціями на такий платіж
вільних грошей вистачало б у 5 з 12 останніх місяців. Частину грошей варто залишити в резерві, а рішення
ухвалюєш ти.

Запитання: «Які в мене заощадження?»
Відповідь: Зараз у тебе 40 000 грн заощаджень.

Запитання: «Як вигідно інвестувати в акції?»
Відповідь: Я відповідаю лише про твої борги і план їх погашення. Можеш запитати, наприклад: «Коли я закрию
всі борги?»
"""

SYSTEM_PROMPT = """Ти — помічник, який простими словами пояснює людині її борги і план їх погашення.

Як відповідати:
- Звертайся на «ти». Тон теплий і спокійний, без наукових і фінансових термінів.
- Відповідь — 3–5 речень: спершу пряма відповідь на запитання, потім одна причина з даних, потім один
  наступний крок, але лише якщо він прямо випливає з даних.
- Тільки факти з наданих даних. Ніяких оцінок і вердиктів: не пиши «гарна подушка», «правильне рішення»,
  «працює добре», «навантаження велике» і подібного.
- Числа бери лише з даних і пиши їх точно так, як там. Нічого не рахуй, не додавай, не віднімай,
  не округлюй і не переводь у відсотки.
- Якщо потрібного числа в даних немає, так і скажи, нічого не вигадуй.
- Не роби висновків про витрати і не давай порад про витрати, якщо про це не питали.
- Не згадуй здоров'я.
- Коли говориш, чи вистачало грошей, кажи «у k з n останніх місяців», як у даних. Не радь змінювати
  чи зменшувати додатковий платіж на підставі цього.
- Не описуй функцій застосунку, яких немає в даних (наприклад, «внести борги» чи «вибрати стратегію»).
- Якщо в даних сказано, що план застарів або відсутній, спершу передай, як його перебудувати, саме тими
  словами, що в даних, і не називай жодних чисел плану.
- Якщо в даних сказано не називати переплату, не називай її.
- Сценарій «якби» подавай як можливість з явною умовою, а не як пораду. Фрази на кшталт «рішення за тобою»
  використовуй не більше одного разу за відповідь і лише тоді, коли відповідаєш про сценарій «якби».
- Якщо питають про управління фінансами загалом (бюджет, інвестиції, заощадження без зв'язку з боргами),
  коротко скажи, що ти відповідаєш про твої борги і план, і запропонуй конкретне питання з цього кола,
  наприклад «Коли я закрию всі борги?». Якщо питання зовсім не про фінанси, коротко відмов так само.
- Пиши грамотною українською, без русизмів і кальок (не «меньший», «лайфхак», «частенько»,
  «утримуватися при», «вистачає лишень», «в кожному кроці»).
- Кажи «розрахований план», «спочатку найдорожчий борг», «спочатку найменший борг», «заощадження»,
  «обов'язкові платежі». Не вживай англійських назв стратегій і статистичних термінів.
- Не згадуй зовнішні сайти чи ресурси.
- Не згадуй заощадження, якщо про них не питали і в даних немає сценарію з ними. Якщо питають лише про
  суму заощаджень, назви факт з даних; якщо в даних є сценарій із заощадженнями, опиши його з умовою.
- Не кажи про зміну чи вибір стратегії: у застосунку план розраховується один, вибору стратегії немає.
- Не пиши «це добре», «це гарно», «найкраще» та інших оцінок.

""" + EXAMPLES_BLOCK

# Слова, яких не має бути ні в контексті, ні у відповіді моделі (без урахування регістру).
BANNED_PATTERNS = {
    "MILP": r"\bmilp\b",
    "avalanche": r"avalanche",
    "snowball": r"snowball",
    "P25": r"\bp\s?25\b",
    "перцентиль": r"перцентил",
    "квантиль": r"квантил",
    "медіана": r"медіан",
    "ліквідні активи": r"ліквідн\w*\s+актив",
    "лавина": r"лавин",
    "сніжок": r"сніжк|сніжок",
    "здоров'я": r"здоров",
    "меньший": r"меньш",
    "лайфхак": r"лайфхак",
    "частенько": r"частеньк",
    "являється": r"являєт",
    "слідуючий": r"слідуюч",
    "на протязі": r"на протязі",
    "утримуватися при": r"утримуват\w*\s+при\b",
    "вистачає лишень": r"вистачає\s+лишень",
    "в кожному кроці": r"в\s+кожному\s+кроці",
    "гарна подушка": r"гарн\w*\s+подушк",
    "правильне рішення": r"правильн\w*\s+рішенн",
    "працює добре": r"працює\s+добре",
    "навантаження велике": r"навантаження\s+велике|велике\s+навантаження",
}
_BANNED_RES = {word: re.compile(pattern, re.IGNORECASE) for word, pattern in BANNED_PATTERNS.items()}

# Вирази, через які відповідь генерується повторно (один раз).
RETRY_PATTERNS = {
    "це добре": r"\bце\s+добре\b",
    "це гарно": r"\bце\s+гарно\b",
    "найкраще": r"найкращ",
    "правильне рішення": r"правильн\w*\s+рішенн",
    "гарна подушка": r"гарн\w*\s+подушк",
    "змінити стратегію": r"\bзмінити\s+стратегі",
    "вибрати стратегію": r"\bвибрати\s+стратегі",
    "зміни стратегію": r"\bзміни\s+стратегі",
}
_RETRY_RES = {phrase: re.compile(pattern, re.IGNORECASE) for phrase, pattern in RETRY_PATTERNS.items()}
_SAVINGS_WORD_RE = re.compile(r"заощадж", re.IGNORECASE)
CORRECTION_TEMPLATE = (
    "Попередня відповідь порушила правила: {violations}. "
    "Відповідай знову за правилами, без цих елементів."
)

# Число з можливими пробілами між тисячами і дробовою частиною: «12 345», «1 234,56», «36».
_NUMBER_RE = re.compile(r"\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
NUMBER_TOLERANCE_UAH = 1.0


def _extract_json(text: str) -> str:
    """Знімає обгортку markdown code fence (```json ... ```) з відповіді моделі, якщо вона є."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def extract_numbers(text: str) -> list[float]:
    """Повертає всі числа з тексту; пробіли між тисячами й десяткова кома враховуються."""
    numbers = []
    for raw in _NUMBER_RE.findall(text):
        cleaned = re.sub(r"[   ]", "", raw).replace(",", ".")
        numbers.append(float(cleaned))
    return numbers


def find_unknown_numbers(answer: str, allowed_text: str) -> list[float]:
    """Повертає числа з відповіді, яких немає в allowed_text з допуском NUMBER_TOLERANCE_UAH."""
    allowed = extract_numbers(allowed_text)
    return [
        n for n in extract_numbers(answer)
        if not any(abs(n - a) <= NUMBER_TOLERANCE_UAH for a in allowed)
    ]


def find_banned_words(text: str) -> list[str]:
    """Повертає заборонені слова (ключі BANNED_PATTERNS), знайдені в тексті."""
    return [word for word, pattern in _BANNED_RES.items() if pattern.search(text)]


def find_rule_violations(answer: str, context: dict, question: str) -> list[str]:
    """Повертає порушення, через які відповідь генерується повторно.

    Порушення — вираз з RETRY_PATTERNS або згадка заощаджень, коли в контексті
    немає сценарію з ними (context["savings_scenario"]) і питання не про
    заощадження.
    """
    violations = [f"вираз «{phrase}»" for phrase, pattern in _RETRY_RES.items() if pattern.search(answer)]
    if (_SAVINGS_WORD_RE.search(answer) and not context.get("savings_scenario")
            and not is_savings_question(question)):
        violations.append("згадка заощаджень, про які не питали")
    return violations


def _log_answer_issues(kind: str, answer: str, allowed_text: str) -> None:
    """Пише в лог невідомі числа й заборонені слова у відповіді моделі; відповідь не змінює."""
    unknown = find_unknown_numbers(answer, allowed_text)
    if unknown:
        logger.warning("радник (%s): у відповіді числа, яких немає в контексті: %s", kind, unknown)
    banned = find_banned_words(answer)
    if banned:
        logger.warning("радник (%s): у відповіді заборонені слова: %s", kind, banned)


def _format_context(context: dict) -> str:
    """Форматує речення контексту (з advisor_context.py) у текст для промпту."""
    sentences = context.get("sentences") or []
    return "\n".join(f"- {s}" for s in sentences) if sentences else "Даних поки недостатньо."


def _log_context(kind: str, context_text: str) -> None:
    """Пише в лог повний текст контексту, якщо ADVISOR_LOG_CONTEXT=1 (за замовчуванням вимкнено).

    Рівень WARNING, бо логування застосунку не налаштоване й нижчі рівні в лог
    сервера не потрапляють. user_id і токени в текст контексту не входять.
    """
    if os.environ.get("ADVISOR_LOG_CONTEXT") == "1":
        logger.warning("ADVISOR_LOG_CONTEXT (%s):\n%s", kind, context_text)


async def generate_questions(context: dict) -> list[str]:
    """Генерує три запитання, які користувач міг би поставити радникові.

    Args:
        context: Контекст з advisor_context.build_advisor_context().

    Returns:
        Список запитань. Якщо модель відповіла не JSON-масивом — список з одним
        рядком сирої відповіді.
    """
    context_text = _format_context(context)
    _log_context("questions", context_text)
    user_prompt = f"""На основі цих даних згенеруй рівно 3 короткі запитання, які людина могла б
поставити про свої борги і план. Поверни ЛИШЕ JSON-масив із 3 рядків українською.
Без пояснень, без markdown, тільки JSON-масив.

Дані:
{context_text}

Формат прикладу: ["Запитання 1?", "Запитання 2?", "Запитання 3?"]
"""

    response = await client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = _extract_json(response.content[0].text)
    _log_answer_issues("questions", text, context_text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Відповідь не є JSON — повертаємо її текст як є.
        return [text]


def _parse_answer(raw: str) -> tuple[str, list[str]]:
    """Розбирає JSON-відповідь моделі на (відповідь, наступні запитання); не JSON — (текст, [])."""
    text = _extract_json(raw)
    try:
        parsed = json.loads(text)
        return parsed["answer"], parsed.get("follow_up", [])
    except (json.JSONDecodeError, KeyError):
        return text, []


async def answer_question(question: str, context: dict) -> tuple[str, list[str]]:
    """Відповідає на запитання користувача й пропонує два наступні запитання.

    Якщо відповідь порушує правила (find_rule_violations), модель викликається
    ще один раз з повідомленням-коригуванням і повертається друга відповідь,
    навіть якщо вона теж порушує правила (тоді це пишеться в лог). Числа й
    заборонені слова фінальної відповіді лише пишуться в лог.

    Args:
        question: Запитання користувача.
        context: Контекст з advisor_context.build_advisor_context().

    Returns:
        Пара (відповідь, наступні запитання). Якщо відповідь моделі не JSON —
        (сирий текст, []).
    """
    context_text = _format_context(context)
    _log_context("answer", context_text)
    user_prompt = f"""Дані про борги і план людини:
{context_text}

Запитання: {question}

Дай відповідь за правилами. Потім запропонуй 2 короткі наступні запитання.

Формат відповіді — ЛИШЕ JSON:
{{"answer": "твоя відповідь тут", "follow_up": ["питання 1?", "питання 2?"]}}
"""
    messages = [{"role": "user", "content": user_prompt}]

    response = await client.messages.create(
        model=CLAUDE_MODEL, max_tokens=500, system=SYSTEM_PROMPT, messages=messages,
    )
    raw = response.content[0].text
    answer, follow_up = _parse_answer(raw)

    violations = find_rule_violations(answer, context, question)
    if violations:
        logger.warning("радник (answer): відповідь порушила правила, повторна генерація: %s", violations)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": CORRECTION_TEMPLATE.format(violations="; ".join(violations))},
        ]
        response = await client.messages.create(
            model=CLAUDE_MODEL, max_tokens=500, system=SYSTEM_PROMPT, messages=retry_messages,
        )
        answer, follow_up = _parse_answer(response.content[0].text)
        still = find_rule_violations(answer, context, question)
        if still:
            logger.warning("радник (answer): повторна відповідь теж порушила правила: %s", still)

    _log_answer_issues("answer", answer, f"{context_text}\n{question}")
    return answer, follow_up
