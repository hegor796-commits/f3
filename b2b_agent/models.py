"""Общие структуры данных."""

from dataclasses import dataclass, field


@dataclass
class Listing:
    """Объявление (торговая процедура) с площадки."""

    listing_id: str
    title: str
    url: str
    company: str = ""
    end_date: str = ""
    description: str = ""
    extra: dict = field(default_factory=dict)

    # Заполняется на этапе ИИ-оценки
    ai_score: int | None = None
    ai_summary: str = ""
    matched_keywords: list[str] = field(default_factory=list)
