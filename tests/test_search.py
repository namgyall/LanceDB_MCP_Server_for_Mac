import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def make_database(rows, table_exists=True, columns=None):
    table = Mock()
    table.schema.names = columns or ["content", "source", "created_at"]
    table.search.return_value.select.return_value.limit.return_value.to_list.return_value = rows
    database = Mock()
    database.list_tables.return_value.tables = (
        [server.TABLE_NAME] if table_exists else []
    )
    database.open_table.return_value = table
    return database


class DatabaseFirstSearchTests(unittest.TestCase):
    def test_local_matches_returned_without_external_access(self):
        rows = [
            {"content": "local match", "source": "local", "created_at": "today"}
        ]
        with (
            patch.object(server, "db", make_database(rows)),
            patch.object(
                server.lancedb,
                "connect",
                side_effect=AssertionError("external connection attempted"),
            ),
        ):
            result = server.search_documents("match", 5)

        self.assertTrue(result["found"])
        self.assertEqual(result["results"], rows)
        self.assertIn("local database", result["message"])
        self.assertEqual(result["source"], "local")
        self.assertFalse(result["fallback_allowed"])

    def test_local_matches_returned_with_titles(self):
        local_rows = [
            {
                "content": "local match",
                "source": "local",
                "created_at": "today",
                "title": "Local script",
            }
        ]
        local = make_database(local_rows, columns=["content", "source", "created_at", "title"])

        results = server._search_database(local, "match", 5)

        self.assertEqual(results, local_rows)
        local.open_table.return_value.search.assert_called_once_with(
            "match", query_type="fts", fts_columns=["content", "title"]
        )

    def test_local_miss_allows_client_configured_external_fallback(self):
        local = make_database([])
        with (
            patch.object(server, "db", local),
            patch.dict(
                os.environ,
                {
                    "LANCEDB_CLOUD_URI": "https://example.invalid",
                    "LANCEDB_CLOUD_API_KEY": "must-not-be-used",
                },
            ),
            patch.object(server.lancedb, "connect", side_effect=AssertionError("external connection attempted")),
        ):
            result = server.search_documents("no match", 5)

        self.assertEqual(
            result,
            {
                "found": False,
                "results": [],
                "message": (
                    "Not found in local database. Continue with the client's configured "
                    "web, cloud, or AI sources if available, and identify any external sources."
                ),
                "source": "local",
                "fallback_allowed": True,
            },
        )
        local.open_table.return_value.search.assert_called_once_with(
            "no match", query_type="fts"
        )

    def test_missing_local_table_returns_explicit_not_found(self):
        local = make_database([], table_exists=False)
        with patch.object(server, "db", local):
            result = server.search_documents("no table", 5)

        self.assertFalse(result["found"])
        self.assertEqual(result["results"], [])
        self.assertTrue(result["fallback_allowed"])
        local.open_table.assert_not_called()

    def test_local_search_error_is_reported_before_any_external_fallback(self):
        local = make_database([])
        local.open_table.side_effect = RuntimeError("local database failure")
        with (
            patch.object(server, "db", local),
            patch.object(server.lancedb, "connect", side_effect=AssertionError("external connection attempted")),
        ):
            with self.assertRaisesRegex(RuntimeError, "local database failure"):
                server.search_documents("query", 5)


if __name__ == "__main__":
    unittest.main()
