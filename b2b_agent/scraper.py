"""Парсер результатов поиска торгов B2B-Center (классический поиск по f_keyword).

Логинимся по логину/паролю, затем для каждого поискового запроса из конфига
запрашиваем страницу поиска /market/?f_keyword=... и разбираем HTML-таблицу
результатов. Слово(а) поиска задаются в config.yaml, поэтому сменить интересы
(проектирование → строительство и т.п.) можно без правки кода.
"""

import logging
import os
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString

from .models import Listing

log = logging.getLogger(__name__)

BASE_URL = "https://www.b2b-center.ru"
SEARCH_URL = "https://www.b2b-center.ru/market/"
LOGIN_URL = "https://www.b2b-center.ru/auth/credentials_ajax_login.html"
AUTH_URL = "https://www.b2b-center.ru/auth/openid/authorize/"
CLIENT_ID = "5d438a01-eda7-4a88-9a68-a21f3ce3d4b0"
ID_RE = re.compile(r"/market/view\.html\?id=(\d+)|/tender-(\d+)/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
}


class ScrapeError(Exception):
    pass


def _login(session: requests.Session, login: str, password: str) -> bool:
    """Авторизуется на сайте через логин и пароль."""
    try:
        auth_params = {
            "client_id": CLIENT_ID,
            "redirect_uri": "https://www.b2b-center.ru/app/next/main/",
            "response_type": "code",
            "scope": "openid",
            "response_mode": "query",
        }
        resp = session.get(AUTH_URL, params=auth_params, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml",
        }, timeout=30)

        csrf_match = re.search(r'name="login_form\[CSRFToken\]"[^>]*?value="([^"]+)"', resp.text)
        mfp_match = re.search(r'name="login_form\[MFPToken\]"[^>]*?value="([^"]+)"', resp.text)

        if not csrf_match:
            log.warning("CSRF токен не найден на странице входа")
            return False

        login_data = {
            "login_form[CSRFToken]": csrf_match.group(1),
            "login_form[location_form]": "form_with_error_page",
            "login_form[MFPToken]": mfp_match.group(1) if mfp_match else "",
            "login_form[login]": login,
            "login_form[password]": password,
        }
        login_resp = session.post(LOGIN_URL, data=login_data, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/json, text/javascript, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": resp.url,
        }, timeout=30)

        # Успешный вход подтверждаем по наличию сессионной куки
        if session.cookies.get("PHPSESSID") or login_resp.ok:
            log.info("Авторизация на B2B-Center выполнена")
            return True
        return False

    except Exception as e:
        log.warning("Ошибка при авторизации: %s", e)
        return False


def _extract_id(url: str) -> str | None:
    m = ID_RE.search(url)
    if m:
        return m.group(1) or m.group(2)
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _parse_search_page(html: str) -> list[Listing]:
    """Разбирает страницу результатов поиска. Каждый тендер — ссылка с data-lot_id."""
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []

    for a in soup.select("a[data-lot_id]"):
        href = (a.get("href") or "").split("#")[0]
        listing_id = a.get("data-lot_id") or _extract_id(href)
        if not listing_id or not href:
            continue

        url = urljoin(BASE_URL, href)

        # Блок с описанием и компанией
        desc_div = a.find("div", class_="search-results-title-desc")
        description = ""
        company = ""
        if desc_div:
            company_div = desc_div.find("div")
            if company_div:
                comp_text = _clean(company_div.get_text(" ", strip=True))
                # Убираем ведущий номер тендера
                company = re.sub(r"^\d+\s*", "", comp_text)
                company_div.extract()
            description = _clean(desc_div.get_text(" ", strip=True))

        # Заголовок — прямой текст ссылки (до вложенных блоков)
        title_parts = [c for c in a.contents if isinstance(c, NavigableString)]
        title = _clean(" ".join(title_parts)) or f"Тендер {listing_id}"

        listings.append(
            Listing(
                listing_id=str(listing_id),
                title=title,
                url=url,
                company=company[:200],
                description=description,
            )
        )

    return listings


def scrape_market(
    market_url: str = SEARCH_URL,
    pages: int = 3,
    request_delay: float = 3.0,
    search_queries: list[str] | None = None,
) -> list[Listing]:
    """Ищет торги по каждому слову из search_queries и собирает результаты."""
    session = requests.Session()

    login = os.environ.get("B2B_LOGIN", "")
    password = os.environ.get("B2B_PASSWORD", "")
    if login and password:
        _login(session, login, password)
    else:
        log.warning("B2B_LOGIN или B2B_PASSWORD не заданы — авторизация пропущена")

    queries = [q for q in (search_queries or []) if q.strip()]
    if not queries:
        log.warning("Не заданы слова для поиска (search_queries) — нечего искать")
        return []

    all_listings: list[Listing] = []
    seen_ids: set[str] = set()

    for query in queries:
        for page in range(pages):
            params = {
                "f_keyword": query,
                "search_type": "2",
                "price_currency": "0",
                "date": "1",
                "trade": "all",
            }
            if page > 0:
                params["page"] = page + 1

            try:
                resp = session.get(SEARCH_URL, params=params, headers=HEADERS, timeout=30)
                if resp.status_code != 200:
                    log.warning("Поиск '%s' стр.%d: HTTP %d", query, page + 1, resp.status_code)
                    break
            except requests.RequestException as e:
                log.warning("Поиск '%s' стр.%d: ошибка сети %s", query, page + 1, e)
                break

            page_listings = _parse_search_page(resp.text)
            if not page_listings:
                if page == 0:
                    log.warning("Поиск '%s': ничего не найдено", query)
                break

            new_count = 0
            for lst in page_listings:
                if lst.listing_id not in seen_ids:
                    seen_ids.add(lst.listing_id)
                    all_listings.append(lst)
                    new_count += 1
            log.info("Поиск '%s' стр.%d: %d найдено (%d новых)",
                     query, page + 1, len(page_listings), new_count)

            if new_count == 0:
                break  # дальше только повторы — след. страница не нужна

            time.sleep(request_delay)

    return all_listings
