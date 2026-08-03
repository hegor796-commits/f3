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


# Стартовые шаблоны поиска (создаются один раз в пустой базе)
TEMPLATE_DEFS = [
    {
        "name": "proekt",
        "title": "📐 Проектирование",
        "geo": "all",
        "keywords": [
            "разработка проектной документации",
            "ПСД",
            "разработка рабочей документации",
            "проект",
            "функции генерального проектировщика",
        ],
        "exclusions": [],
    },
    {
        "name": "stroy",
        "title": "🏗 Строительство (Москва)",
        "geo": "moscow",
        "keywords": [
            "СМР",
            "строительно-монтажные работы",
            "строительство",
            "монтажные работы",
        ],
        "exclusions": [],
    },
]


def _init_templates(store: Store) -> None:
    for t in TEMPLATE_DEFS:
        store.ensure_template(t["name"], t["title"], t["keywords"], t["exclusions"], t["geo"])


def process_for_user(cfg: dict, store: Store, session, chat_id: str) -> int:
    """Ищет тендеры по ВСЕМ шаблонам, шлёт пользователю новые. Возвращает число отправленных."""
    src = cfg.get("source", {})
    ai_cfg = cfg.get("ai", {})
    parse_mode = cfg.get("telegram", {}).get("parse_mode", "HTML")
    pages = int(src.get("pages", 1))
    delay = float(src.get("request_delay", 3))

    # Собираем результаты из всех шаблонов (гео и исключения применяются внутри)
    collected: dict[str, object] = {}
    for tpl in store.all_templates():
        for lst in scraper.search_template(session, tpl, pages, delay):
            collected.setdefault(lst.listing_id, lst)
    listings = list(collected.values())

    fresh = [lst for lst in listings if not store.is_seen(chat_id, lst.listing_id)]

    # ИИ-оценка (если доступна; сейчас OpenAI блокирует РФ, поэтому обычно пропускается)
    to_notify = fresh
    if fresh and ai_cfg.get("enabled", True) and ai_filter.is_available():
        min_score = int(ai_cfg.get("min_score", 6))
        ai_filter.check_relevance(
            fresh,
            interest_profile=ai_cfg.get("interest_profile", ""),
            model=ai_cfg.get("model", "gpt-4o"),
            max_checks=int(ai_cfg.get("max_checks_per_cycle", 20)),
        )
        to_notify = [l for l in fresh if l.ai_score is None or l.ai_score >= min_score]
        for l in fresh:
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
    """Один цикл для режимов once/run — по chat_id из .env и всем шаблонам."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    store = _open_store(cfg)
    try:
        _init_templates(store)
        session = scraper.build_logged_session()
        return process_for_user(cfg, store, session, chat_id)
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


WELCOME = (
    "🤖 <b>Бот мониторинга тендеров B2B-Center</b>\n\n"
    "Я каждые 30 минут ищу новые тендеры по двум шаблонам и присылаю тебе "
    "(без повторов):\n"
    "• 📐 <b>Проектирование</b> — по всей стране\n"
    "• 🏗 <b>Строительство</b> — только по Москве\n\n"
    "Нажми «Показать новые», чтобы проверить прямо сейчас, или зайди в шаблон "
    "чтобы добавить/убрать ключевые слова и исключения.\n\n"
    "Выбери действие:"
)


def _main_menu() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🔍 Показать новые тендеры", "callback_data": "new"}],
            [{"text": "📐 Шаблон проектирования", "callback_data": "tpl:proekt"}],
            [{"text": "🏗 Шаблон строительства", "callback_data": "tpl:stroy"}],
        ]
    }


def _template_menu(name: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "➕ Добавить ключевое слово", "callback_data": f"addkw:{name}"}],
            [{"text": "🚫 Добавить исключение", "callback_data": f"addex:{name}"}],
            [{"text": "➖ Убрать ключевое слово", "callback_data": f"rmkw:{name}"}],
            [{"text": "➖ Убрать исключение", "callback_data": f"rmex:{name}"}],
            [{"text": "⬅️ В главное меню", "callback_data": "menu"}],
        ]
    }


def _removal_menu(name: str, items: list[str], kind: str) -> dict:
    rows = [
        [{"text": f"❌ {w[:45]}", "callback_data": f"del{kind}:{name}:{i}"}]
        for i, w in enumerate(items)
    ]
    rows.append([{"text": "⬅️ Назад", "callback_data": f"tpl:{name}"}])
    return {"inline_keyboard": rows}


def _template_text(tpl: dict) -> str:
    kw = "\n".join(f"• {w}" for w in tpl["keywords"]) or "—"
    ex = "\n".join(f"• {w}" for w in tpl["exclusions"]) or "—"
    geo = "только Москва" if tpl["geo"] == "moscow" else "вся страна"
    return (
        f"<b>{tpl['title']}</b>\n"
        f"🌍 Гео: {geo}\n\n"
        f"🔑 <b>Ключевые слова:</b>\n{kw}\n\n"
        f"🚫 <b>Исключения:</b>\n{ex}"
    )


def _show_template(store: Store, chat_id: str, name: str) -> None:
    tpl = store.get_template(name)
    if not tpl:
        notifier.send_message("Шаблон не найден.", chat_id=chat_id, reply_markup=_main_menu())
        return
    notifier.send_message(_template_text(tpl), chat_id=chat_id, reply_markup=_template_menu(name))


def _do_show_new(cfg: dict, store: Store, chat_id: str) -> None:
    store.add_user(chat_id)
    try:
        notifier.send_message("🔍 Проверяю сайт, подожди…", chat_id=chat_id)
        session = scraper.build_logged_session()
        sent = process_for_user(cfg, store, session, chat_id)
        tail = f"✅ Готово, отправил {sent} шт." if sent else "✅ Новых тендеров нет."
        notifier.send_message(tail, chat_id=chat_id, reply_markup=_main_menu())
    except Exception as e:
        log.exception("Ошибка show_new для %s", chat_id)
        notifier.send_message(f"⚠️ Ошибка: {e}", chat_id=chat_id, reply_markup=_main_menu())


def _handle_message(cfg, store, msg, awaiting) -> None:
    text = (msg.get("text") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id:
        return

    # Пользователь вводит слово для добавления в шаблон
    pending = awaiting.pop(chat_id, None)
    if pending:
        action, name = pending
        words = [w.strip() for w in re.split(r"[,\n;]+", text) if w.strip()]
        if not words:
            notifier.send_message("Не понял слова, попробуй ещё раз.", chat_id=chat_id,
                                  reply_markup=_template_menu(name))
            return
        for w in words:
            if action == "addkw":
                store.add_keyword(name, w)
            else:
                store.add_exclusion(name, w)
        what = "ключевые слова" if action == "addkw" else "исключения"
        notifier.send_message(f"✅ Добавил в {what}: <b>{', '.join(words)}</b>", chat_id=chat_id)
        _show_template(store, chat_id, name)
        return

    if text.startswith("/start"):
        store.add_user(chat_id)
        notifier.send_message(WELCOME, chat_id=chat_id, reply_markup=_main_menu())
    elif text.startswith("/new"):
        _do_show_new(cfg, store, chat_id)
    elif text.startswith("/help"):
        notifier.send_message(WELCOME, chat_id=chat_id, reply_markup=_main_menu())
    else:
        store.add_user(chat_id)
        notifier.send_message("Выбери действие:", chat_id=chat_id, reply_markup=_main_menu())


def _handle_callback(cfg, store, cq, awaiting) -> None:
    data = cq.get("data", "")
    chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
    notifier.answer_callback(cq.get("id", ""))
    if not chat_id:
        return

    store.add_user(chat_id)

    if data == "menu":
        notifier.send_message("Главное меню:", chat_id=chat_id, reply_markup=_main_menu())
    elif data == "new":
        _do_show_new(cfg, store, chat_id)
    elif data.startswith("tpl:"):
        _show_template(store, chat_id, data.split(":", 1)[1])
    elif data.startswith("addkw:"):
        name = data.split(":", 1)[1]
        awaiting[chat_id] = ("addkw", name)
        notifier.send_message(
            "➕ Напиши ключевое слово (или несколько через запятую), "
            "которое добавить в поиск:", chat_id=chat_id)
    elif data.startswith("addex:"):
        name = data.split(":", 1)[1]
        awaiting[chat_id] = ("addex", name)
        notifier.send_message(
            "🚫 Напиши слово-исключение (или несколько через запятую). "
            "Тендеры с этим словом присылаться не будут:", chat_id=chat_id)
    elif data.startswith("rmkw:"):
        name = data.split(":", 1)[1]
        tpl = store.get_template(name)
        if tpl and tpl["keywords"]:
            notifier.send_message("Выбери слово для удаления:", chat_id=chat_id,
                                  reply_markup=_removal_menu(name, tpl["keywords"], "kw"))
        else:
            notifier.send_message("Список ключевых слов пуст.", chat_id=chat_id,
                                  reply_markup=_template_menu(name))
    elif data.startswith("rmex:"):
        name = data.split(":", 1)[1]
        tpl = store.get_template(name)
        if tpl and tpl["exclusions"]:
            notifier.send_message("Выбери исключение для удаления:", chat_id=chat_id,
                                  reply_markup=_removal_menu(name, tpl["exclusions"], "ex"))
        else:
            notifier.send_message("Список исключений пуст.", chat_id=chat_id,
                                  reply_markup=_template_menu(name))
    elif data.startswith("delkw:"):
        _, name, idx = data.split(":", 2)
        removed = store.remove_keyword(name, int(idx))
        if removed:
            notifier.send_message(f"➖ Убрал ключевое слово: <b>{removed}</b>", chat_id=chat_id)
        _show_template(store, chat_id, name)
    elif data.startswith("delex:"):
        _, name, idx = data.split(":", 2)
        removed = store.remove_exclusion(name, int(idx))
        if removed:
            notifier.send_message(f"➖ Убрал исключение: <b>{removed}</b>", chat_id=chat_id)
        _show_template(store, chat_id, name)


def cmd_bot(cfg: dict) -> None:
    """Многопользовательский режим с шаблонами (проектирование / строительство)."""
    interval = int(cfg.get("schedule", {}).get("interval_minutes", 30)) * 60
    store = _open_store(cfg)
    _init_templates(store)
    awaiting: dict[str, tuple] = {}
    # Сразу отвечаем на команды; первая автопроверка — через interval (не блокируем старт)
    last_run = time.time()
    offset: int | None = None

    log.info("Запуск бота с шаблонами, автопроверка каждые %d мин", interval // 60)

    while True:
        if time.time() - last_run >= interval:
            try:
                users = store.list_active_users()
                if users:
                    session = scraper.build_logged_session()
                    for uid in users:
                        try:
                            process_for_user(cfg, store, session, uid)
                        except Exception:
                            log.exception("Ошибка автоцикла для %s", uid)
            except Exception:
                log.exception("Ошибка автоцикла")
            last_run = time.time()

        updates = notifier.get_updates(offset, timeout=30)
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    _handle_callback(cfg, store, upd["callback_query"], awaiting)
                elif "message" in upd:
                    _handle_message(cfg, store, upd["message"], awaiting)
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
