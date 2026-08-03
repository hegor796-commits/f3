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
        # Шаблоны поиска (общие для всех): name, слова, исключения, гео
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
                name        TEXT PRIMARY KEY,
                title       TEXT,
                keywords    TEXT,   -- JSON-список
                exclusions  TEXT,   -- JSON-список
                geo         TEXT    -- 'all' или 'moscow'
            )
            """
        )
        self.conn.commit()

    # ----- Шаблоны -----

    def ensure_template(self, name: str, title: str, keywords: list[str],
                        exclusions: list[str], geo: str) -> None:
        """Создаёт шаблон с дефолтными значениями, если его ещё нет."""
        row = self.conn.execute(
            "SELECT 1 FROM templates WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return
        self.conn.execute(
            "INSERT INTO templates (name, title, keywords, exclusions, geo) VALUES (?,?,?,?,?)",
            (name, title, json.dumps(keywords, ensure_ascii=False),
             json.dumps(exclusions, ensure_ascii=False), geo),
        )
        self.conn.commit()

    def get_template(self, name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT name, title, keywords, exclusions, geo FROM templates WHERE name = ?",
            (name,),
        ).fetchone()
        if not row:
            return None
        return {
            "name": row[0],
            "title": row[1],
            "keywords": json.loads(row[2]) if row[2] else [],
            "exclusions": json.loads(row[3]) if row[3] else [],
            "geo": row[4],
        }

    def all_templates(self) -> list[dict]:
        names = [r[0] for r in self.conn.execute("SELECT name FROM templates").fetchall()]
        return [self.get_template(n) for n in names]

    def _set_list(self, name: str, column: str, values: list[str]) -> None:
        self.conn.execute(
            f"UPDATE templates SET {column} = ? WHERE name = ?",
            (json.dumps(values, ensure_ascii=False), name),
        )
        self.conn.commit()

    def add_keyword(self, name: str, word: str) -> None:
        t = self.get_template(name)
        if t and word and word not in t["keywords"]:
            self._set_list(name, "keywords", t["keywords"] + [word])

    def remove_keyword(self, name: str, index: int) -> str | None:
        t = self.get_template(name)
        if t and 0 <= index < len(t["keywords"]):
            word = t["keywords"].pop(index)
            self._set_list(name, "keywords", t["keywords"])
            return word
        return None

    def add_exclusion(self, name: str, word: str) -> None:
        t = self.get_template(name)
        if t and word and word not in t["exclusions"]:
            self._set_list(name, "exclusions", t["exclusions"] + [word])

    def remove_exclusion(self, name: str, index: int) -> str | None:
        t = self.get_template(name)
        if t and 0 <= index < len(t["exclusions"]):
            word = t["exclusions"].pop(index)
            self._set_list(name, "exclusions", t["exclusions"])
            return word
        return None

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
