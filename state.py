from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg


FETCH_ADVISORY_LOCK_ID = 7_241_835_190_431


@dataclass(frozen=True)
class SavedSearch:
    sku: str
    name: str


@dataclass(frozen=True)
class SeenLink:
    url: str
    marketplace_key: str | None = None
    query: str | None = None


def get_database_url() -> str:
    database_url = os.getenv('DATABASE_URL', '').strip()
    if not database_url:
        raise ValueError('DATABASE_URL is required')
    return database_url


class BotStateStore:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()

    def initialize(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS saved_searches (
                        sku TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS seen_links (
                        url TEXT PRIMARY KEY,
                        marketplace_key TEXT,
                        query TEXT,
                        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS chat_state (
                        chat_id TEXT PRIMARY KEY,
                        pinned_saved_searches_message_id BIGINT,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO saved_searches (sku, name)
                    VALUES (%s, %s)
                    ON CONFLICT(sku) DO UPDATE SET
                        name = EXCLUDED.name,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (sku, name),
                )
        return SavedSearch(sku=sku, name=name)

    def list_saved_searches(self) -> list[SavedSearch]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT sku, name FROM saved_searches ORDER BY LOWER(sku), sku')
                rows = cursor.fetchall()
        return [SavedSearch(sku=row[0], name=row[1]) for row in rows]

    def delete_saved_search(self, sku: str) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute('DELETE FROM saved_searches WHERE sku = %s', (sku.strip(),))
                deleted = cursor.rowcount
        return deleted > 0

    def has_seen_link(self, url: str) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1 FROM seen_links WHERE url = %s', (url,))
                return cursor.fetchone() is not None

    def record_seen_links(self, links: list[SeenLink]) -> None:
        if not links:
            return
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO seen_links (url, marketplace_key, query)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(url) DO NOTHING
                    """,
                    [(link.url, link.marketplace_key, link.query) for link in links],
                )

    def get_pinned_saved_searches_message_id(self, chat_id: str) -> int | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT pinned_saved_searches_message_id FROM chat_state WHERE chat_id = %s',
                    (str(chat_id),),
                )
                row = cursor.fetchone()
        return None if row is None else row[0]

    def set_pinned_saved_searches_message_id(self, chat_id: str, message_id: int) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chat_state (chat_id, pinned_saved_searches_message_id)
                    VALUES (%s, %s)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        pinned_saved_searches_message_id = EXCLUDED.pinned_saved_searches_message_id,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (str(chat_id), message_id),
                )

    @contextmanager
    def fetch_lock(self) -> Iterator[bool]:
        """Hold the process-independent fetch lock for the duration of the context."""
        with self._connect(autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', (FETCH_ADVISORY_LOCK_ID,))
                acquired = bool(cursor.fetchone()[0])
                try:
                    yield acquired
                finally:
                    if acquired:
                        cursor.execute('SELECT pg_advisory_unlock(%s)', (FETCH_ADVISORY_LOCK_ID,))

    def _connect(self, *, autocommit: bool = False) -> psycopg.Connection:
        return psycopg.connect(self.database_url, autocommit=autocommit)
