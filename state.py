from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATABASE_PATH = 'bot_state.sqlite3'


@dataclass(frozen=True)
class SavedSearch:
    sku: str
    name: str


@dataclass(frozen=True)
class SeenLink:
    url: str
    marketplace_key: str | None = None
    query: str | None = None


def get_database_path() -> str:
    return os.getenv('DATABASE_PATH', DEFAULT_DATABASE_PATH).strip() or DEFAULT_DATABASE_PATH


class BotStateStore:
    def __init__(self, database_path: str | os.PathLike[str] | None = None) -> None:
        self.database_path = str(database_path or get_database_path())

    def initialize(self) -> None:
        self._ensure_parent_directory()

        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS saved_searches (
                    sku TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS seen_links (
                    url TEXT PRIMARY KEY,
                    marketplace_key TEXT,
                    query TEXT,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS chat_state (
                    chat_id TEXT PRIMARY KEY,
                    pinned_saved_searches_message_id INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def upsert_saved_search(self, sku: str, name: str) -> SavedSearch:
        sku = sku.strip()
        name = name.strip()

        if not sku:
            raise ValueError('SKU is required')

        if not name:
            raise ValueError('Name is required')

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO saved_searches (sku, name)
                VALUES (?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    name = excluded.name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (sku, name),
            )

        return SavedSearch(sku=sku, name=name)

    def list_saved_searches(self) -> list[SavedSearch]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sku, name
                FROM saved_searches
                ORDER BY sku COLLATE NOCASE
                """
            ).fetchall()

        return [SavedSearch(sku=row['sku'], name=row['name']) for row in rows]

    def delete_saved_search(self, sku: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                'DELETE FROM saved_searches WHERE sku = ?',
                (sku.strip(),),
            )

        return cursor.rowcount > 0

    def has_seen_link(self, url: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT 1 FROM seen_links WHERE url = ?',
                (url,),
            ).fetchone()

        return row is not None

    def record_seen_links(self, links: list[SeenLink]) -> None:
        if not links:
            return

        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO seen_links (url, marketplace_key, query)
                VALUES (?, ?, ?)
                """,
                [(link.url, link.marketplace_key, link.query) for link in links],
            )

    def get_pinned_saved_searches_message_id(self, chat_id: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pinned_saved_searches_message_id
                FROM chat_state
                WHERE chat_id = ?
                """,
                (str(chat_id),),
            ).fetchone()

        if row is None:
            return None

        return row['pinned_saved_searches_message_id']

    def set_pinned_saved_searches_message_id(self, chat_id: str, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_state (chat_id, pinned_saved_searches_message_id)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    pinned_saved_searches_message_id = excluded.pinned_saved_searches_message_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (str(chat_id), message_id),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_parent_directory(self) -> None:
        parent = Path(self.database_path).expanduser().parent

        if str(parent) not in ('', '.'):
            parent.mkdir(parents=True, exist_ok=True)
