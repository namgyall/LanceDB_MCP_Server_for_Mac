import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from file_ingestion import prepare_dropped_files

lancedb = server.lancedb
pa = server.pa


class StoreImportantItemTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.app_data_patch = patch.object(
            server, "APP_DATA_DIR", Path(self.temporary_directory.name)
        )
        self.app_data_patch.start()
        self.database = lancedb.connect(self.temporary_directory.name)
        self.table = self.database.create_table(
            server.TABLE_NAME,
            schema=pa.schema(
                [
                    pa.field("content", pa.string()),
                    pa.field("source", pa.string()),
                    pa.field("created_at", pa.string()),
                ]
            ),
        )
        self.table.add(
            pa.Table.from_pylist(
                [
                    {
                        "content": "preexisting data must remain",
                        "source": "legacy",
                        "created_at": "2026-11-03T10:00:00+00:00",
                    }
                ]
            )
        )
        self.table.create_index("content", config=server.FTS())
        self.database_patch = patch.object(server, "db", self.database)
        self.database_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        self.app_data_patch.stop()
        self.temporary_directory.cleanup()

    def test_explicit_save_adds_title_metadata_and_preserves_old_records(self):
        message = server.store_important_item(
            "Cleanup script",
            "def cleanup():\n    return 'saved'",
            source="user-provided Python script",
        )

        self.assertIn("Cleanup script", message)
        table = self.database.open_table(server.TABLE_NAME)
        self.assertIn("title", table.schema.names)
        self.assertEqual(len(table), 2)
        documents = server.list_ingested_documents(self.database)
        saved = next(document for document in documents if document["title"])
        self.assertEqual(saved["title"], "Cleanup script")
        self.assertEqual(saved["source"], "user-provided Python script")
        self.assertEqual(saved["content"], "def cleanup():\n    return 'saved'")
        self.assertIsNone(next(document for document in documents if not document["title"])["title"])

    def test_invalid_item_is_not_stored(self):
        with self.assertRaisesRegex(ValueError, "Title must be a non-empty"):
            server.store_important_item("", "content", "source")

        self.assertEqual(len(self.database.open_table(server.TABLE_NAME)), 1)

    def test_server_startup_does_not_configure_console_logging(self):
        import uvicorn

        app = Mock()
        with (
            patch.object(server.mcp, "streamable_http_app", return_value=app),
            patch.object(server, "_record_progress"),
            patch.object(server, "optimize_database_on_startup") as optimize,
            patch.object(uvicorn, "run") as run,
        ):
            self.assertEqual(server.run_server(), 0)

        optimize.assert_called_once_with()
        run.assert_called_once_with(
            app,
            host="127.0.0.1",
            port=server.PORT,
            log_level="info",
            log_config=None,
            access_log=False,
        )

    def test_dashboard_store_route_saves_only_submitted_item(self):
        class FakeRequest:
            async def json(self):
                return {
                    "title": "Dashboard item",
                    "content": "Explicitly pasted content",
                    "source": "dashboard",
                }

        response = asyncio.run(server.store_important_item_route(FakeRequest()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"stored": 1, "duplicates": 0})
        saved = next(
            item
            for item in server.list_ingested_documents(self.database)
            if item["title"] == "Dashboard item"
        )
        self.assertEqual(saved["content"], "Explicitly pasted content")

    def test_q_and_a_tool_stores_question_and_answer_for_future_search(self):
        message = server.store_qa_item(
            "How do I preserve reports?",
            "Save each report in the local database with its title.",
            source="copilot-answer",
        )

        self.assertIn("future local search", message)
        saved = next(
            item
            for item in server.list_ingested_documents(self.database)
            if item["title"].startswith("Q&A:")
        )
        self.assertIn("How do I preserve reports?", saved["title"])
        self.assertIn("Question: How do I preserve reports?", saved["content"])
        self.assertIn("Answer: Save each report", saved["content"])
        self.assertEqual(saved["source"], "copilot-answer")

    def test_q_and_a_tool_rejects_empty_question_or_answer(self):
        with self.assertRaisesRegex(ValueError, "Question cannot be empty"):
            server.store_qa_item(" ", "valid answer")
        with self.assertRaisesRegex(ValueError, "Answer cannot be empty"):
            server.store_qa_item("valid question", " ")
        self.assertEqual(len(self.database.open_table(server.TABLE_NAME)), 1)

    def test_exact_duplicate_pdf_chunks_ignore_wrapper_and_page_metadata(self):
        content = "Repeated PDF page text that must only be stored once."
        records = [
            {
                "title": "first-wrapper.pdf (page 1; line 1; chunk 1/1)",
                "source": "file-drop:first-wrapper.pdf",
                "content": content,
                "file_fingerprint": "1" * 64,
                "ingest_id": "1" * 32,
            },
            {
                "title": "second-wrapper.pdf (page 7; line 1; chunk 1/1)",
                "source": "file-drop:second-wrapper.pdf",
                "content": content,
                "file_fingerprint": "2" * 64,
                "ingest_id": "2" * 32,
            },
        ]

        result = server._store_records(records)

        self.assertEqual(result, {"stored": 1, "duplicates": 1})
        duplicate = server._store_records(
            [
                {
                    "title": "third-wrapper.pdf (page 9; line 1; chunk 1/1)",
                    "source": "file-drop:third-wrapper.pdf",
                    "content": content,
                }
            ]
        )
        self.assertEqual(duplicate, {"stored": 0, "duplicates": 1})
        self.assertEqual(len(server.list_ingested_documents(self.database)), 2)

    def test_file_fingerprint_skips_same_file_with_a_different_basename(self):
        fingerprint = "a" * 64
        first = {
            "title": "original.txt (lines 1-1; chunk 1/1)",
            "source": "file-drop:original.txt",
            "content": "first unique chunk",
            "file_fingerprint": fingerprint,
            "ingest_id": "b" * 32,
        }
        renamed_duplicate = {
            "title": "renamed.txt (lines 1-1; chunk 1/1)",
            "source": "file-drop:renamed.txt",
            "content": "different chunk",
            "file_fingerprint": fingerprint,
            "ingest_id": "c" * 32,
        }

        first_result = server._store_records([first])
        duplicate_result = server._store_records([renamed_duplicate])

        self.assertEqual(first_result, {"stored": 1, "duplicates": 0})
        self.assertEqual(duplicate_result, {"stored": 0, "duplicates": 1})

    def test_legacy_file_records_without_ingest_id_are_deduped_across_batches(self):
        fingerprint = "d" * 64
        first_batch = [
            {
                "title": f"legacy.txt (lines {index}-{index}; chunk {index}/51)",
                "source": "file-drop:legacy.txt",
                "content": f"legacy chunk {index}",
                "file_fingerprint": fingerprint,
            }
            for index in range(1, 51)
        ]
        final_chunk = {
            "title": "legacy.txt (lines 51-51; chunk 51/51)",
            "source": "file-drop:legacy.txt",
            "content": "legacy chunk 51",
            "file_fingerprint": fingerprint,
        }

        first_result = server._store_records(first_batch)
        second_result = server._store_records([final_chunk])

        self.assertEqual(first_result, {"stored": 50, "duplicates": 0})
        self.assertEqual(second_result, {"stored": 1, "duplicates": 0})
        self.assertEqual(len(server.list_ingested_documents(self.database)), 52)

    def test_store_documents_dedupes_within_batch_and_against_existing_rows(self):
        message = server.store_documents(
            ["same note content", "same note content", "a unique note"]
        )
        self.assertIn("Stored 2 document(s)", message)
        self.assertIn("skipped 1 exact duplicate(s)", message)

        repeated = server.store_documents(["same note content"])
        self.assertIn("Stored 0 document(s)", repeated)
        self.assertIn("skipped 1 exact duplicate(s)", repeated)
        self.assertEqual(len(server.list_ingested_documents(self.database)), 3)

    def test_important_and_qa_items_report_duplicate_content(self):
        content = "exact important note content"
        saved = server.store_important_item("First title", content)
        repeated = server.store_important_item("Different title", content)
        self.assertIn("Stored 'First title'", saved)
        self.assertIn("exact duplicate", repeated)

        question = "What is the duplicate behavior?"
        answer = "Exact question and answer pairs are stored only once."
        first_qa = server.store_qa_item(question, answer)
        repeated_qa = server.store_qa_item(question, answer)
        self.assertIn("Saved the answer", first_qa)
        self.assertIn("exact duplicate", repeated_qa)

    def test_long_q_and_a_question_keeps_valid_title_and_full_searchable_content(self):
        question = "q" * 600
        server.store_qa_item(question, "answer")

        saved = next(
            item
            for item in server.list_ingested_documents(self.database)
            if item["title"].startswith("Q&A:")
        )
        self.assertEqual(len(saved["title"]), 500)
        self.assertIn(question, saved["content"])

    def test_file_batch_route_persists_only_submitted_chunks(self):
        class FakeRequest:
            async def json(self):
                return {
                    "documents": [
                        {
                            "title": "sample.py (lines 1-2; chunk 1/1)",
                            "source": "file-drop:sample.py",
                            "content": "print('not executed')",
                        }
                    ]
                }

        response = asyncio.run(server.store_documents_batch_route(FakeRequest()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"stored": 1, "duplicates": 0})
        saved = next(
            item
            for item in server.list_ingested_documents(self.database)
            if item["source"] == "file-drop:sample.py"
        )
        self.assertEqual(saved["content"], "print('not executed')")

    def test_file_batch_route_reports_duplicate_chunks(self):
        class FakeRequest:
            async def json(self):
                return {
                    "documents": [
                        {
                            "title": "first.pdf (page 1; line 1; chunk 1/1)",
                            "source": "file-drop:first.pdf",
                            "content": "Repeated page text.",
                            "file_fingerprint": "1" * 64,
                            "ingest_id": "1" * 32,
                        },
                        {
                            "title": "second.pdf (page 6; line 1; chunk 1/1)",
                            "source": "file-drop:second.pdf",
                            "content": "Repeated page text.",
                            "file_fingerprint": "2" * 64,
                            "ingest_id": "2" * 32,
                        },
                    ]
                }

        response = asyncio.run(server.store_documents_batch_route(FakeRequest()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"stored": 1, "duplicates": 1})

    @unittest.skipUnless(
        importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed"
    )
    def test_extracted_pdf_text_is_persisted_with_page_metadata(self):
        import pymupdf

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "reference.pdf"
            document = pymupdf.open()
            document.new_page().insert_text((72, 72), "Local PDF retrieval marker.")
            document.save(path)
            document.close()

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(skipped, [])
        self.assertEqual(pending, [])
        server._store_records(records)
        saved = next(
            item
            for item in server.list_ingested_documents(self.database)
            if item["source"] == "file-drop:reference.pdf"
        )
        self.assertIn("Local PDF retrieval marker", saved["content"])
        self.assertIn("page 1", saved["title"])


if __name__ == "__main__":
    unittest.main()
