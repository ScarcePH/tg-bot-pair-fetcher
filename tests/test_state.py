from __future__ import annotations

import os
import unittest

try:
    from state import BotStateStore, SavedSearch, SeenLink
except ModuleNotFoundError as exc:
    BotStateStore = None
    SavedSearch = None
    SeenLink = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


TEST_DATABASE_URL = os.getenv('TEST_DATABASE_URL', '').strip()


@unittest.skipIf(IMPORT_ERROR is not None, f'missing optional dependency: {IMPORT_ERROR}')
@unittest.skipUnless(TEST_DATABASE_URL, 'TEST_DATABASE_URL is not set; skipping Postgres integration tests')
class BotStateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BotStateStore(TEST_DATABASE_URL)
        self.store.initialize()
        with self.store._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute('TRUNCATE chat_state, seen_links, saved_searches')

    def tearDown(self) -> None:
        with self.store._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute('TRUNCATE chat_state, seen_links, saved_searches')

    def test_upsert_list_and_delete_saved_search(self) -> None:
        self.store.upsert_saved_search('b-sku', 'Second')
        self.store.upsert_saved_search('A-sku', 'First')
        self.store.upsert_saved_search('A-sku', 'First updated')
        self.assertEqual(
            self.store.list_saved_searches(),
            [SavedSearch('A-sku', 'First updated'), SavedSearch('b-sku', 'Second')],
        )
        self.assertTrue(self.store.delete_saved_search('A-sku'))
        self.assertFalse(self.store.delete_saved_search('A-sku'))

    def test_seen_links_are_recorded_once(self) -> None:
        link = SeenLink('https://market.example/item/1', 'marketplace_a', 'sku')
        self.assertFalse(self.store.has_seen_link(link.url))
        self.store.record_seen_links([link, link])
        self.assertTrue(self.store.has_seen_link(link.url))
        with self.store._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) FROM seen_links')
                self.assertEqual(cursor.fetchone()[0], 1)

    def test_chat_state_stores_pinned_message_id(self) -> None:
        self.assertIsNone(self.store.get_pinned_saved_searches_message_id('123'))
        self.store.set_pinned_saved_searches_message_id('123', 456)
        self.assertEqual(self.store.get_pinned_saved_searches_message_id('123'), 456)

    def test_fetch_lock_excludes_another_connection(self) -> None:
        other_store = BotStateStore(TEST_DATABASE_URL)
        with self.store.fetch_lock() as first:
            with other_store.fetch_lock() as second:
                self.assertTrue(first)
                self.assertFalse(second)


if __name__ == '__main__':
    unittest.main()
