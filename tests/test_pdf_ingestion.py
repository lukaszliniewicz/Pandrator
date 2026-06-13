import json
import os
import tempfile
import unittest

import fitz

from pandrator.logic.source_cleaning import (
    PDFIngestionConfig,
    apply_cleaning_operations,
    build_source_document,
    propose_deterministic_operations,
)
from pandrator.logic.source_cleaning.pdf_adapter import build_source_document as build_pdf_source_document


class _FakeOCREngine:
    def recognize(self, page, language, dpi):
        return (
            [
                {
                    "text": "Chapter I",
                    "bbox": [72, 72, 180, 90],
                    "font_size": 16,
                    "font": "FakeOCR",
                    "confidence": 0.99,
                },
                {
                    "text": "OCR narration begins here.",
                    "bbox": [72, 110, 400, 128],
                    "font_size": 11,
                    "font": "FakeOCR",
                    "confidence": 0.98,
                },
            ],
            {"engine": "fake", "dpi": dpi, "mean_confidence": 0.985},
        )


class PDFIngestionTests(unittest.TestCase):
    def _write_native_fixture(self, path):
        document = fitz.open()
        for page_number in range(1, 4):
            page = document.new_page(width=500, height=700)
            page.insert_text((72, 30), "Running Header", fontsize=8)
            page.insert_text((230, 680), str(page_number), fontsize=8)
            page.insert_text((72, 90), f"Chapter {page_number}", fontsize=18)
            page.insert_text((72, 130), f"Narration on page {page_number}.", fontsize=11)
        document.save(path)
        document.close()

    def test_native_pdf_preserves_geometry_and_proposes_safe_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.pdf")
            artifacts = os.path.join(directory, "artifacts")
            self._write_native_fixture(path)

            document = build_source_document(
                path,
                pdf_config=PDFIngestionConfig(ocr_mode="off"),
                artifact_dir=artifacts,
            )

            header_blocks = [block for block in document.blocks if block.text == "Running Header"]
            self.assertEqual(len(header_blocks), 3)
            self.assertTrue(all(block.attributes.get("bbox") for block in header_blocks))
            self.assertTrue(all(block.role_score("repeated_marginal") >= 0.95 for block in header_blocks))
            operations = propose_deterministic_operations(document)
            cleaned = apply_cleaning_operations(document, operations)
            self.assertNotIn("Running Header", cleaned.cleaned_text)
            self.assertIn("[[Chapter]]Chapter 1", cleaned.cleaned_text)
            self.assertTrue(os.path.isfile(os.path.join(artifacts, "source_document.json")))

            with open(os.path.join(artifacts, "ingestion_report.json"), "r", encoding="utf-8") as file_handle:
                report = json.load(file_handle)
            self.assertEqual(len(report["pages"]), 3)

    def test_force_ocr_uses_injected_engine_and_records_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "scan.pdf")
            document = fitz.open()
            document.new_page(width=500, height=700)
            document.save(path)
            document.close()

            structured = build_pdf_source_document(
                path,
                config=PDFIngestionConfig(ocr_mode="force"),
                ocr_engine=_FakeOCREngine(),
            )

            self.assertIn("OCR narration begins here.", structured.plain_text())
            self.assertTrue(all(block.attributes["source_method"] == "ocr" for block in structured.blocks))
            self.assertEqual(
                structured.attributes["pdf_ingestion"]["pages"][0]["ocr"]["engine"],
                "fake",
            )

    def test_two_column_native_text_is_grouped_in_column_reading_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "columns.pdf")
            document = fitz.open()
            page = document.new_page(width=600, height=700)
            page.insert_textbox(
                fitz.Rect(20, 60, 285, 300),
                "Left one.\nLeft two.\nLeft three.\nLeft four.",
                fontsize=11,
            )
            page.insert_textbox(
                fitz.Rect(315, 60, 580, 300),
                "Right one.\nRight two.\nRight three.\nRight four.",
                fontsize=11,
            )
            document.save(path)
            document.close()

            structured = build_source_document(path, pdf_config=PDFIngestionConfig(ocr_mode="off"))

            text = structured.plain_text()
            self.assertLess(text.index("Left two."), text.index("Right one."))
            self.assertTrue(any(block.attributes["reading_order"] == "two_columns" for block in structured.blocks))

    def test_decimal_ocr_artifact_is_not_marked_as_chapter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "decimal.pdf")
            document = fitz.open()
            page = document.new_page(width=500, height=700)
            page.insert_text((72, 80), "1478.455", fontsize=20)
            page.insert_text((72, 130), "Ordinary body narration follows here.", fontsize=11)
            document.save(path)
            document.close()

            structured = build_source_document(path, pdf_config=PDFIngestionConfig(ocr_mode="off"))
            decimal = next(block for block in structured.blocks if block.text == "1478.455")

            self.assertLess(decimal.role_score("deterministic_chapter"), 0.85)

    def test_pycroppdf_sidecar_is_preserved_in_document_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cropped.pdf")
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "Narration.")
            document.save(path)
            document.close()
            sidecar = {
                "schema": "pycroppdf.provenance",
                "source": {"path": "original.pdf", "sha256": "a" * 64},
                "page_map": [{"output_page": 1, "original_page": 3}],
            }
            with open(f"{path}.pycroppdf.json", "w", encoding="utf-8") as file_handle:
                json.dump(sidecar, file_handle)

            structured = build_source_document(path, pdf_config=PDFIngestionConfig(ocr_mode="off"))

            self.assertEqual(
                structured.attributes["pycroppdf_provenance"]["page_map"][0]["original_page"],
                3,
            )

    def test_large_roman_heading_is_not_removed_as_page_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "roman-heading.pdf")
            document = fitz.open()
            page = document.new_page(width=500, height=700)
            page.insert_text((230, 80), "III", fontsize=24)
            page.insert_text((72, 130), "Ordinary body narration follows here.", fontsize=11)
            document.save(path)
            document.close()

            structured = build_source_document(path, pdf_config=PDFIngestionConfig(ocr_mode="off"))
            roman = next(block for block in structured.blocks if block.text == "III")

            self.assertLess(roman.role_score("page_number"), 0.98)


if __name__ == "__main__":
    unittest.main()
