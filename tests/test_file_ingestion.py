import tempfile
import unittest
import importlib.util
import zipfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_ingestion import (
    CHUNK_CHARACTERS,
    MAX_OFFICE_FILE_BYTES,
    MAX_PDF_FILE_BYTES,
    MAX_TEXT_FILE_BYTES,
    prepare_dropped_files,
    validate_office_runtime,
)
from openpyxl import Workbook
from docx import Document


class PrepareDroppedFilesTests(unittest.TestCase):
    def test_office_runtime_validation_smoke(self):
        excel_version, word_version = validate_office_runtime()

        self.assertRegex(excel_version, r"^\d+\.\d+")
        self.assertRegex(word_version, r"^\d+\.\d+")

    def test_xlsx_extracts_sheets_rows_metadata_and_formula_text_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "sales.xlsx"
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = "Sales 2026"
            worksheet.append(["Product", "Revenue"])
            worksheet.append(["Widget", 42])
            worksheet.append(["Formula", "=1+1"])
            workbook.create_sheet("Notes").append(["Workbook note"])
            workbook.save(path)
            workbook.close()

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(skipped, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(records), 4)
        self.assertIn("sheet 'Sales 2026'; row 1", records[0]["title"])
        self.assertIn("Product: Widget", records[1]["content"])
        self.assertIn("sheet 'Sales 2026'; row 2", records[1]["title"])
        self.assertIn("=1+1", records[2]["content"])
        self.assertIn("sheet 'Notes'; row 1", records[3]["title"])
        self.assertTrue(all(record["source"] == "file-drop:sales.xlsx" for record in records))
        self.assertTrue(all(record["file_fingerprint"] == records[0]["file_fingerprint"] for record in records))

    def test_docx_extracts_paragraphs_tables_sections_and_row_metadata(self):
        from docx.enum.section import WD_SECTION

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "meeting.docx"
            document = Document()
            document.add_paragraph("Agenda overview.")
            document.add_section(WD_SECTION.NEW_PAGE)
            document.add_paragraph("Second section decision.")
            table = document.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "Owner"
            table.cell(0, 1).text = "Action"
            table.cell(1, 0).text = "Jordan"
            table.cell(1, 1).text = "Review roadmap"
            document.save(path)

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(skipped, [])
        self.assertEqual(pending, [])
        self.assertTrue(any("paragraph 1" in record["title"] for record in records))
        self.assertTrue(any("section 2; paragraph" in record["title"] for record in records))
        table_row = next(record for record in records if "table 1; row 2" in record["title"])
        self.assertIn("Jordan", table_row["content"])
        self.assertIn("Review roadmap", table_row["content"])
        self.assertTrue(all(record["source"] == "file-drop:meeting.docx" for record in records))

    def test_docx_ingests_more_than_500_records_within_safe_drop_bound(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "large-word.docx"
            document = Document()
            for index in range(600):
                document.add_paragraph(f"Unique searchable paragraph number {index}.")
            document.save(path)

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(skipped, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(records), 600)
        self.assertIn("paragraph 600", records[-1]["title"])

    def test_empty_excel_and_word_files_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workbook_path = root / "empty.xlsx"
            Workbook().save(workbook_path)
            document_path = root / "empty.docx"
            Document().save(document_path)

            records, skipped, pending = prepare_dropped_files([workbook_path, document_path])

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(skipped), 2)
        self.assertTrue(all("no readable text" in item for item in skipped))

    def test_corrupt_and_password_protected_excel_packages_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            corrupt = root / "corrupt.xlsx"
            corrupt.write_bytes(b"not an Office package")
            encrypted = root / "encrypted.xlsx"
            workbook = Workbook()
            workbook.save(encrypted)
            workbook.close()
            contents = bytearray(encrypted.read_bytes())
            central_header = contents.find(b"PK\x01\x02")
            self.assertGreaterEqual(central_header, 0)
            flags = int.from_bytes(contents[central_header + 8 : central_header + 10], "little")
            contents[central_header + 8 : central_header + 10] = (flags | 1).to_bytes(2, "little")
            encrypted.write_bytes(contents)

            records, skipped, pending = prepare_dropped_files([corrupt, encrypted])

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertTrue(any("invalid or password-encrypted" in item for item in skipped))
        self.assertTrue(any("password-protected" in item for item in skipped))

    def test_corrupt_word_package_is_reported_without_ingestion(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "corrupt.docx"
            path.write_bytes(b"not an Office package")

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("invalid or password-encrypted", skipped[0])

    def test_office_password_and_macro_files_are_not_opened_for_ingestion(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            encrypted_docx = root / "locked.docx"
            encrypted_docx.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"encrypted")
            macro_workbook = root / "macro.xlsx"
            workbook = Workbook()
            workbook.save(macro_workbook)
            workbook.close()
            with zipfile.ZipFile(macro_workbook, "a") as archive:
                archive.writestr("xl/vbaProject.bin", b"macro data")
            legacy_excel = root / "legacy.xls"
            legacy_excel.write_bytes(b"not supported")
            legacy_word = root / "legacy.doc"
            legacy_word.write_bytes(b"not supported")

            records, skipped, pending = prepare_dropped_files(
                [encrypted_docx, macro_workbook, legacy_excel, legacy_word]
            )

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertTrue(any("invalid or password-encrypted" in item for item in skipped))
        self.assertTrue(any("macro-enabled" in item for item in skipped))
        self.assertEqual(sum("unsupported file type" in item for item in skipped), 2)

    def test_office_file_limit_is_100_mib(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "large.xlsx"
            with path.open("wb") as file:
                file.truncate(MAX_OFFICE_FILE_BYTES + 1)

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("100 MB limit", skipped[0])

    def test_repeated_excel_chunks_are_retained_for_server_duplicate_detection(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.xlsx"
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.append(["Item"])
            worksheet.append(["Repeated local value"])
            worksheet.append(["Repeated local value"])
            workbook.save(first)
            workbook.close()
            copied = root / "renamed.xlsx"
            copied.write_bytes(first.read_bytes())

            first_records, first_skipped, _ = prepare_dropped_files([first])
            copied_records, copied_skipped, _ = prepare_dropped_files([copied])

        repeated = [record for record in first_records if record["content"] == "Item: Repeated local value"]
        self.assertEqual(first_skipped, [])
        self.assertEqual(copied_skipped, [])
        self.assertEqual(len(repeated), 2)
        self.assertEqual(
            first_records[0]["file_fingerprint"],
            copied_records[0]["file_fingerprint"],
        )
        self.assertNotEqual(first_records[0]["ingest_id"], copied_records[0]["ingest_id"])

    def test_excel_credential_like_cell_is_staged_for_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "account.xlsx"
            workbook = Workbook()
            workbook.active.append(["api_key = test-only-secret"])
            workbook.save(path)
            workbook.close()

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(records, [])
        self.assertEqual(skipped, [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["reason"], "credential-like literal detected")
        self.assertIn("test-only-secret", pending[0]["records"][0]["content"])

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
    def test_pdf_runtime_validation_smoke(self):
        from file_ingestion import validate_pdf_runtime

        self.assertTrue(validate_pdf_runtime())

    def test_supported_text_file_is_titled_and_sourced_with_line_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "notes.md"
            path.write_text("# Local note\nUseful content.\n", encoding="utf-8")

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(skipped, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "notes.md (lines 1-2; chunk 1/1)")
        self.assertEqual(records[0]["source"], "file-drop:notes.md")
        self.assertIn("Useful content.", records[0]["content"])
        self.assertRegex(records[0]["file_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertRegex(records[0]["ingest_id"], r"^[0-9a-f]{32}$")

    def test_identical_files_get_same_content_fingerprint_but_separate_ingest_ids(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "notes.txt"
            renamed = root / "renamed.md"
            first.write_text("same bytes\n", encoding="utf-8")
            renamed.write_bytes(first.read_bytes())

            first_records, first_skipped, _ = prepare_dropped_files([first])
            renamed_records, renamed_skipped, _ = prepare_dropped_files([renamed])

        self.assertEqual(first_skipped, [])
        self.assertEqual(renamed_skipped, [])
        self.assertEqual(
            first_records[0]["file_fingerprint"],
            renamed_records[0]["file_fingerprint"],
        )
        self.assertNotEqual(first_records[0]["ingest_id"], renamed_records[0]["ingest_id"])

    def test_large_text_is_chunked_with_line_ranges_and_stays_within_record_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "large.py"
            path.write_text(
                "\n".join(f"line {index}: " + ("x" * 300) for index in range(130)),
                encoding="utf-8",
            )

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(skipped, [])
        self.assertEqual(pending, [])
        self.assertGreater(len(records), 1)
        self.assertTrue(all(len(record["content"]) <= CHUNK_CHARACTERS for record in records))
        self.assertTrue(all(f"chunk {index}/{len(records)}" in record["title"] for index, record in enumerate(records, 1)))
        self.assertEqual(len({record["ingest_id"] for record in records}), 1)

    def test_unsupported_directories_and_sensitive_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            unsupported = root / "archive.zip"
            unsupported.write_bytes(b"not text")
            directory = root / "folder"
            directory.mkdir()
            env_file = root / ".env"
            env_file.write_text("TOKEN=private-value", encoding="utf-8")
            private_file = root / "notes.txt"
            private_file.write_text(
                "-----BEGIN PRIVATE KEY-----\nprivate-value",
                encoding="utf-8",
            )

            records, skipped, pending = prepare_dropped_files(
                [unsupported, directory, env_file, private_file]
            )

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(skipped), 4)
        self.assertTrue(any("unsupported file type" in item for item in skipped))
        self.assertTrue(any("folders and non-files" in item for item in skipped))
        self.assertTrue(any("environment files" in item for item in skipped))
        self.assertTrue(any("private-key material" in item for item in skipped))

    def test_credential_literal_and_key_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            credential_file = root / "service_credentials.json"
            credential_file.write_text("{}", encoding="utf-8")
            literal_secret = root / "config.py"
            literal_secret.write_text(
                'api_key = "definitely-not-a-placeholder-secret"\n',
                encoding="utf-8",
            )
            private_key = root / "client.pem"
            private_key.write_text("no key contents", encoding="utf-8")

            records, skipped, pending = prepare_dropped_files(
                [credential_file, literal_secret, private_key]
            )

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(skipped), 3)
        self.assertTrue(any("credential/private-key named files" in item for item in skipped))
        self.assertTrue(any("credential-like literal" in item for item in skipped))
        self.assertTrue(any("key and certificate files" in item for item in skipped))

    def test_file_size_and_file_count_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            too_large = root / "too-large.txt"
            with too_large.open("wb") as file:
                file.truncate(MAX_TEXT_FILE_BYTES + 1)
            files = []
            for index in range(11):
                path = root / f"note-{index}.txt"
                path.write_text(f"note {index}", encoding="utf-8")
                files.append(path)

            records, skipped, pending = prepare_dropped_files([too_large, *files])

        self.assertEqual(len(records), 9)
        self.assertEqual(pending, [])
        self.assertTrue(any("5 MiB limit" in item for item in skipped))
        self.assertTrue(any("Only the first 10" in item for item in skipped))

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
    def test_pdf_size_limit_accepts_exactly_100_mb_and_rejects_one_byte_over(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            at_limit = root / "at-limit.pdf"
            oversized = root / "oversized.pdf"
            with at_limit.open("wb") as file:
                file.truncate(MAX_PDF_FILE_BYTES)
            with oversized.open("wb") as file:
                file.truncate(MAX_PDF_FILE_BYTES + 1)

            with patch(
                "file_ingestion._read_pdf_pages",
                return_value=([(1, "PDF boundary content.")], None),
            ) as read_pdf:
                records, skipped, pending = prepare_dropped_files([at_limit])
                self.assertEqual(len(records), 1)
                self.assertEqual(skipped, [])
                self.assertEqual(pending, [])
                read_pdf.assert_called_once_with(at_limit)

            with patch("file_ingestion._read_pdf_pages") as read_pdf:
                records, skipped, pending = prepare_dropped_files([oversized])

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("100 MB limit", skipped[0])
        read_pdf.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
    def test_pdf_text_is_extracted_with_page_and_chunk_metadata(self):
        import pymupdf as fitz

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "report.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "First page searchable content.")
            document.new_page()
            document.save(path)
            document.close()

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(len(records), 1)
        self.assertEqual(pending, [])
        self.assertIn("page 1", records[0]["title"])
        self.assertIn("chunk 1/1", records[0]["title"])
        self.assertIn("First page searchable content.", records[0]["content"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("pages 2", skipped[0])
        self.assertIn("OCR is not used", skipped[0])

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
    def test_textless_pdf_is_reported_without_ocr(self):
        import pymupdf as fitz

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "scan.pdf"
            document = fitz.open()
            document.new_page()
            document.save(path)
            document.close()

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("no extractable text", skipped[0])
        self.assertIn("OCR is not used", skipped[0])

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
    def test_password_protected_pdf_is_skipped(self):
        import pymupdf as fitz

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "locked.pdf"
            document = fitz.open()
            document.new_page().insert_text((72, 72), "private PDF text")
            document.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
            document.close()

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("password-protected PDFs", skipped[0])

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
    def test_malformed_pdf_is_reported_without_database_records(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "malformed.pdf"
            path.write_bytes(b"%PDF-1.7\nnot a valid PDF document")

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("could not read PDF", skipped[0])

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
    def test_pdf_page_count_is_bounded(self):
        import pymupdf as fitz

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "many-pages.pdf"
            document = fitz.open()
            for _ in range(251):
                document.new_page()
            document.save(path)
            document.close()

            records, skipped, pending = prepare_dropped_files([path])

        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("250-page limit", skipped[0])

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
    def test_literal_credentials_in_pdf_are_staged_for_consent_without_secret_exposure(self):
        import pymupdf as fitz

        secret = "realistic-test-only-api-secret"
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "account.pdf"
            document = fitz.open()
            document.new_page().insert_text((72, 72), f"api_key = {secret}")
            document.save(path)
            document.close()

            records, skipped, confirmation_required = prepare_dropped_files([path])

        self.assertEqual(records, [])
        self.assertEqual(skipped, [])
        self.assertEqual(len(confirmation_required), 1)
        candidate = confirmation_required[0]
        self.assertEqual(candidate["file_name"], "account.pdf")
        self.assertEqual(candidate["reason"], "credential-like literal detected")
        self.assertIn(secret, candidate["records"][0]["content"])
        self.assertNotIn(secret, repr(skipped))
        self.assertNotIn(secret, repr(candidate["reason"]))



if __name__ == "__main__":
    unittest.main()
