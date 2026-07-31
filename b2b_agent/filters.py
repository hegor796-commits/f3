"""Первичная фильтрация объявлений по ключевым словам."""

from .models import Listing


def keyword_filter(
    listings: list[Listing],
    keywords: list[str],
    exclude_keywords: list[str] | None = None,
) -> list[Listing]:
    """Оставляет объявления, где встречается хотя бы одно ключевое слово
    и нет ни одного слова-исключения. Совпавшие слова записываются
    в listing.matched_keywords.
    """
    keywords_lower = [k.lower() for k in keywords if k.strip()]
    excludes_lower = [k.lower() for k in (exclude_keywords or []) if k.strip()]

    result: list[Listing] = []
    for lst in listings:
        haystack = f"{lst.title} {lst.company} {lst.description}".lower()

        if any(ex in haystack for ex in excludes_lower):
            continue

        matched = [kw for kw in keywords_lower if kw in haystack]
        if matched:
            lst.matched_keywords = matched
            result.append(lst)

    return result
