"""
AI-радник: запитання-підказки й відповіді на запитання користувача.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.models.user import User
from app.services.advisor_context import build_advisor_context
from app.services.llm_advisor import generate_questions, answer_question

router = APIRouter(prefix="/advisor", tags=["advisor"])

# Запитання, що показується першим, коли є порада щодо використання активу.
ASSET_ARBITRAGE_QUESTION = "Чи варто скористатися заощадженнями, щоб швидше закрити борг?"


class QuestionRequest(BaseModel):
    """Тіло запиту: запитання користувача."""
    question: str


def _apply_asset_arbitrage_pin(questions: list[str], context: dict) -> list[str]:
    """Ставить ASSET_ARBITRAGE_QUESTION першим, якщо в контексті є порада щодо активу.

    Список обрізається до трьох запитань.
    """
    if context.get("asset_arbitrage"):
        return [ASSET_ARBITRAGE_QUESTION] + questions[:2]
    return questions


@router.get("/questions")
async def get_questions(
    current_user: User = Depends(get_current_user),
):
    """Генерує три запитання-підказки за поточним фінансовим станом користувача.

    Повертає також контекст, на якому вони побудовані. Повертає 500, якщо
    виклик моделі не вдався.
    """
    try:
        context = await build_advisor_context(current_user.id)
        questions = await generate_questions(context)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    questions = _apply_asset_arbitrage_pin(questions, context)

    return {"questions": questions, "context_used": context}


@router.post("/answer")
async def get_answer(
    request: QuestionRequest,
    current_user: User = Depends(get_current_user),
):
    """Відповідає на запитання користувача й пропонує два наступні запитання.

    Повертає 500, якщо виклик моделі не вдався.
    """
    try:
        context = await build_advisor_context(current_user.id, question=request.question)
        answer, follow_up = await answer_question(request.question, context)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"answer": answer, "follow_up": follow_up}
