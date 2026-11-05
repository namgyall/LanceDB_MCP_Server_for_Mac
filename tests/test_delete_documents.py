import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lancedb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class IngestedDocumentDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.app_data_patch = patch.object(
            server, "APP_DATA_DIR", Path(self.temporary_directory.name)
        )
        self.app_data_patch.start()
        self.database = lancedb.connect(self.temporary_directory.name)
        self.table = self.database.create_table(
            server.TABLE_NAME,
            data=[
                {
                    "content": "unique phrase to remove",
                    "source": "user's notes",
                    "created_at": "2026-11-03T10:00:00+00:00",
                },
                {
                    "content": "unique phrase to remove",
                    "source": "user's notes",
                    "created_at": "2026-11-03T10:00:00+00:00",
                },
                {
                    "content": "keep this searchable",
                    "source": "other source",
                    "created_at": "2026-11-03T10:01:00+00:00",
                },
            ],
        )
        self.table.create_index("content", config=server.FTS())

    def tearDown(self):
        self.app_data_patch.stop()
        self.temporary_directory.cleanup()

    def test_selected_document_deletion_updates_counts_and_search(self):
        documents = server.list_ingested_documents(self.database)
        selected = next(document for document in documents if "remove" in document["content"])

        deleted = server.delete_ingested_documents(
            [selected["id"]], self.database
        )

        self.assertEqual(deleted, 2)
        self.assertEqual(len(self.database.open_table(server.TABLE_NAME)), 1)
        self.assertEqual(
            server._search_database(self.database, "unique phrase to remove", 5),
            [],
        )
        self.assertEqual(
            server._search_database(self.database, "keep this searchable", 5)[0]["content"],
            "keep this searchable",
        )
        progress_log = (
            Path(self.temporary_directory.name) / "logs" / "app.log"
        ).read_text(encoding="utf-8")
        self.assertIn("Deleted 2 ingested record(s)", progress_log)
        self.assertNotIn("unique phrase to remove", progress_log)

    def test_clearing_ingested_records_preserves_other_tables(self):
        other_table = self.database.create_table(
            "unrelated",
            data=[{"value": "keep"}],
        )

        deleted = server.clear_ingested_documents(self.database)

        self.assertEqual(deleted, 3)
        self.assertEqual(len(self.database.open_table(server.TABLE_NAME)), 0)
        self.assertEqual(len(other_table), 1)
        self.assertEqual(server.list_ingested_documents(self.database), [])

    def test_stale_selection_is_rejected_without_deleting_records(self):
        with self.assertRaisesRegex(ValueError, "list is out of date"):
            server.delete_ingested_documents(["not-a-current-document"], self.database)

        self.assertEqual(len(self.database.open_table(server.TABLE_NAME)), 3)


if __name__ == "__main__":
    unittest.main()
