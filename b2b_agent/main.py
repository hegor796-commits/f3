"""Точка входа агента.

Команды:
    python -m b2b_agent once           — один цикл проверки (для cron)
    python -m b2b_agent run            — бесконечный цикл с интервалом из конфига
    python -m b2b_agent test-telegram  — проверить отправку сообщения в Telegram
    python -m b2b_agent get-chat-id    — узнать свой chat_id (напиши боту /start и запусти)
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import requests
import yaml

from . import ai_filter, filters, notifier, scraper
from .state import SeenStore

log = logging.getLogger("b2b_agent")


def load_env(path: str = ".env") -> None:
    """Минимальный загрузчик .env без внешних зависимостей."""
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_cycle(cfg: dict) -> None:
    src = cfg.get("source", {})
    ai_cfg = cfg.get("ai", {})
    # DB_PATH из окружения имеет приоритет над config.yaml —
    # на Railway сюда указывают путь примонтированного volume (напр. /data/state.db)
    db_path = os.environ.get("DB_PATH") or cfg.get("storage", {}).get("db_path", "state.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store = SeenStore(db_path)

    try:
        # 1. Парсим площадку
        listings = scraper.scrape_market(
            market_url=src.get("market_url", "https://www.b2b-center.ru/market/"),
            pages=int(src.get("pages", 1)),
            request_delay=float(src.get("request_delay", 3)),
        )
        log.info("Собрано объявлений: %d", len(listings))

        # 2. Отбрасываем уже виденные
        fresh = [lst for lst in listings if not store.is_seen(lst.listing_id)]
        log.info("Новых (не виденных ранее): %d", len(fresh))

        # 3. Фильтр по ключевым словам
        matched = filters.keyword_filter(
            fresh,
            keywords=cfg.get("keywords", []),
            exclude_keywords=cfg.get("exclude_keywords", []),
        )
        log.info("Прошло фильтр по ключевым словам: %d", len(matched))

        # Всё, что не прошло фильтр, помечаем как виденное без уведомления
        matched_ids = {lst.listing_id for lst in matched}
        for lst in fresh:
            if lst.listing_id not in matched_ids:
                store.mark_seen(lst.listing_id, lst.title, notified=False)

        # 4. ИИ-оценка релевантности
        to_notify = matched
        if matched and ai_cfg.get("enabled", True):
            if ai_filter.is_available():
                min_score = int(ai_cfg.get("min_score", 6))
                ai_filter.check_relevance(
                    matched,
                    interest_profile=ai_cfg.get("interest_profile", ""),
                    model=ai_cfg.get("model", "claude-opus-4-8"),
                    max_checks=int(ai_cfg.get("max_checks_per_cycle", 20)),
                )
                # Без оценки (сбой API / лимит проверок) — отправляем,
                # чтобы не потерять потенциально важное
                to_notify = [
                    lst for lst in matched
                    if lst.ai_score is None or lst.ai_score >= min_score
                ]
                rejected = [lst for lst in matched if lst not in to_notify]
                for lst in rejected:
                    store.mark_seen(lst.listing_id, lst.title, notified=False, ai_score=lst.ai_score)
                log.info("Прошло ИИ-фильтр (score >= %d): %d", min_score, len(to_notify))
            else:
                log.warning("ANTHROPIC_API_KEY не задан — работаем без ИИ-фильтра")

        # 5. Уведомления в Telegram
        for lst in to_notify:
            try:
                notifier.send_message(
                    notifier.format_message(lst),
                    parse_mode=cfg.get("telegram", {}).get("parse_mode", "HTML"),
                )
                store.mark_seen(lst.listing_id, lst.title, notified=True, ai_score=lst.ai_score)
                log.info("Отправлено: %s", lst.title[:70])
                time.sleep(1)
            except notifier.TelegramError as e:
                # Не помечаем как seen — попробуем снова в следующем цикле
                log.error("Не удалось отправить уведомление по %s: %s", lst.listing_id, e)
    finally:
        store.close()


def cmd_get_chat_id() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Задай TELEGRAM_BOT_TOKEN в .env, напиши своему боту /start и запусти снова.")
    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
    data = resp.json()
    if not data.get("ok"):
        sys.exit(f"Ошибка Telegram API: {data}")
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat", {})
        if chat.get("id"):
            name = chat.get("username") or chat.get("title") or chat.get("first_name", "")
            chats[chat["id"]] = name
    if not chats:
        print("Обновлений нет. Напиши боту любое сообщение (например /start) и запусти снова.")
    else:
        print("Найденные чаты (chat_id — имя):")
        for cid, name in chats.items():
            print(f"  {cid} — {name}")
        print("\nВыбери нужный chat_id и запиши его в .env как TELEGRAM_CHAT_ID")


def main() -> None:
    parser = argparse.ArgumentParser(description="ИИ-агент мониторинга B2B-Center")
    parser.add_argument(
        "command",
        choices=["once", "run", "test-telegram", "get-chat-id"],
        help="once — один цикл; run — бесконечный цикл; "
        "test-telegram — тест уведомления; get-chat-id — узнать chat_id",
    )
    parser.add_argument("--config", default="config.yaml", help="Путь к config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    if args.command == "get-chat-id":
        cmd_get_chat_id()
        return

    if args.command == "test-telegram":
        notifier.send_message("✅ Тест: бот мониторинга B2B-Center подключён и работает.")
        print("Сообщение отправлено — проверь Telegram.")
        return

    cfg = load_config(args.config)

    if args.command == "once":
        run_cycle(cfg)
        return

    interval = int(cfg.get("schedule", {}).get("interval_minutes", 30)) * 60
    log.info("Запуск в режиме демона, интервал %d мин", interval // 60)
    while True:
        try:
            run_cycle(cfg)
        except Exception:
            log.exception("Ошибка цикла — продолжаем работу")
        log.info("Следующая проверка через %d мин", interval // 60)
        time.sleep(interval)


if __name__ == "__main__":
    main()
