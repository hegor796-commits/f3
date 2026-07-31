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
LOGIN_URL = "https://www.b2b-center.ru/auth/credentials_ajax_login.html"
AUTH_URL = "https://www.b2b-center.ru/auth/openid/authorize/"
TOKEN_URL = "https://www.b2b-center.ru/auth/openid/token/"
CLIENT_ID = "5d438a01-eda7-4a88-9a68-a21f3ce3d4b0"
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


def _login(session: requests.Session, login: str, password: str) -> bool:
    """Авторизуется на сайте через логин и пароль."""
    try:
        # Шаг 1: получаем страницу логина чтобы забрать CSRF токен
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

        # Ищем CSRF токен в HTML (между name и value могут быть другие атрибуты)
        csrf_match = re.search(r'name="login_form\[CSRFToken\]"[^>]*?value="([^"]+)"', resp.text)
        mfp_match = re.search(r'name="login_form\[MFPToken\]"[^>]*?value="([^"]+)"', resp.text)

        if not csrf_match:
            log.warning("CSRF токен не найден на странице входа")
            return False

        csrf_token = csrf_match.group(1)
        mfp_token = mfp_match.group(1) if mfp_match else ""

        # Шаг 2: отправляем логин и пароль
        login_data = {
            "login_form[CSRFToken]": csrf_token,
            "login_form[location_form]": "form_with_error_page",
            "login_form[MFPToken]": mfp_token,
            "login_form[login]": login,
            "login_form[password]": password,
        }
        login_resp = session.post(LOGIN_URL, data=login_data, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/json, text/javascript, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": resp.url,
        }, timeout=30)

        result = login_resp.json()
        if result.get("status") == "ok" or result.get("redirect"):
            log.info("Авторизация на B2B-Center успешна")
            return True
        else:
            log.warning("Ошибка авторизации: %s", result)
            return False

    except Exception as e:
        log.warning("Ошибка при авторизации: %s", e)
        return False


def _get_access_token(session: requests.Session) -> str | None:
    """Получает access_token через silent auth после логина."""
    try:
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": "https://www.b2b-center.ru/app/next/silent-auth/",
            "response_type": "code",
            "scope": "openid",
            "prompt": "none",
        }
        resp = session.get(AUTH_URL, params=params, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml",
        }, allow_redirects=False, timeout=15)

        location = resp.headers.get("Location", "")
        code_match = re.search(r"[?&]code=([^&]+)", location)
        if not code_match:
            log.warning("Не удалось получить code: %s", location[:200])
            return None

        code = code_match.group(1)

        token_resp = session.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://www.b2b-center.ru/app/next/silent-auth/",
            "client_id": CLIENT_ID,
        }, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Content-Type": "application/x-www-form-urlencoded",
        }, timeout=15)

        token = token_resp.json().get("access_token")
        if token:
            log.info("Bearer токен успешно получен")
        return token

    except Exception as e:
        log.warning("Ошибка получения токена: %s", e)
        return None


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
    session = requests.Session()

    # Авторизуемся через логин/пароль
    login = os.environ.get("B2B_LOGIN", "")
    password = os.environ.get("B2B_PASSWORD", "")

    if login and password:
        _login(session, login, password)
    else:
        log.warning("B2B_LOGIN или B2B_PASSWORD не заданы — работаем без авторизации")

    # Получаем Bearer токен
    token = _get_access_token(session)
    if not token:
        log.warning("Bearer токен недоступен — запросы могут вернуть 401")

    api_headers = {**HEADERS}
    if token:
        api_headers["Authorization"] = f"Bearer {token}"

    all_listings: list[Listing] = []
    seen_ids: set[str] = set()

    for page in range(pages):
        params = {
            "page": page + 1,
            "page_size": 20,
            "buy_sell": "buy",
        }
        try:
            resp = session.get(API_URL, headers=api_headers, params=params, timeout=30)
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
                listing_id = str(trade.get("sphinx_id", ""))
            if not listing_id or listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)

            full_url = urljoin(BASE_URL, url) if url.startswith("/") else url
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
