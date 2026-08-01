"""Пробник: проверяет классический поиск по f_keyword на B2B-Center.

Запуск на сервере:
    python3 -m scripts.probe_search проектирование
"""

import os
import re
import sys

import requests

from b2b_agent import scraper


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    load_env()
    keyword = sys.argv[1] if len(sys.argv) > 1 else "проектирование"

    session = requests.Session()
    login = os.environ.get("B2B_LOGIN", "")
    password = os.environ.get("B2B_PASSWORD", "")
    ok = scraper._login(session, login, password)
    print("Логин:", "успех" if ok else "не подтверждён (но пробуем дальше)")

    # Классический поиск — отдаёт HTML с таблицей результатов
    url = "https://www.b2b-center.ru/market/"
    params = {
        "f_keyword": keyword,
        "search_type": "2",
        "price_currency": "0",
        "date": "1",
        "trade": "all",
    }
    resp = session.get(url, params=params, headers={
        "User-Agent": scraper.HEADERS["User-Agent"],
        "Accept": "text/html,application/xhtml+xml",
    }, timeout=30)

    print("URL:", resp.url)
    print("STATUS:", resp.status_code, "| SIZE:", len(resp.text))

    ids = re.findall(r"/market/view\.html\?id=(\d+)", resp.text)
    tender_links = re.findall(r"/tender-(\d+)/", resp.text)
    print("Ссылок view.html?id=:", len(ids), "| уникальных:", len(set(ids)))
    print("Ссылок /tender-N/:", len(tender_links), "| уникальных:", len(set(tender_links)))

    with open("search_probe.html", "w", encoding="utf-8") as f:
        f.write(resp.text)
    print("Сохранил страницу в search_probe.html")

    # Показываем первые найденные заголовки для примера
    titles = re.findall(r'/market/view\.html\?id=\d+"[^>]*>([^<]{10,120})', resp.text)
    for t in titles[:5]:
        print("  •", t.strip())


if __name__ == "__main__":
    main()
