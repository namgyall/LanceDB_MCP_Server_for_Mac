import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class SearchIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.app_data_patch = patch.object(
            server, "APP_DATA_DIR", Path(self.temporary_directory.name)
        )
        self.app_data_patch.start()
        self.database = server.lancedb.connect(self.temporary_directory.name)
        self.database_patch = patch.object(server, "db", self.database)
        self.database_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        self.app_data_patch.stop()
        self.temporary_directory.cleanup()

    def test_empty_database_reports_that_indexing_waits_for_first_record(self):
        listed = server.list_search_indexes()
        ensured = server.ensure_search_indexes()

        self.assertEqual(
            listed,
            {"table": server.TABLE_NAME, "table_exists": False, "indexes": []},
        )
        self.assertFalse(ensured["table_exists"])
        self.assertEqual(ensured["created"], [])
        self.assertIn("first document", ensured["message"])
        self.assertEqual(self.database.list_tables().tables, [])

    def test_new_table_gets_search_indexes_once_and_appends_remain_searchable(self):
        first = server._store_records(
            [
                {
                    "title": "Initial title",
                    "content": "initial searchable passage",
                    "source": "test",
                }
            ]
        )
        table = self.database.open_table(server.TABLE_NAME)
        initial_indexes = {
            index.name: index.index_uuid for index in table.list_indices()
        }

        self.assertEqual(first, {"stored": 1, "duplicates": 0})
        self.assertEqual(
            {tuple(index.columns) for index in table.list_indices()},
            {("content",), ("title",)},
        )
        self.assertTrue(all(index.index_type == "FTS" for index in table.list_indices()))

        second = server._store_records(
            [
                {
                    "title": "Appended title",
                    "content": "newly appended searchable passage",
                    "source": "test",
                }
            ]
        )
        updated_table = self.database.open_table(server.TABLE_NAME)
        updated_indexes = {
            index.name: index.index_uuid for index in updated_table.list_indices()
        }

        self.assertEqual(second, {"stored": 1, "duplicates": 0})
        self.assertEqual(updated_indexes, initial_indexes)
        self.assertEqual(len(updated_table), 2)
        self.assertEqual(
            server._search_database(
                self.database, "newly appended searchable", limit=5
            )[0]["title"],
            "Appended title",
        )
        status = server.lancedb_status()
        self.assertEqual({item["type"] for item in status["indexes"]}, {"FTS"})
        self.assertEqual(status["documents"], 2)

    def test_legacy_table_gets_only_applicable_text_index_without_changing_rows(self):
        table = self.database.create_table(
            server.TABLE_NAME,
            data=[
                {
                    "content": "legacy content remains",
                    "source": "old source",
                    "created_at": "2026-01-01",
                }
            ],
        )

        result = server.ensure_search_indexes()

        self.assertEqual(result["created"], ["content"])
        self.assertEqual(len(self.database.open_table(server.TABLE_NAME)), 1)
        indexes = self.database.open_table(server.TABLE_NAME).list_indices()
        self.assertEqual(len(indexes), 1)
        self.assertEqual(indexes[0].index_type, "FTS")
        self.assertEqual(indexes[0].columns, ["content"])
        self.assertNotIn("pc_cc", table.schema.names)
        self.assertEqual(
            {column for index in indexes for column in index.columns}
            & {"source", "created_at"},
            set(),
        )

    def test_non_text_schema_columns_are_not_fts_indexed(self):
        self.database.create_table(
            server.TABLE_NAME,
            data=[{"content": 7, "title": 12, "source": "test"}],
        )

        result = server.ensure_search_indexes()

        self.assertEqual(result["created"], [])
        self.assertEqual(result["indexes"], [])

    def test_ensure_is_idempotent_and_listing_reports_existing_index_metadata(self):
        table = self.database.create_table(
            server.TABLE_NAME,
            data=[
                {
                    "content": "existing searchable text",
                    "source": "test",
                    "created_at": "2026-01-01",
                    "title": "Existing title",
                }
            ],
        )
        table.create_index("content", config=server.FTS(), replace=False)

        first = server.ensure_search_indexes()
        uuids = {
            index.name: index.index_uuid
            for index in self.database.open_table(server.TABLE_NAME).list_indices()
        }
        second = server.ensure_search_indexes()
        listed = server.list_search_indexes()

        self.assertEqual(first["created"], ["title"])
        self.assertEqual(second["created"], [])
        self.assertTrue(listed["table_exists"])
        self.assertEqual(
            {
                (index["type"], tuple(index["columns"]))
                for index in listed["indexes"]
            },
            {("FTS", ("content",)), ("FTS", ("title",))},
        )
        self.assertEqual(
            uuids,
            {
                index.name: index.index_uuid
                for index in self.database.open_table(server.TABLE_NAME).list_indices()
            },
        )

    def test_index_creation_failure_is_logged_and_propagated(self):
        self.database.create_table(
            server.TABLE_NAME,
            data=[
                {
                    "content": "private text must not be logged",
                    "source": "test",
                    "created_at": "2026-01-01",
                }
            ],
        )
        table_type = type(self.database.open_table(server.TABLE_NAME))
        with patch.object(
            table_type,
            "create_index",
            autospec=True,
            side_effect=RuntimeError("index backend unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "index backend unavailable"):
                server.ensure_search_indexes()

        progress_log = (
            Path(self.temporary_directory.name) / "logs" / "app.log"
        ).read_text(encoding="utf-8")
        self.assertIn("Search index creation failed for content", progress_log)
        self.assertNotIn("private text must not be logged", progress_log)

    def test_startup_optimization_updates_indexes_without_changing_records(self):
        table = self.database.create_table(
            server.TABLE_NAME,
            data=[
                {
                    "content": "initial searchable content",
                    "source": "test",
                    "created_at": "2026-01-01",
                }
            ],
        )
        table.create_index("content", config=server.FTS())
        table.add(
            [
                {
                    "content": "newly appended searchable content",
                    "source": "test",
                    "created_at": "2026-01-02",
                }
            ]
        )

        result = server.optimize_database_on_startup()

        self.assertTrue(result["optimized"])
        self.assertEqual(len(self.database.open_table(server.TABLE_NAME)), 2)
        self.assertEqual(result["indexes"][0]["unindexed_rows"], 0)
        self.assertEqual(
            server._search_database(
                self.database, "newly appended searchable", limit=5
            )[0]["content"],
            "newly appended searchable content",
        )
        progress_log = (
            Path(self.temporary_directory.name) / "logs" / "app.log"
        ).read_text(encoding="utf-8")
        self.assertIn("Database optimization completed", progress_log)

    def test_startup_optimization_skips_missing_or_empty_tables(self):
        missing = server.optimize_database_on_startup()
        self.assertEqual(missing["reason"], "table_missing")

        self.database.create_table(
            server.TABLE_NAME,
            schema=server.pa.schema([server.pa.field("content", server.pa.string())]),
        )
        empty = server.optimize_database_on_startup()
        self.assertEqual(empty["reason"], "table_empty")

    def test_startup_optimization_failure_is_logged_without_blocking_startup(self):
        table = self.database.create_table(
            server.TABLE_NAME,
            data=[{"content": "existing content"}],
        )
        with patch.object(
            type(table),
            "optimize",
            autospec=True,
            side_effect=RuntimeError("index backend unavailable"),
        ):
            result = server.optimize_database_on_startup()

        self.assertFalse(result["optimized"])
        self.assertEqual(result["reason"], "optimization_failed")
        progress_log = (
            Path(self.temporary_directory.name) / "logs" / "app.log"
        ).read_text(encoding="utf-8")
        self.assertIn("Database optimization failed (RuntimeError)", progress_log)


if __name__ == "__main__":
    unittest.main()
