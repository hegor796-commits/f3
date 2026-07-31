"""Парсер списка торговых процедур B2B-Center через JSON API."""

import logging
import os
import re
import time
from urllib.parse import urljoin

import requests

from .models import Listing

log = logging.getLogger(__name__)

BASE_URL = "https://www.b2b-center.ru"
API_URL = "https://www.b2b-center.ru/site/api/v1/market_for_me/main_page/participant/"
VIEW_LINK_RE = re.compile(r"/market/view\.html\?id=(\d+)|/app/market/.+/tender-(\d+)/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "Referer": "https://www.b2b-center.ru/app/next/main/",
}


class ScrapeError(Exception):
    pass


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

    bearer = os.environ.get("B2B_BEARER", "")
    if bearer:
        session.headers.update({"Authorization": f"Bearer {bearer}"})
        log.info("Используем Bearer токен B2B-Center")

    return session


def _extract_id(url: str) -> str | None:
    m = VIEW_LINK_RE.search(url)
    if m:
        return m.group(1) or m.group(2)
    return None


def scrape_market(
    market_url: str,
    pages: int = 1,
    request_delay: float = 3.0,
) -> list[Listing]:
    session = _build_session()
    all_listings: list[Listing] = []
    seen_ids: set[str] = set()

    for page in range(pages):
        params = {
            "page": page + 1,
            "page_size": 20,
            "buy_sell": "buy",
        }
        try:
            resp = session.get(API_URL, headers=HEADERS, params=params, timeout=30)
            if resp.status_code != 200:
                log.warning("API вернул статус %d на странице %d", resp.status_code, page + 1)
                break
            data = resp.json()
        except Exception as e:
            log.warning("Ошибка запроса к API на странице %d: %s", page + 1, e)
            break

        trades = data.get("trades", [])
        if not trades:
            log.warning("API вернул пустой список торгов на странице %d", page + 1)
            break

        new_count = 0
        for trade in trades:
            url = trade.get("url", "")
            listing_id = _extract_id(url)
            if not listing_id:
                # используем sphinx_id как запасной вариант
                listing_id = str(trade.get("sphinx_id", ""))
            if not listing_id or listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)

            full_url = urljoin(BASE_URL, url) if url.startswith("/") else url
            # очищаем HTML-сущности в названии
            title = re.sub(r"&nbsp;?", " ", trade.get("title", "")).strip()
            description = re.sub(r"&nbsp;?", " ", trade.get("description", "")).strip()
            company = trade.get("org_name_short", "")
            end_date = trade.get("date_actual", "")

            all_listings.append(
                Listing(
                    listing_id=listing_id,
                    title=title,
                    url=full_url,
                    company=company,
                    end_date=end_date,
                    description=description,
                )
            )
            new_count += 1

        log.info("Страница %d: %d объявлений (%d новых)", page + 1, len(trades), new_count)

        if page < pages - 1:
            time.sleep(request_delay)

    return all_listings
