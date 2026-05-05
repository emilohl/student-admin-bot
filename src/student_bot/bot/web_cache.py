from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from student_bot.config import Config


@dataclass
class CachedPage:
    url: str
    title: str
    content: str
    fetched_at: int


class WebCache:
    def __init__(self, cfg: Config):
        self._path = cfg.absolute(Path(cfg.dynamic_web.cache_db))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS web_cache (
                  url TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  content TEXT NOT NULL,
                  fetched_at INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def get(self, url: str) -> CachedPage | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT url, title, content, fetched_at FROM web_cache WHERE url = ?",
                (url,),
            ).fetchone()
        if row is None:
            return None
        return CachedPage(
            url=row["url"],
            title=row["title"],
            content=row["content"],
            fetched_at=int(row["fetched_at"]),
        )

    def put(self, page: CachedPage) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO web_cache(url, title, content, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                  title=excluded.title,
                  content=excluded.content,
                  fetched_at=excluded.fetched_at
                """,
                (page.url, page.title, page.content, page.fetched_at),
            )
            conn.commit()

    @staticmethod
    def age_days(fetched_at: int) -> int:
        secs = max(0, int(time.time()) - fetched_at)
        return secs // 86400

