"""Парсер списка торговых процедур B2B-Center.

Стратегия: сначала пробуем разобрать таблицу торгов по известной
разметке, если она поменялась — fallback на поиск любых ссылок вида
/market/view.html?id=NNN, чтобы агент не ломался при редизайне.
"""

import logging
import os
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .models import Listing

log = logging.getLogger(__name__)

BASE_URL = "https://www.b2b-center.ru"
VIEW_LINK_RE = re.compile(r"/market/view\.html\?id=(\d+)")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "Referer": "https://www.b2b-center.ru/",
}


class ScrapeError(Exception):
    pass


def fetch_page(url: str, session: requests.Session, timeout: int = 30) -> str:
    resp = session.get(url, headers=HEADERS, timeout=timeout)
    if resp.status_code != 200:
        raise ScrapeError(f"HTTP {resp.status_code} for {url}")
    return resp.text


def _build_session() -> requests.Session:
    session = requests.Session()
    cookies_str = os.environ.get("B2B_COOKIES", "")
    if cookies_str:
        for part in cookies_str.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                session.cookies.set(name.strip(), value.strip(), domain="b2b-center.ru")
        log.info("Используем куки авторизации B2B-Center")
    return session


def _parse_table_rows(soup: BeautifulSoup) -> list[Listing]:
    """Основной путь: таблица со списком торгов."""
    listings: list[Listing] = []
    for row in soup.select("table tr"):
        link = row.find("a", href=VIEW_LINK_RE)
        if not link:
            continue
        m = VIEW_LINK_RE.search(link["href"])
        if not m:
            continue
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        listings.append(
            Listing(
                listing_id=m.group(1),
                title=link.get_text(" ", strip=True),
                url=urljoin(BASE_URL, link["href"]),
                company=cells[1] if len(cells) > 1 else "",
                end_date=cells[-1] if cells else "",
            )
        )
    return listings


def _parse_fallback_links(soup: BeautifulSoup) -> list[Listing]:
    """Запасной путь: любые ссылки на карточки торгов на странице."""
    listings: list[Listing] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=VIEW_LINK_RE):
        m = VIEW_LINK_RE.search(link["href"])
        listing_id = m.group(1)
        title = link.get_text(" ", strip=True)
        if listing_id in seen or len(title) < 10:
            continue
        seen.add(listing_id)
        listings.append(
            Listing(
                listing_id=listing_id,
                title=title,
                url=urljoin(BASE_URL, link["href"]),
            )
        )
    return listings


def parse_market_page(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings = _parse_table_rows(soup)
    if not listings:
        listings = _parse_fallback_links(soup)
    return listings


def scrape_market(
    market_url: str,
    pages: int = 1,
    request_delay: float = 3.0,
) -> list[Listing]:
    """Собирает объявления с нескольких страниц списка торгов.

    Пагинация B2B-Center: /market/?from=20, /market/?from=40 ...
    (по 20 позиций на страницу).
    """
    session = _build_session()
    all_listings: list[Listing] = []
    seen_ids: set[str] = set()

    for page in range(pages):
        url = market_url if page == 0 else f"{market_url.rstrip('/')}/?from={page * 20}"
        try:
            html = fetch_page(url, session)
        except (ScrapeError, requests.RequestException) as e:
            log.warning("Не удалось получить страницу %s: %s", url, e)
            break

        page_listings = parse_market_page(html)
        if not page_listings:
            log.warning("На странице %s не найдено объявлений (изменилась разметка?)", url)
            break

        new_count = 0
        for lst in page_listings:
            if lst.listing_id not in seen_ids:
                seen_ids.add(lst.listing_id)
                all_listings.append(lst)
                new_count += 1
        log.info("Страница %d: %d объявлений (%d новых)", page + 1, len(page_listings), new_count)

        if page < pages - 1:
            time.sleep(request_delay)

    return all_listings
