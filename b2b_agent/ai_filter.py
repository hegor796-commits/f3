"""ИИ-оценка релевантности объявлений через OpenAI API.

Каждое объявление, прошедшее фильтр по ключевым словам, отправляется
в OpenAI вместе с профилем интересов пользователя. Модель возвращает
структурированный ответ: оценку релевантности 0-10 и краткое резюме
для уведомления.
"""

import logging
import os

from openai import OpenAI
from pydantic import BaseModel, Field

from .models import Listing

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — ассистент по отбору B2B-тендеров и закупок.
Тебе дают профиль интересов пользователя и одно объявление с торговой площадки.
Оцени, насколько объявление действительно соответствует интересам пользователя,
а не просто формально совпало по ключевым словам.

Оценка score от 0 до 10:
- 0-3: не относится к интересам (случайное совпадение слов)
- 4-5: слабое отношение, сомнительно
- 6-8: релевантно, стоит посмотреть
- 9-10: точное попадание в профиль интересов

В summary напиши 1-2 коротких предложения по-русски: что закупают/предлагают
и почему это (не)интересно пользователю."""


class RelevanceCheck(BaseModel):
    score: int = Field(description="Релевантность объявления интересам пользователя, 0-10")
    summary: str = Field(description="Краткое резюме объявления на русском, 1-2 предложения")


def is_available() -> bool:
    """Есть ли ключ API (иначе агент работает без ИИ-фильтра)."""
    return bool(os.environ.get("OPENAI_API_KEY"))


def check_relevance(
    listings: list[Listing],
    interest_profile: str,
    model: str = "gpt-4o",
    max_checks: int = 20,
) -> list[Listing]:
    """Проставляет ai_score и ai_summary каждому объявлению.

    При ошибке API объявление сохраняет ai_score=None — решение о его
    отправке принимает вызывающий код (по умолчанию отправляем, чтобы
    сбой ИИ не приводил к потере потенциально важных уведомлений).
    """
    client = OpenAI()

    for lst in listings[:max_checks]:
        user_message = (
            f"Профиль интересов пользователя:\n{interest_profile.strip()}\n\n"
            f"Объявление:\n"
            f"Название: {lst.title}\n"
            f"Описание: {lst.description or 'не указано'}\n"
            f"Организатор: {lst.company or 'не указан'}\n"
            f"Срок: {lst.end_date or 'не указан'}\n"
            f"Совпавшие ключевые слова: {', '.join(lst.matched_keywords)}"
        )
        try:
            response = client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                response_format=RelevanceCheck,
                max_tokens=1024,
            )
            parsed = response.choices[0].message.parsed
            lst.ai_score = parsed.score
            lst.ai_summary = parsed.summary
            log.info("ИИ-оценка %s: %d/10 — %s", lst.listing_id, parsed.score, lst.title[:60])
        except Exception as e:
            log.warning("Ошибка OpenAI API для объявления %s: %s", lst.listing_id, e)

    if len(listings) > max_checks:
        log.info(
            "Лимит ИИ-проверок (%d) исчерпан, %d объявлений пропущено без оценки",
            max_checks,
            len(listings) - max_checks,
        )
    return listings
