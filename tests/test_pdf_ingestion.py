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


class _ContinuationOCREngine:
    def recognize(self, page, language, dpi):
        if page.number == 0:
            lines = [
                {
                    "text": "OCR narration continues to",
                    "bbox": [72, 600, 350, 618],
                    "font_size": 11,
                    "font": "FakeOCR",
                    "confidence": 0.99,
                }
            ]
        else:
            lines = [
                {
                    "text": "the next scanned page without a paragraph break.",
                    "bbox": [72, 86, 430, 104],
                    "font_size": 11,
                    "font": "FakeOCR",
                    "confidence": 0.99,
                }
            ]
        return lines, {"engine": "continuation-fake", "dpi": dpi, "mean_confidence": 0.99}


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
            progress_messages = []
            self._write_native_fixture(path)

            document = build_source_document(
                path,
                pdf_config=PDFIngestionConfig(ocr_mode="off"),
                artifact_dir=artifacts,
                progress_callback=progress_messages.append,
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
            self.assertIn("Ingesting PDF page 1/3...", progress_messages)
            self.assertIn("Analyzing PDF structure and layout...", progress_messages)
            self.assertIn("Saving structured PDF ingestion cache...", progress_messages)

    def test_force_ocr_uses_injected_engine_and_records_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "scan.pdf")
            progress_messages = []
            document = fitz.open()
            document.new_page(width=500, height=700)
            document.save(path)
            document.close()

            structured = build_pdf_source_document(
                path,
                config=PDFIngestionConfig(ocr_mode="force"),
                ocr_engine=_FakeOCREngine(),
                progress_callback=progress_messages.append,
            )

            self.assertIn("OCR narration begins here.", structured.plain_text())
            self.assertTrue(all(block.attributes["source_method"] == "ocr" for block in structured.blocks))
            self.assertEqual(
                structured.attributes["pdf_ingestion"]["pages"][0]["ocr"]["engine"],
                "fake",
            )
            self.assertIn("Running OCR on PDF page 1/1...", progress_messages)

    def test_page_continuations_reflow_cleaned_native_and_ocr_text_without_crossing_sentences(self):
        with tempfile.TemporaryDirectory() as directory:
            native_path = os.path.join(directory, "native-continuations.pdf")
            native = fitz.open()
            for page_number in range(1, 5):
                page = native.new_page(width=500, height=700)
                if page_number == 1:
                    page.insert_textbox(
                        fitz.Rect(72, 540, 430, 630),
                        "This deliberately long narration carries an unfinished thought across the physical "
                        "page boundary without ending its paragraph at the artificial PDF",
                        fontsize=11,
                    )
                elif page_number == 2:
                    page.insert_textbox(
                        fitz.Rect(72, 80, 430, 240),
                        "boundary and preserves the single paragraph for the audiobook "
                        "listener instead of creating an artificial pause.",
                        fontsize=11,
                    )
                    page.insert_textbox(
                        fitz.Rect(72, 540, 430, 630),
                        "This separate and deliberately long paragraph ends here with a complete sentence.",
                        fontsize=11,
                    )
                elif page_number == 3:
                    page.insert_textbox(
                        fitz.Rect(72, 80, 430, 240),
                        "A fresh and deliberately long paragraph starts on a new page after the complete "
                        "sentence, so it must remain separate in the cleaned output.",
                        fontsize=11,
                    )
                    page.insert_textbox(
                        fitz.Rect(72, 540, 430, 630),
                        "This deliberately long explanatory line reaches the page edge and carries a single "
                        "unbroken international term from the previous line fragment inter-",
                        fontsize=11,
                    )
                else:
                    page.insert_textbox(
                        fitz.Rect(72, 80, 430, 240),
                        "national example that remains one word after the page continuation is reflowed "
                        "for the audiobook listener and remains continuous in the final narration output.",
                        fontsize=11,
                    )
            native.save(native_path)
            native.close()

            structured = build_source_document(native_path, pdf_config=PDFIngestionConfig(ocr_mode="off"))
            by_text = {block.text: block for block in structured.blocks}
            self.assertEqual(
                by_text[
                    "boundary and preserves the single paragraph for the audiobook listener "
                    "instead of creating an artificial pause."
                ].attributes[
                    "continuation_from_block_id"
                ],
                by_text[
                    "This deliberately long narration carries an unfinished thought across the physical page "
                    "boundary without ending its paragraph at the artificial PDF"
                ].block_id,
            )
            self.assertEqual(
                by_text[
                    "national example that remains one word after the page continuation is reflowed for "
                    "the audiobook listener and remains continuous in the final narration output."
                ].attributes["continuation_join"],
                "remove_hyphen",
            )
            cleaned = apply_cleaning_operations(structured, propose_deterministic_operations(structured))
            self.assertIn(
                "This deliberately long narration carries an unfinished thought across the physical page boundary "
                "without ending its paragraph at the artificial PDF "
                "boundary and preserves the single "
                "paragraph",
                cleaned.cleaned_text,
            )
            self.assertIn("international example that remains one word", cleaned.cleaned_text)
            self.assertIn(
                "This separate and deliberately long paragraph ends here with a complete sentence.\n\n"
                "A fresh and deliberately long paragraph starts on a new page",
                cleaned.cleaned_text,
            )
            self.assertEqual(cleaned.report["page_continuation_join_count"], 2)

            ocr_path = os.path.join(directory, "ocr-continuations.pdf")
            ocr = fitz.open()
            ocr.new_page(width=500, height=700)
            ocr.new_page(width=500, height=700)
            ocr.save(ocr_path)
            ocr.close()
            ocr_document = build_pdf_source_document(
                ocr_path,
                config=PDFIngestionConfig(ocr_mode="force"),
                ocr_engine=_ContinuationOCREngine(),
            )
            ocr_cleaned = apply_cleaning_operations(ocr_document, [])
            self.assertIn(
                "OCR narration continues to the next scanned page without a paragraph break.",
                ocr_cleaned.cleaned_text,
            )
            self.assertEqual(ocr_cleaned.report["page_continuation_join_count"], 1)

    def test_page_continuation_does_not_cross_a_chapter_heading_at_a_page_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "chapter-boundary.pdf")
            document = fitz.open()
            first_page = document.new_page(width=500, height=700)
            first_page.insert_textbox(
                fitz.Rect(72, 500, 430, 610),
                "This deliberately long paragraph reaches the end of the page without terminal punctuation "
                "but must not continue beyond the chapter heading",
                fontsize=11,
            )
            first_page.insert_text((72, 660), "Chapter 2", fontsize=16)
            second_page = document.new_page(width=500, height=700)
            second_page.insert_textbox(
                fitz.Rect(72, 80, 430, 220),
                "lowercase text begins a deliberately long new chapter paragraph and should remain separate "
                "from the previous chapter despite its lowercase first letter.",
                fontsize=11,
            )
            document.save(path)
            document.close()

            structured = build_source_document(path, pdf_config=PDFIngestionConfig(ocr_mode="off"))
            next_page_text = next(
                block
                for block in structured.blocks
                if block.text.startswith("lowercase text begins")
            )
            self.assertNotIn("continuation_from_block_id", next_page_text.attributes)

            cleaned = apply_cleaning_operations(structured, [])
            self.assertIn(
                "chapter heading\n\nChapter 2\n\nlowercase text begins",
                cleaned.cleaned_text,
            )
            self.assertEqual(cleaned.report["page_continuation_join_count"], 0)

    def test_page_continuation_does_not_reflow_an_identified_abbreviations_section(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "abbreviations-boundary.pdf")
            document = fitz.open()
            first_page = document.new_page(width=500, height=700)
            first_page.insert_text((72, 90), "List of Abbreviations", fontsize=16)
            first_page.insert_textbox(
                fitz.Rect(72, 500, 430, 610),
                "This deliberately long abbreviations entry reaches the end of the page without terminal "
                "punctuation but is not narrative prose",
                fontsize=11,
            )
            second_page = document.new_page(width=500, height=700)
            second_page.insert_textbox(
                fitz.Rect(72, 80, 430, 220),
                "lowercase continuation-like text represents another abbreviations entry and must remain "
                "separate even though its geometry resembles a prose page continuation.",
                fontsize=11,
            )
            document.save(path)
            document.close()

            structured = build_source_document(path, pdf_config=PDFIngestionConfig(ocr_mode="off"))
            next_page_text = next(
                block
                for block in structured.blocks
                if block.text.startswith("lowercase continuation-like text")
            )
            self.assertNotIn("continuation_from_block_id", next_page_text.attributes)
            self.assertGreaterEqual(
                next(
                    block
                    for block in structured.blocks
                    if block.text == "List of Abbreviations"
                ).role_score("non_narrative_section"),
                0.85,
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

    def test_pdf_heading_policy_promotes_content_openers_without_promoting_title_or_notes_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "headings.pdf")
            document = fitz.open()

            title_page = document.new_page(width=500, height=700)
            title_page.insert_text((72, 90), "EXAMPLE ANTHOLOGY", fontsize=20)

            chapter_page = document.new_page(width=500, height=700)
            chapter_page.insert_text((72, 90), "Chapter 1", fontsize=12)
            chapter_page.insert_text(
                (72, 140),
                "Narration begins after the chapter heading and continues with enough body text to establish "
                "that this is a genuine content-opening page rather than a title page.",
                fontsize=11,
            )

            story_page = document.new_page(width=500, height=700)
            story_page.insert_text((72, 90), "A Short Story", fontsize=18)
            story_page.insert_textbox(
                fitz.Rect(72, 140, 430, 280),
                "Narration begins after the story title and continues with enough body text to establish "
                "that this is a genuine content-opening page rather than a title page.",
                fontsize=11,
            )

            noisy_page = document.new_page(width=500, height=700)
            noisy_page.insert_text((72, 30), "13 INTRODUCTION", fontsize=8)
            noisy_page.insert_text((72, 90), "I was thinking about ordinary narration.", fontsize=11)
            noisy_page.insert_text((230, 680), "1 30", fontsize=8)

            bibliography_page = document.new_page(width=500, height=700)
            bibliography_page.insert_text((72, 90), "Select Bibliography", fontsize=18)
            bibliography_page.insert_text(
                (72, 140),
                "A list of sources follows this non-narrative heading and is deliberately long enough to "
                "look like ordinary page content to the geometry-aware extractor.",
                fontsize=11,
            )

            abbreviations_page = document.new_page(width=500, height=700)
            abbreviations_page.insert_text((72, 90), "List of Abbreviations", fontsize=18)
            abbreviations_page.insert_textbox(
                fitz.Rect(72, 140, 430, 280),
                "A list of abbreviations follows this non-narrative heading and is deliberately long enough "
                "to look like ordinary page content to the geometry-aware extractor.",
                fontsize=11,
            )

            document.save(path)
            document.close()

            structured = build_source_document(path, pdf_config=PDFIngestionConfig(ocr_mode="off"))
            by_text = {block.text: block for block in structured.blocks}

            self.assertLess(by_text["EXAMPLE ANTHOLOGY"].role_score("deterministic_chapter"), 0.85)
            self.assertGreaterEqual(by_text["Chapter 1"].role_score("deterministic_chapter"), 0.85)
            self.assertGreaterEqual(by_text["A Short Story"].role_score("deterministic_chapter"), 0.85)
            self.assertLess(by_text["I was thinking about ordinary narration."].role_score("heading"), 0.45)
            self.assertLess(by_text["1 30"].role_score("heading"), 0.45)
            self.assertGreaterEqual(by_text["13 INTRODUCTION"].role_score("running_header"), 0.98)
            self.assertGreaterEqual(by_text["Select Bibliography"].role_score("non_narrative_section"), 0.85)
            self.assertLess(by_text["Select Bibliography"].role_score("deterministic_chapter"), 0.85)
            self.assertGreaterEqual(by_text["List of Abbreviations"].role_score("non_narrative_section"), 0.85)
            self.assertLess(by_text["List of Abbreviations"].role_score("deterministic_chapter"), 0.85)

            deleted = {
                block_id
                for operation in propose_deterministic_operations(structured)
                if operation["op"] == "delete_blocks"
                for block_id in operation["block_ids"]
            }
            self.assertIn(by_text["13 INTRODUCTION"].block_id, deleted)

    def test_pdf_toc_boilerplate_and_roman_footnotes_are_safe_to_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "front-matter.pdf")
            document = fitz.open()

            toc_page = document.new_page(width=500, height=700)
            toc_page.insert_text((72, 90), "Contents", fontsize=18)
            toc_page.insert_text((72, 140), "First Story 7", fontsize=11)
            toc_page.insert_text((72, 170), "Second Story 21", fontsize=11)

            continuation_page = document.new_page(width=500, height=700)
            continuation_page.insert_text((72, 90), "Third Story 43", fontsize=11)
            continuation_page.insert_text((72, 120), "Fourth Story 67", fontsize=11)

            copyright_page = document.new_page(width=500, height=700)
            copyright_page.insert_text((72, 90), "Copyright 2026 Example Press", fontsize=9)
            copyright_page.insert_text((72, 120), "All rights reserved.", fontsize=9)
            copyright_page.insert_text((72, 150), "ISBN 978-1-2345-6789-0", fontsize=9)

            body_page = document.new_page(width=500, height=700)
            body_page.insert_text((72, 90), "Chapter 1", fontsize=15)
            body_page.insert_text((72, 140), "Narration continues here with ordinary body text.", fontsize=11)
            body_page.insert_text((72, 620), "I A textual note for the printed edition.", fontsize=7)

            document.save(path)
            document.close()

            structured = build_source_document(path, pdf_config=PDFIngestionConfig(ocr_mode="off"))
            toc_blocks = [block for block in structured.blocks if block.page in {1, 2}]
            copyright_blocks = [block for block in structured.blocks if block.page == 3]
            note = next(block for block in structured.blocks if block.text.startswith("I A textual note"))

            self.assertTrue(all(block.role_score("toc") >= 0.92 for block in toc_blocks))
            self.assertTrue(all(block.role_score("boilerplate") >= 0.98 for block in copyright_blocks))
            self.assertGreaterEqual(note.role_score("footnote"), 0.92)

            operations = propose_deterministic_operations(structured, remove_footnotes=True)
            deleted = {
                block_id
                for operation in operations
                if operation["op"] == "delete_blocks"
                for block_id in operation["block_ids"]
            }
            self.assertTrue({block.block_id for block in toc_blocks}.issubset(deleted))
            self.assertTrue({block.block_id for block in copyright_blocks}.issubset(deleted))
            self.assertIn(note.block_id, deleted)

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
