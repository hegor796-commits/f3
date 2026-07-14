"""Отправка уведомлений в Telegram через Bot API."""

import html
import logging
import os
import time

import requests

from .models import Listing

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    pass


def _credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise TelegramError(
            "Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID (см. .env.example)"
        )
    return token, chat_id


def format_message(lst: Listing) -> str:
    title = html.escape(lst.title)
    lines = [f"🔔 <b>{title}</b>"]
    if lst.company:
        lines.append(f"🏢 {html.escape(lst.company)}")
    if lst.end_date:
        lines.append(f"⏳ Срок: {html.escape(lst.end_date)}")
    if lst.ai_score is not None:
        lines.append(f"⭐ Релевантность: {lst.ai_score}/10")
    if lst.ai_summary:
        lines.append(f"💬 {html.escape(lst.ai_summary)}")
    if lst.matched_keywords:
        lines.append(f"🔑 {html.escape(', '.join(lst.matched_keywords))}")
    lines.append(f'\n<a href="{lst.url}">Открыть на B2B-Center</a>')
    return "\n".join(lines)


def send_message(text: str, parse_mode: str = "HTML", retries: int = 3) -> None:
    token, chat_id = _credentials()
    url = API_URL.format(token=token, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=30)
            data = resp.json()
            if data.get("ok"):
                return
            # Telegram может попросить подождать (rate limit)
            if resp.status_code == 429:
                wait = data.get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram rate limit, ждём %d с", wait)
                time.sleep(wait)
                continue
            raise TelegramError(f"Telegram API: {data.get('description', resp.text)}")
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise TelegramError(f"Сетевая ошибка Telegram: {e}") from e
            time.sleep(2 ** (attempt + 1))


def notify(listings: list[Listing], parse_mode: str = "HTML") -> int:
    """Отправляет уведомление по каждому объявлению. Возвращает число отправленных."""
    sent = 0
    for lst in listings:
        send_message(format_message(lst), parse_mode=parse_mode)
        sent += 1
        time.sleep(1)  # мягкий лимит Telegram ~1 сообщение/сек в один чат
    return sent
