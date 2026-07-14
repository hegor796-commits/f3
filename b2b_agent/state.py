"""Хранение уже обработанных объявлений (защита от повторных уведомлений)."""

import sqlite3
import time
from pathlib import Path


class SeenStore:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_listings (
                listing_id TEXT PRIMARY KEY,
                title      TEXT,
                notified   INTEGER NOT NULL DEFAULT 0,
                ai_score   INTEGER,
                seen_at    INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()

    def is_seen(self, listing_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row is not None

    def mark_seen(
        self,
        listing_id: str,
        title: str = "",
        notified: bool = False,
        ai_score: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO seen_listings (listing_id, title, notified, ai_score, seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
                notified = excluded.notified,
                ai_score = excluded.ai_score
            """,
            (listing_id, title, int(notified), ai_score, int(time.time())),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
