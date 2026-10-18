"""
AI-радник на Claude API.

Модель лише пояснює українською вже пораховані числа (план MILP, агрегати
витрат, оцінки з advisor_context.py). Фінансових розрахунків вона не робить:
усі числа передаються їй у контексті як готові факти.
"""
import json
import re

from anthropic import AsyncAnthropic

from app.core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Ти — асистент персонального фінансового радника. Твоя задача —
пояснювати результати фінансового аналізу простою, зрозумілою українською мовою.

Правила:
- Завжди відповідай українською
- Використовуй терміни: стратегія MILP (математично оптимальна), "лавина" (avalanche), "сніжок" (snowball)
- Стисло — максимум 150 слів
- Ніколи не згадуй зовнішні сайти чи ресурси
- НІКОЛИ не вигадуй цифри — використовуй лише ті дані, що надані в контексті
- Фокусуйся на практичній пораді для користувача
- Якщо дані позначені як 'low_confidence' (низька впевненість прогнозу) —
  обов'язково згадай це користувачу, не подавай прогноз як точний факт
"""


def _extract_json(text: str) -> str:
    """Знімає обгортку markdown code fence (```json ... ```) з відповіді моделі, якщо вона є."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _format_context(context: dict) -> str:
    """Форматує агрегований контекст (з advisor_context.py) у текст для промпту."""
    lines = []
    if context.get("total_debt") is not None:
        lines.append(f"- Загальний борг: {context['total_debt']} грн")
    if context.get("monthly_budget_for_debt") is not None:
        lines.append(f"- Прогнозований вільний бюджет на місяць: {context['monthly_budget_for_debt']} грн")
    if context.get("forecast_low_confidence"):
        lines.append("- УВАГА: прогноз побудований на малій кількості даних, точність низька")
    if context.get("best_strategy"):
        lines.append(f"- Оптимальна стратегія погашення боргу: {context['best_strategy'].upper()}")
    if context.get("months_to_payoff") is not None:
        lines.append(f"- Місяців до повного погашення боргу: {context['months_to_payoff']}")
    if context.get("total_interest_paid") is not None:
        lines.append(f"- Загальна переплата відсотків за обраною стратегією: {context['total_interest_paid']} грн")
    if context.get("savings_vs_avalanche") is not None:
        lines.append(f"- Економія оптимальної стратегії проти популярної 'лавини': {context['savings_vs_avalanche']} грн")
    if context.get("top_spending_category"):
        lines.append(
            f"- Найбільша категорія витрат за останні 30 днів: "
            f"{context['top_spending_category']} ({context.get('top_spending_category_amount')} грн)"
        )
    if context.get("cash_withdrawn_last_30_days"):
        lines.append(f"- Знято готівкою: {context['cash_withdrawn_last_30_days']} грн (за останні 30 днів)")
    if context.get("asset_arbitrage_annual_savings") is not None:
        lines.append(
            "- Можлива економія: наразі бюджет не покриває повний план погашення боргів. "
            f"Зняття {context['asset_arbitrage_amount']} грн з активу "
            f"\"{context['asset_arbitrage_asset_name']}\" ({context['asset_arbitrage_liquidity_note']}) "
            f"для дострокового погашення \"{context['asset_arbitrage_debt_name']}\" може заощадити "
            f"≈{context['asset_arbitrage_annual_savings']} грн/рік на відсотках."
        )
    if context.get("assets_applied_amount") is not None:
        line = (
            f"- Що якщо застосувати наявні ліквідні активи ({context['assets_applied_amount']} грн) "
            "одноразово до погашення боргів сьогодні: "
        )
        if context.get("debt_payoff_months_saved") is not None:
            line += f"погашення скоротиться на {context['debt_payoff_months_saved']} міс."
        else:
            line += f"план вийде на повне погашення за {context['adjusted_months_to_payoff']} міс."
        if context.get("interest_saved_by_assets"):
            line += f", економія на відсотках ≈{context['interest_saved_by_assets']} грн."
        lines.append(line)
    return "\n".join(lines) if lines else "Даних поки недостатньо."


async def generate_questions(context: dict) -> list[str]:
    """Генерує три запитання, які користувач міг би поставити радникові.

    Args:
        context: Агрегований контекст з advisor_context.build_advisor_context().

    Returns:
        Список запитань. Якщо модель відповіла не JSON-масивом — список з одним
        рядком сирої відповіді.
    """
    user_prompt = f"""На основі цих фінансових даних згенеруй рівно 3 релевантних запитання,
які користувач міг би захотіти поставити. Поверни ЛИШЕ JSON-масив із 3 рядків
українською. Без пояснень, без markdown, тільки JSON-масив.

Дані:
{_format_context(context)}

Формат прикладу: ["Запитання 1?", "Запитання 2?", "Запитання 3?"]
"""

    response = await client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = _extract_json(response.content[0].text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Відповідь не є JSON — повертаємо її текст як є.
        return [text]


async def answer_question(question: str, context: dict) -> tuple[str, list[str]]:
    """Відповідає на запитання користувача й пропонує два наступні запитання.

    Args:
        question: Запитання користувача.
        context: Агрегований контекст з advisor_context.build_advisor_context().

    Returns:
        Пара (відповідь, наступні запитання). Якщо відповідь моделі не JSON —
        (сирий текст, []).
    """
    user_prompt = f"""Контекст фінансового стану користувача:
{_format_context(context)}

Запитання користувача: {question}

Дай конкретну, практичну відповідь на основі наданих даних. Потім запропонуй
2 логічних наступних запитання, які міг би поставити користувач.

Формат відповіді — ЛИШЕ JSON:
{{"answer": "твоя відповідь тут", "follow_up": ["питання 1?", "питання 2?"]}}
"""

    response = await client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = _extract_json(response.content[0].text)
    try:
        parsed = json.loads(text)
        return parsed["answer"], parsed.get("follow_up", [])
    except (json.JSONDecodeError, KeyError):
        return text, []
