"""SQLite: widziane oferty, stan fraz, cache sprzedających i wyszukiwania dodane z Discorda."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import SearchConfig
from .scraper import Seller

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_id     INTEGER PRIMARY KEY,
    search_name TEXT NOT NULL,
    price       REAL,
    seen_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_items(seen_at);

CREATE TABLE IF NOT EXISTS search_state (
    search_name    TEXT PRIMARY KEY,
    initialized_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
    name       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,          -- JSON z polami SearchConfig (bez name/channel_id/source)
    channel_id INTEGER,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sellers (
    user_id    INTEGER PRIMARY KEY,
    login      TEXT,
    rating     REAL NOT NULL,
    reviews    INTEGER NOT NULL,
    fetched_at REAL NOT NULL
);
"""


@dataclass
class StoredSearch:
    search: SearchConfig
    enabled: bool
    created_by: str | None
    created_at: float


_NOT_IN_JSON = {"name", "channel_id", "source"}


class Storage:
    """Jedna instancja na wątek (monitor i bot Discord mają osobne połączenia)."""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: obiekt bywa tworzony w innym wątku niż ten, który go używa.
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------- oferty
    def filter_unseen(self, item_ids: Iterable[int]) -> set[int]:
        ids = list(item_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(f"SELECT item_id FROM seen_items WHERE item_id IN ({placeholders})", ids)
        return set(ids) - {r[0] for r in rows}

    def mark_seen(self, search_name: str, items: Iterable[tuple[int, float]]) -> None:
        now = time.time()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_items(item_id, search_name, price, seen_at) VALUES (?, ?, ?, ?)",
            [(item_id, search_name, price, now) for item_id, price in items],
        )
        self.conn.commit()

    # ------------------------------------------------------ stan fraz
    def is_initialized(self, search_name: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM search_state WHERE search_name = ?", (search_name,)).fetchone()
        return row is not None

    def set_initialized(self, search_name: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO search_state(search_name, initialized_at) VALUES (?, ?)",
            (search_name, time.time()),
        )
        self.conn.commit()

    def reset_initialized(self, search_name: str) -> None:
        """Następny cykl frazy znów tylko zapisze obecne oferty, bez powiadomień."""
        self.conn.execute("DELETE FROM search_state WHERE search_name = ?", (search_name,))
        self.conn.commit()

    # ---------------------------------------------- wyszukiwania z Discorda
    def add_search(self, search: SearchConfig, created_by: str | None = None) -> None:
        data = {k: v for k, v in dataclasses.asdict(search).items() if k not in _NOT_IN_JSON}
        self.conn.execute(
            "INSERT INTO searches(name, data, channel_id, enabled, created_by, created_at) VALUES (?, ?, ?, 1, ?, ?)",
            (search.name, json.dumps(data, ensure_ascii=False), search.channel_id, created_by, time.time()),
        )
        self.reset_initialized(search.name)  # commit w środku

    def list_searches(self) -> list[StoredSearch]:
        rows = self.conn.execute(
            "SELECT name, data, channel_id, enabled, created_by, created_at FROM searches ORDER BY created_at"
        ).fetchall()
        known = {f.name for f in dataclasses.fields(SearchConfig)} - _NOT_IN_JSON
        result = []
        for name, data, channel_id, enabled, created_by, created_at in rows:
            fields = {k: v for k, v in json.loads(data).items() if k in known}
            search = SearchConfig(name=name, channel_id=channel_id, source="discord", **fields)
            result.append(StoredSearch(search, bool(enabled), created_by, created_at))
        return result

    def delete_search(self, name: str) -> bool:
        cur = self.conn.execute("DELETE FROM searches WHERE name = ?", (name,))
        self.reset_initialized(name)
        return cur.rowcount > 0

    def set_search_enabled(self, name: str, enabled: bool) -> bool:
        cur = self.conn.execute("UPDATE searches SET enabled = ? WHERE name = ?", (int(enabled), name))
        self.conn.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------- sprzedający
    def get_seller(self, user_id: int, max_age_s: float) -> Seller | None:
        row = self.conn.execute(
            "SELECT login, rating, reviews FROM sellers WHERE user_id = ? AND fetched_at >= ?",
            (user_id, time.time() - max_age_s),
        ).fetchone()
        if row is None:
            return None
        return Seller(id=user_id, login=row[0] or "", rating=row[1], reviews=row[2])

    def save_seller(self, seller: Seller) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sellers(user_id, login, rating, reviews, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (seller.id, seller.login, seller.rating, seller.reviews, time.time()),
        )
        self.conn.commit()

    # --------------------------------------------------------- sprzątanie
    def cleanup(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        cur = self.conn.execute("DELETE FROM seen_items WHERE seen_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM sellers WHERE fetched_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount
