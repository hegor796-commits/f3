"""Хранение пользователей и обработанных объявлений (по каждому пользователю).

Многопользовательский режим: у каждого пользователя (Telegram chat_id) свой
набор поисковых слов и своя история отправленных тендеров (чтобы одному и тому
же человеку одно объявление не приходило дважды).
"""

import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id     TEXT PRIMARY KEY,
                keywords    TEXT,               -- JSON-список слов; пусто = слова по умолчанию
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_seen (
                chat_id     TEXT NOT NULL,
                listing_id  TEXT NOT NULL,
                title       TEXT,
                seen_at     INTEGER NOT NULL,
                PRIMARY KEY (chat_id, listing_id)
            )
            """
        )
        self.conn.commit()

    # ----- Пользователи -----

    def add_user(self, chat_id: str) -> None:
        self.conn.execute(
            """
            INSERT INTO users (chat_id, keywords, active, created_at)
            VALUES (?, NULL, 1, ?)
            ON CONFLICT(chat_id) DO UPDATE SET active = 1
            """,
            (str(chat_id), int(time.time())),
        )
        self.conn.commit()

    def deactivate_user(self, chat_id: str) -> None:
        self.conn.execute(
            "UPDATE users SET active = 0 WHERE chat_id = ?", (str(chat_id),)
        )
        self.conn.commit()

    def set_keywords(self, chat_id: str, keywords: list[str]) -> None:
        self.add_user(chat_id)
        value = json.dumps(keywords, ensure_ascii=False) if keywords else None
        self.conn.execute(
            "UPDATE users SET keywords = ? WHERE chat_id = ?", (value, str(chat_id))
        )
        self.conn.commit()

    def get_keywords(self, chat_id: str) -> list[str] | None:
        row = self.conn.execute(
            "SELECT keywords FROM users WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def list_active_users(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT chat_id FROM users WHERE active = 1"
        ).fetchall()
        return [r[0] for r in rows]

    # ----- История отправленного (по пользователю) -----

    def is_seen(self, chat_id: str, listing_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM user_seen WHERE chat_id = ? AND listing_id = ?",
            (str(chat_id), str(listing_id)),
        ).fetchone()
        return row is not None

    def mark_seen(self, chat_id: str, listing_id: str, title: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO user_seen (chat_id, listing_id, title, seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, listing_id) DO NOTHING
            """,
            (str(chat_id), str(listing_id), title, int(time.time())),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
