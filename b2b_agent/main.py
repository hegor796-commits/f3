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
import re
import sys
import time
from pathlib import Path

import requests
import yaml

from . import ai_filter, notifier, scraper
from .state import Store

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


def _open_store(cfg: dict) -> Store:
    db_path = os.environ.get("DB_PATH") or cfg.get("storage", {}).get("db_path", "state.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return Store(db_path)


def process_for_user(
    cfg: dict,
    store: Store,
    session,
    chat_id: str,
    queries: list[str],
) -> int:
    """Ищет тендеры по словам пользователя, шлёт ему новые. Возвращает число отправленных."""
    src = cfg.get("source", {})
    ai_cfg = cfg.get("ai", {})
    parse_mode = cfg.get("telegram", {}).get("parse_mode", "HTML")

    listings = scraper.search_listings(
        session,
        queries,
        pages=int(src.get("pages", 1)),
        request_delay=float(src.get("request_delay", 3)),
    )

    # Только те, что этому пользователю ещё не слали
    fresh = [lst for lst in listings if not store.is_seen(chat_id, lst.listing_id)]

    # Отсеиваем слова-исключения (сам поиск на сайте уже отобрал по словам)
    excludes = [e.lower() for e in cfg.get("exclude_keywords", []) if e.strip()]
    matched = []
    for lst in fresh:
        haystack = f"{lst.title} {lst.company} {lst.description}".lower()
        if any(ex in haystack for ex in excludes):
            store.mark_seen(chat_id, lst.listing_id, lst.title)
            continue
        lst.matched_keywords = queries
        matched.append(lst)

    # ИИ-оценка (если доступна)
    to_notify = matched
    if matched and ai_cfg.get("enabled", True) and ai_filter.is_available():
        min_score = int(ai_cfg.get("min_score", 6))
        ai_filter.check_relevance(
            matched,
            interest_profile=ai_cfg.get("interest_profile", ""),
            model=ai_cfg.get("model", "gpt-4o"),
            max_checks=int(ai_cfg.get("max_checks_per_cycle", 20)),
        )
        to_notify = [l for l in matched if l.ai_score is None or l.ai_score >= min_score]
        for l in matched:
            if l not in to_notify:
                store.mark_seen(chat_id, l.listing_id, l.title)

    sent = 0
    for lst in to_notify:
        try:
            notifier.send_message(
                notifier.format_message(lst), parse_mode=parse_mode, chat_id=chat_id
            )
            store.mark_seen(chat_id, lst.listing_id, lst.title)
            sent += 1
            time.sleep(1)
        except notifier.TelegramError as e:
            log.error("Не удалось отправить %s пользователю %s: %s", lst.listing_id, chat_id, e)
    if sent:
        log.info("Пользователю %s отправлено: %d", chat_id, sent)
    return sent


def run_cycle(cfg: dict) -> int:
    """Один цикл для режимов once/run — по chat_id из .env и словам из конфига."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    queries = cfg.get("source", {}).get("search_queries", [])
    store = _open_store(cfg)
    try:
        session = scraper.build_logged_session()
        return process_for_user(cfg, store, session, chat_id, queries)
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


def _main_menu() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🔍 Показать новые тендеры", "callback_data": "show_new"}],
            [{"text": "✏️ Задать слова поиска", "callback_data": "set_keywords"}],
            [{"text": "📋 Мои слова", "callback_data": "my_keywords"}],
            [{"text": "🔔 Слова по умолчанию", "callback_data": "reset_keywords"}],
            [{"text": "⏸ Отписаться", "callback_data": "unsubscribe"}],
        ]
    }


WELCOME = (
    "🤖 <b>Бот мониторинга тендеров B2B-Center</b>\n\n"
    "Я каждые 30 минут ищу новые тендеры и присылаю тебе (без повторов).\n\n"
    "По умолчанию ищу по словам, заданным администратором. Хочешь свои — "
    "нажми «✏️ Задать слова поиска» и напиши их через запятую.\n\n"
    "Выбери действие:"
)


def _do_show_new(cfg: dict, store: Store, chat_id: str, default_queries: list[str]) -> None:
    store.add_user(chat_id)
    queries = store.get_keywords(chat_id) or default_queries
    try:
        notifier.send_message("🔍 Проверяю сайт, подожди…", chat_id=chat_id)
        session = scraper.build_logged_session()
        sent = process_for_user(cfg, store, session, chat_id, queries)
        tail = f"✅ Готово, отправил {sent} шт." if sent else "✅ Новых тендеров нет."
        notifier.send_message(tail, chat_id=chat_id, reply_markup=_main_menu())
    except Exception as e:
        log.exception("Ошибка show_new для %s", chat_id)
        notifier.send_message(f"⚠️ Ошибка: {e}", chat_id=chat_id)


def _handle_message(cfg, store, msg, awaiting, default_queries) -> None:
    text = (msg.get("text") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id:
        return

    # Пользователь вводит свои ключевые слова
    if awaiting.pop(chat_id, False):
        words = [w.strip() for w in re.split(r"[,\n;]+", text) if w.strip()]
        if words:
            store.set_keywords(chat_id, words)
            notifier.send_message(
                f"✅ Буду искать по словам: <b>{', '.join(words)}</b>",
                chat_id=chat_id,
            )
            _do_show_new(cfg, store, chat_id, default_queries)
        else:
            notifier.send_message("Не понял слова, попробуй ещё раз.", chat_id=chat_id,
                                  reply_markup=_main_menu())
        return

    if text.startswith("/start"):
        store.add_user(chat_id)
        notifier.send_message(WELCOME, chat_id=chat_id, reply_markup=_main_menu())
    elif text.startswith("/new"):
        _do_show_new(cfg, store, chat_id, default_queries)
    elif text.startswith("/help"):
        notifier.send_message(WELCOME, chat_id=chat_id, reply_markup=_main_menu())
    else:
        store.add_user(chat_id)
        notifier.send_message("Выбери действие:", chat_id=chat_id, reply_markup=_main_menu())


def _handle_callback(cfg, store, cq, awaiting, default_queries) -> None:
    data = cq.get("data", "")
    chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
    notifier.answer_callback(cq.get("id", ""))
    if not chat_id:
        return

    if data == "show_new":
        _do_show_new(cfg, store, chat_id, default_queries)
    elif data == "set_keywords":
        awaiting[chat_id] = True
        notifier.send_message(
            "✏️ Напиши ключевые слова через запятую.\n"
            "Например: <i>проектирование, изыскания, обследование</i>",
            chat_id=chat_id,
        )
    elif data == "my_keywords":
        q = store.get_keywords(chat_id)
        if q:
            notifier.send_message(f"📋 Твои слова: <b>{', '.join(q)}</b>",
                                  chat_id=chat_id, reply_markup=_main_menu())
        else:
            notifier.send_message(
                f"У тебя слова по умолчанию: <b>{', '.join(default_queries)}</b>",
                chat_id=chat_id, reply_markup=_main_menu())
    elif data == "reset_keywords":
        store.set_keywords(chat_id, [])
        notifier.send_message(
            f"🔔 Вернул слова по умолчанию: <b>{', '.join(default_queries)}</b>",
            chat_id=chat_id, reply_markup=_main_menu())
    elif data == "unsubscribe":
        store.deactivate_user(chat_id)
        notifier.send_message("⏸ Отписал тебя. Вернуться — команда /start", chat_id=chat_id)


def cmd_bot(cfg: dict) -> None:
    """Многопользовательский режим: у каждого свои слова, кнопки, автопроверка."""
    interval = int(cfg.get("schedule", {}).get("interval_minutes", 30)) * 60
    default_queries = cfg.get("source", {}).get("search_queries", [])
    store = _open_store(cfg)
    awaiting: dict[str, bool] = {}
    last_run = 0.0
    offset: int | None = None

    log.info("Запуск многопользовательского бота, автопроверка каждые %d мин", interval // 60)

    while True:
        # Периодическая автопроверка по всем активным пользователям
        if time.time() - last_run >= interval:
            try:
                users = store.list_active_users()
                if users:
                    session = scraper.build_logged_session()
                    for uid in users:
                        try:
                            queries = store.get_keywords(uid) or default_queries
                            process_for_user(cfg, store, session, uid, queries)
                        except Exception:
                            log.exception("Ошибка автоцикла для %s", uid)
            except Exception:
                log.exception("Ошибка автоцикла")
            last_run = time.time()

        # Слушаем команды/кнопки
        updates = notifier.get_updates(offset, timeout=30)
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    _handle_callback(cfg, store, upd["callback_query"], awaiting, default_queries)
                elif "message" in upd:
                    _handle_message(cfg, store, upd["message"], awaiting, default_queries)
            except Exception:
                log.exception("Ошибка обработки обновления")


def main() -> None:
    parser = argparse.ArgumentParser(description="ИИ-агент мониторинга B2B-Center")
    parser.add_argument(
        "command",
        choices=["once", "run", "bot", "test-telegram", "get-chat-id"],
        help="once — один цикл; run — бесконечный цикл; "
        "bot — слушать команды /new в Telegram + автопроверка; "
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

    if args.command == "bot":
        cmd_bot(cfg)
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
