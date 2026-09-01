import json
import os
import tempfile
import unittest

import fitz

from pandrator.logic.source_cleaning import (
    PDFIngestionConfig,
    SourceCleaningTools,
    apply_cleaning_operations,
    build_source_document,
    propose_deterministic_operations,
)
from pandrator.logic.source_cleaning.models import SourceBlock, SourceDocument
from pandrator.logic.source_cleaning.pdf_adapter import (
    _annotate_layout_continuations,
    _annotate_page_continuations,
    _annotate_structural_roles,
    _geometry_order,
    _lines_to_blocks,
)
from pandrator.logic.source_cleaning.pdf_adapter import (
    build_source_document as build_pdf_source_document,
)


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
    def test_ocr_mode_normalization_accepts_ui_and_canonical_values(self):
        self.assertEqual(PDFIngestionConfig(ocr_mode="always").normalized().ocr_mode, "force")
        self.assertEqual(PDFIngestionConfig(ocr_mode="never").normalized().ocr_mode, "off")
        self.assertEqual(PDFIngestionConfig(ocr_mode="force").normalized().ocr_mode, "force")
        self.assertEqual(PDFIngestionConfig(ocr_mode="off").normalized().ocr_mode, "off")
        self.assertEqual(PDFIngestionConfig(ocr_mode="unknown").normalized().ocr_mode, "auto")

    def test_native_line_grouping_keeps_margins_and_headings_separate_from_body(self):
        def line(text, bbox, font_size):
            return {
                "text": text,
                "bbox": bbox,
                "block_index": 0,
                "font_size": font_size,
                "font": "Fixture",
                "confidence": None,
            }

        payloads = _lines_to_blocks(
            [
                line("JOURNAL TITLE", [72, 20, 180, 32], 10),
                line("12", [420, 20, 432, 32], 10),
                line("A Study of Extraction", [72, 82, 300, 106], 19),
                line("Ada Example", [72, 132, 170, 148], 13),
                line("I. INTRODUCTION", [72, 170, 200, 184], 11),
                line("Narration begins on this line and continues through a substantial explanation", [72, 196, 430, 209], 11),
                line("without becoming a separate paragraph, preserving the complete argument for narration.", [72, 210, 410, 223], 11),
            ],
            fitz.Rect(0, 0, 500, 700),
            "native",
        )
        self.assertEqual(
            [payload["text"] for payload in payloads],
            [
                "JOURNAL TITLE",
                "12",
                "A Study of Extraction",
                "Ada Example",
                "I. INTRODUCTION",
                "Narration begins on this line and continues through a substantial explanation without becoming a separate paragraph, preserving the complete argument for narration.",
            ],
        )

        document = SourceDocument(source_type="pdf_structured", source_path="fixture.pdf", filename="fixture.pdf")
        for index, payload in enumerate(payloads, start=1):
            document.blocks.append(
                SourceBlock(
                    block_id=f"b:{index}",
                    text=payload["text"],
                    line_start=index,
                    line_end=index,
                    source_index=index,
                    page=1,
                    tag="p",
                    attributes={
                        "bbox": payload["bbox"],
                        "page_size": [500, 700],
                        "font_size": payload["font_size"],
                        "role_evidence": {},
                    },
                )
            )
        _annotate_structural_roles(document)
        by_text = {block.text: block for block in document.blocks}
        self.assertGreaterEqual(by_text["JOURNAL TITLE"].role_score("running_header"), 0.98)
        self.assertGreaterEqual(by_text["12"].role_score("page_number"), 0.99)
        self.assertGreaterEqual(by_text["I. INTRODUCTION"].role_score("deterministic_chapter"), 0.85)
        self.assertLess(by_text["A Study of Extraction"].role_score("deterministic_chapter"), 0.85)

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
                "keep_hyphen",
            )
            cleaned = apply_cleaning_operations(structured, propose_deterministic_operations(structured))
            self.assertIn(
                "This deliberately long narration carries an unfinished thought across the physical page boundary "
                "without ending its paragraph at the artificial PDF "
                "boundary and preserves the single "
                "paragraph",
                cleaned.cleaned_text,
            )
            self.assertIn("inter-national example that remains one word", cleaned.cleaned_text)
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

    def test_page_continuation_does_not_join_a_bilingual_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bilingual-boundary.pdf")
            document = fitz.open()
            french_page = document.new_page(width=500, height=700)
            french_page.insert_textbox(
                fitz.Rect(72, 500, 430, 610),
                "Cette longue phrase française est poursuivie jusqu'à la page suivante sans ponctuation "
                "finale afin de vérifier que la traduction reste séparée",
                fontsize=11,
            )
            english_page = document.new_page(width=500, height=700)
            english_page.insert_textbox(
                fitz.Rect(72, 80, 430, 220),
                "this English translation begins on the next page and must remain separate from the French "
                "original even though its geometry resembles a prose continuation.",
                fontsize=11,
            )
            document.save(path)
            document.close()

            structured = build_source_document(path, pdf_config=PDFIngestionConfig(ocr_mode="off"))
            english_text = next(
                block for block in structured.blocks if block.text.startswith("this English translation")
            )
            self.assertNotIn("continuation_from_block_id", english_text.attributes)

    def test_page_continuation_skips_a_short_running_header(self):
        base = {
            "page_size": [500, 700],
            "font_size": 11,
            "source_method": "native",
            "reading_order": "top_to_bottom",
            "direction": [1.0, 0.0],
            "role_evidence": {},
        }
        previous = SourceBlock(
            "pdf:1:1",
            "This deliberately long sentence reaches the physical page boundary without ending its thought",
            1,
            1,
            page=1,
            attributes={
                **base,
                "bbox": [72, 590, 430, 630],
                "source_lines": 3,
            },
        )
        header = SourceBlock(
            "pdf:2:1",
            "x Book Title",
            2,
            2,
            page=2,
            attributes={
                **base,
                "bbox": [72, 35, 180, 48],
                "source_lines": 1,
            },
        )
        continuation = SourceBlock(
            "pdf:2:2",
            "and continues as ordinary prose below the running header with enough words to be narrative.",
            3,
            3,
            page=2,
            attributes={
                **base,
                "bbox": [72, 80, 430, 150],
                "source_lines": 5,
            },
        )
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="running-header.pdf",
            filename="running-header.pdf",
            blocks=[previous, header, continuation],
        )

        _annotate_page_continuations(document)

        self.assertNotIn("continuation_from_block_id", header.attributes)
        self.assertEqual(
            continuation.attributes["continuation_from_block_id"], previous.block_id
        )

    def test_page_continuation_does_not_cross_an_acknowledgments_heading(self):
        base = {
            "page_size": [500, 700],
            "font_size": 11,
            "source_method": "native",
            "reading_order": "top_to_bottom",
            "direction": [1.0, 0.0],
            "role_evidence": {},
        }
        previous = SourceBlock(
            "pdf:1:1",
            "The previous section reaches the physical page boundary without terminal punctuation",
            1,
            1,
            page=1,
            attributes={**base, "bbox": [72, 590, 430, 630], "source_lines": 3},
        )
        heading = SourceBlock(
            "pdf:2:1",
            "x Acknowledgments",
            2,
            2,
            page=2,
            attributes={**base, "bbox": [72, 35, 180, 48], "source_lines": 2},
        )
        body = SourceBlock(
            "pdf:2:2",
            "acknowledgment prose begins here and belongs to a new section of the book.",
            3,
            3,
            page=2,
            attributes={**base, "bbox": [72, 80, 430, 150], "source_lines": 5},
        )
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="acknowledgments.pdf",
            filename="acknowledgments.pdf",
            blocks=[previous, heading, body],
        )

        _annotate_structural_roles(document)
        _annotate_page_continuations(document)

        self.assertLess(heading.role_score("non_narrative_section"), 0.85)
        self.assertNotIn("continuation_from_block_id", body.attributes)

    def test_page_continuation_rejects_a_german_to_english_language_switch(self):
        base = {
            "page_size": [500, 700],
            "font_size": 11,
            "source_lines": 6,
            "source_method": "native",
            "reading_order": "top_to_bottom",
            "direction": [1.0, 0.0],
            "role_evidence": {},
        }
        previous = SourceBlock(
            "pdf:1:1",
            "Der Gedanke ist in dem deutschen Text und wird mit einer langen Erklärung fortgeführt",
            1,
            1,
            page=1,
            attributes={**base, "bbox": [72, 560, 430, 630]},
        )
        translation = SourceBlock(
            "pdf:2:1",
            "in the English translation the argument begins again and must remain a separate paragraph",
            2,
            2,
            page=2,
            attributes={**base, "bbox": [72, 80, 430, 150]},
        )
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="translation.pdf",
            filename="translation.pdf",
            blocks=[previous, translation],
        )

        _annotate_page_continuations(document)

        self.assertNotIn("continuation_from_block_id", translation.attributes)

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

    def test_word_fragmented_native_rows_are_not_mistaken_for_columns(self):
        words = [
            ["Theorists", "and", "practitioners", "commonly", "assume", "that"],
            ["rights", "remain", "part", "of", "one", "paragraph"],
            ["although", "the", "native", "layer", "split", "each"],
            ["word", "into", "a", "separate", "line", "record."],
        ]
        lines = []
        for row_index, row in enumerate(words):
            x = 80.0
            y = 100.0 + row_index * 14.0
            for word in row:
                width = max(12.0, len(word) * 5.5)
                lines.append(
                    {
                        "text": word,
                        "bbox": [x, y, x + width, y + 10.0],
                        "block_index": 5,
                        "direction": [1.0, 0.0],
                        "font_size": 10.0,
                        "font": "Fixture",
                        "confidence": None,
                    }
                )
                x += width + 8.0

        blocks = _lines_to_blocks(lines, fitz.Rect(0, 0, 468, 700), "native")

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["reading_order"], "top_to_bottom")
        self.assertIn(
            "Theorists and practitioners commonly assume that rights remain",
            blocks[0]["text"],
        )


    def test_two_columns_can_reach_slightly_into_the_nominal_gutter(self):
        lines = []
        for row_index in range(5):
            y = 100.0 + row_index * 14.0
            lines.extend(
                [
                    {
                        "text": f"Left line {row_index} continues near center",
                        "bbox": [40.0, y, 304.0, y + 10.0],
                        "block_index": 1,
                        "direction": [1.0, 0.0],
                        "font_size": 10.0,
                        "font": "Fixture",
                        "confidence": None,
                    },
                    {
                        "text": f"Right line {row_index} starts near center",
                        "bbox": [307.0, y, 560.0, y + 10.0],
                        "block_index": 2,
                        "direction": [1.0, 0.0],
                        "font_size": 10.0,
                        "font": "Fixture",
                        "confidence": None,
                    },
                ]
            )

        blocks = _lines_to_blocks(lines, fitz.Rect(0, 0, 600, 700), "native")
        text = "\n".join(block["text"] for block in blocks)

        self.assertTrue(blocks)
        self.assertTrue(
            all(block["reading_order"] == "two_columns" for block in blocks)
        )
        self.assertLess(text.index("Left line 4"), text.index("Right line 0"))

    def test_multi_column_order_supports_more_than_two_text_lanes(self):
        lines = [
            {
                "text": "Notes",
                "bbox": [40.0, 20.0, 560.0, 34.0],
                "block_index": 0,
                "direction": [1.0, 0.0],
                "font_size": 14.0,
            }
        ]
        for column, (x0, x1) in enumerate(
            ((40.0, 170.0), (225.0, 355.0), (410.0, 540.0)), start=1
        ):
            for row in range(4):
                lines.append(
                    {
                        "text": f"Column {column} line {row}",
                        "bbox": [x0, 60.0 + row * 14.0, x1, 70.0 + row * 14.0],
                        "block_index": column,
                        "direction": [1.0, 0.0],
                        "font_size": 10.0,
                    }
                )

        ordered, reading_order = _geometry_order(lines, 600.0)
        texts = [line["text"] for line in ordered]

        self.assertEqual(reading_order, "multi_columns")
        self.assertEqual(texts[0], "Notes")
        self.assertLess(texts.index("Column 1 line 3"), texts.index("Column 2 line 0"))
        self.assertLess(texts.index("Column 2 line 3"), texts.index("Column 3 line 0"))

    def test_spanning_heading_splits_two_column_reading_regions(self):
        lines = []
        for region, top in (("upper", 40.0), ("lower", 140.0)):
            for column, (x0, x1) in enumerate(((40.0, 275.0), (325.0, 560.0)), 1):
                for row in range(4):
                    lines.append(
                        {
                            "text": f"{region} column {column} line {row}",
                            "bbox": [
                                x0,
                                top + row * 12.0,
                                x1,
                                top + row * 12.0 + 9.0,
                            ],
                            "block_index": column + (10 if region == "lower" else 0),
                            "direction": [1.0, 0.0],
                            "font_size": 10.0,
                        }
                    )
        lines.append(
            {
                "text": "A Full Width Section",
                "bbox": [50.0, 105.0, 550.0, 122.0],
                "block_index": 20,
                "direction": [1.0, 0.0],
                "font_size": 16.0,
            }
        )

        ordered, reading_order = _geometry_order(lines, 600.0)
        texts = [line["text"] for line in ordered]
        heading_index = texts.index("A Full Width Section")

        self.assertEqual(reading_order, "two_columns")
        self.assertLess(texts.index("upper column 2 line 3"), heading_index)
        self.assertLess(heading_index, texts.index("lower column 1 line 0"))

    def test_layout_continuations_are_explicit_and_do_not_cross_footnotes(self):
        base = {
            "page_size": [500, 700],
            "font_size": 11,
            "source_lines": 2,
            "source_method": "native",
            "reading_order": "top_to_bottom",
            "direction": [1.0, 0.0],
            "role_evidence": {},
        }
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="fixture.pdf",
            filename="fixture.pdf",
            blocks=[
                SourceBlock(
                    "pdf:1:1",
                    "This deliberately long sentence reaches the end of its visual line",
                    1,
                    1,
                    source_index=1,
                    page=1,
                    attributes={
                        **base,
                        "bbox": [72, 100, 430, 135],
                        "last_line_bbox": [72, 120, 430, 135],
                        "native_block_indexes": [4],
                    },
                ),
                SourceBlock(
                    "pdf:1:2",
                    "Quoted continuation remains part of that sentence",
                    2,
                    2,
                    source_index=2,
                    page=1,
                    attributes={
                        **base,
                        "bbox": [72, 136, 430, 170],
                        "first_line_bbox": [72, 136, 430, 150],
                        "native_block_indexes": [4],
                    },
                ),
                SourceBlock(
                    "pdf:1:3",
                    "1 A source note that must remain a separator.",
                    3,
                    3,
                    source_index=3,
                    page=1,
                    role_candidates=["footnote"],
                    attributes={
                        **base,
                        "bbox": [72, 180, 430, 200],
                        "first_line_bbox": [72, 180, 430, 200],
                        "last_line_bbox": [72, 180, 430, 200],
                        "native_block_indexes": [5],
                        "role_evidence": {"footnote": {"score": 0.9}},
                    },
                ),
                SourceBlock(
                    "pdf:1:4",
                    "lowercase prose after the note starts a separate block.",
                    4,
                    4,
                    source_index=4,
                    page=1,
                    attributes={
                        **base,
                        "bbox": [72, 201, 430, 230],
                        "first_line_bbox": [72, 201, 430, 215],
                        "native_block_indexes": [6],
                    },
                ),
            ],
        )

        _annotate_layout_continuations(document)

        self.assertEqual(
            document.blocks[1].attributes["layout_continuation_from_block_id"],
            "pdf:1:1",
        )
        self.assertNotIn(
            "layout_continuation_from_block_id", document.blocks[3].attributes
        )

    def test_layout_continuations_preserve_clear_paragraph_and_verse_boundaries(self):
        base = {
            "page_size": [500, 700],
            "font_size": 11,
            "source_method": "native",
            "reading_order": "top_to_bottom",
            "direction": [1.0, 0.0],
            "role_evidence": {},
        }

        def pair(
            left: str,
            right: str,
            *,
            current_first_x: float = 72,
            left_lines: int = 3,
            right_lines: int = 3,
        ) -> SourceDocument:
            return SourceDocument(
                source_type="pdf_structured",
                source_path="boundaries.pdf",
                filename="boundaries.pdf",
                blocks=[
                    SourceBlock(
                        "pdf:1:1",
                        left,
                        1,
                        1,
                        page=1,
                        attributes={
                            **base,
                            "bbox": [72, 100, 430, 145],
                            "last_line_bbox": [72, 130, 430, 145],
                            "native_block_indexes": [1],
                            "source_lines": left_lines,
                        },
                    ),
                    SourceBlock(
                        "pdf:1:2",
                        right,
                        2,
                        2,
                        page=1,
                        attributes={
                            **base,
                            "bbox": [72, 144, 430, 210],
                            "first_line_bbox": [current_first_x, 144, 430, 159],
                            "native_block_indexes": [2],
                            "source_lines": right_lines,
                        },
                    ),
                ],
            )

        fixtures = [
            pair(
                "The preceding paragraph happens to end without terminal punctuation",
                "the indented next paragraph must remain separate despite starting lowercase",
                current_first_x=84,
            ),
            pair(
                "My love for you",
                "has driven me insane",
                left_lines=2,
                right_lines=1,
            ),
            pair(
                "Institute of Physics and Cosmology",
                "author@example.org",
                right_lines=1,
            ),
            pair(
                "KOREA IN GLOBAL PERSPECTIVE",
                "modern readers begin a separate body paragraph here",
                left_lines=1,
            ),
        ]

        for document in fixtures:
            _annotate_layout_continuations(document)
            self.assertNotIn(
                "layout_continuation_from_block_id", document.blocks[1].attributes
            )

    def test_dense_short_prose_needs_repeated_columns_to_be_tabular(self):
        def document(left_positions: list[float]) -> SourceDocument:
            blocks = []
            for index, x0 in enumerate(left_positions, start=1):
                blocks.append(
                    SourceBlock(
                        f"pdf:1:{index}",
                        f"short prose fragment {index}",
                        index,
                        index,
                        page=1,
                        attributes={
                            "bbox": [x0, 50 + index * 20, x0 + 160, 64 + index * 20],
                            "page_size": [500, 700],
                            "font_size": 10,
                            "source_lines": 1,
                            "source_method": "native",
                            "reading_order": "top_to_bottom",
                            "direction": [1.0, 0.0],
                            "role_evidence": {},
                        },
                    )
                )
            return SourceDocument(
                source_type="pdf_structured",
                source_path="records.pdf",
                filename="records.pdf",
                blocks=blocks,
            )

        prose = document([72] * 18)
        table = document([72 if index % 2 else 280 for index in range(18)])

        _annotate_structural_roles(prose)
        _annotate_structural_roles(table)

        self.assertTrue(
            all(block.role_score("tabular_region") < 0.90 for block in prose.blocks)
        )
        self.assertTrue(
            all(block.role_score("tabular_region") >= 0.90 for block in table.blocks)
        )

    def test_tabular_rows_are_not_annotated_as_cross_block_continuations(self):
        attributes = {
            "page_size": [500, 700],
            "font_size": 10,
            "source_lines": 1,
            "source_method": "native",
            "reading_order": "two_columns",
            "direction": [1.0, 0.0],
            "role_evidence": {"tabular_region": {"score": 0.9}},
        }
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="table.pdf",
            filename="table.pdf",
            blocks=[
                SourceBlock(
                    "pdf:1:1",
                    "par about and several other words in this dictionary entry",
                    1,
                    1,
                    source_index=1,
                    page=1,
                    attributes={
                        **attributes,
                        "bbox": [72, 100, 250, 120],
                        "last_line_bbox": [72, 100, 250, 120],
                        "native_block_indexes": [1],
                    },
                ),
                SourceBlock(
                    "pdf:1:2",
                    "as a separate dictionary lemma",
                    2,
                    2,
                    source_index=2,
                    page=1,
                    attributes={
                        **attributes,
                        "bbox": [72, 121, 250, 140],
                        "first_line_bbox": [72, 121, 250, 140],
                        "native_block_indexes": [1],
                    },
                ),
            ],
        )

        _annotate_layout_continuations(document)

        self.assertNotIn(
            "layout_continuation_from_block_id", document.blocks[1].attributes
        )


    def test_centered_multiline_heading_across_native_blocks_stays_one_block(self):
        lines = [
            {
                "text": "4",
                "bbox": [250, 70, 270, 92],
                "block_index": 0,
                "direction": [1.0, 0.0],
                "font_size": 22.0,
                "font": "Fixture",
                "confidence": None,
            },
            {
                "text": "Understanding a Command",
                "bbox": [125, 96, 395, 118],
                "block_index": 1,
                "direction": [1.0, 0.0],
                "font_size": 22.0,
                "font": "Fixture",
                "confidence": None,
            },
            {
                "text": "the Condition for Our Being Able to Obey It.",
                "bbox": [80, 122, 440, 144],
                "block_index": 2,
                "direction": [1.0, 0.0],
                "font_size": 22.0,
                "font": "Fixture",
                "confidence": None,
            },
        ]

        blocks = _lines_to_blocks(lines, fitz.Rect(0, 0, 520, 700), "native")

        self.assertEqual(len(blocks), 1)
        self.assertEqual(
            blocks[0]["text"],
            "4 Understanding a Command the Condition for Our Being Able to Obey It.",
        )


    def test_rotated_edge_text_is_kept_separate_and_removed_as_a_marginal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rotated-margin.pdf")
            document = fitz.open()
            page = document.new_page(width=500, height=700)
            page.insert_textbox(
                fitz.Rect(72, 120, 430, 300),
                "Ordinary body narration remains intact and must never absorb a vertical download stamp.",
                fontsize=11,
            )
            page.insert_text(
                (10, 560), "Downloaded by Example University", fontsize=9, rotate=90
            )
            document.save(path)
            document.close()

            structured = build_source_document(
                path, pdf_config=PDFIngestionConfig(ocr_mode="off")
            )
            rotated = next(
                block
                for block in structured.blocks
                if block.text.startswith("Downloaded by")
            )
            cleaned = apply_cleaning_operations(
                structured, propose_deterministic_operations(structured)
            )

            self.assertGreaterEqual(rotated.role_score("repeated_marginal"), 0.95)
            self.assertNotIn("Downloaded by Example University", cleaned.cleaned_text)
            self.assertIn(
                "Ordinary body narration remains intact", cleaned.cleaned_text
            )

    def test_short_horizontal_rule_marks_the_lower_footnote_area(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ruled-footnote.pdf")
            document = fitz.open()
            page = document.new_page(width=500, height=700)
            page.insert_textbox(
                fitz.Rect(72, 280, 430, 380),
                "The main paragraph ends above the conventional footnote rule",
                fontsize=11,
            )
            page.draw_line((72, 400), (140, 400), width=0.5)
            page.insert_textbox(
                fitz.Rect(72, 410, 430, 520),
                "published by his followers. This is source-note material, not a continuation of the body.",
                fontsize=9,
            )
            document.save(path)
            document.close()

            structured = build_source_document(
                path, pdf_config=PDFIngestionConfig(ocr_mode="off")
            )
            note = next(
                block
                for block in structured.blocks
                if block.text.startswith("published by his followers")
            )

            self.assertGreaterEqual(note.role_score("footnote"), 0.90)
            self.assertNotIn(
                "layout_continuation_from_block_id", note.attributes
            )

    def test_page_rotation_is_applied_to_native_body_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rotated-page.pdf")
            document = fitz.open()
            page = document.new_page(width=447, height=286)
            page.insert_text(
                (200, 250),
                "Rotated metadata body remains narrative.",
                fontsize=9,
                rotate=90,
            )
            page.set_rotation(90)
            document.save(path)
            document.close()

            structured = build_source_document(
                path, pdf_config=PDFIngestionConfig(ocr_mode="off")
            )
            body = next(
                block
                for block in structured.blocks
                if block.text.startswith("Rotated metadata")
            )
            cleaned = apply_cleaning_operations(
                structured, propose_deterministic_operations(structured)
            )

            self.assertEqual(body.attributes["direction"], [1.0, 0.0])
            self.assertLess(body.role_score("repeated_marginal"), 0.95)
            self.assertIn("metadata body remains narrative", cleaned.cleaned_text)

    def test_page_rotation_is_applied_before_classifying_horizontal_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rotated-rule.pdf")
            document = fitz.open()
            page = document.new_page(width=447, height=286)
            page.insert_text((72, 80), "Ordinary body text.", fontsize=11)
            page.draw_line((72, 180), (140, 180), width=0.5)
            page.set_rotation(90)
            document.save(path)
            document.close()

            structured = build_source_document(
                path, pdf_config=PDFIngestionConfig(ocr_mode="off")
            )
            page_record = structured.attributes["pdf_ingestion"]["pages"][0]

            self.assertEqual(page_record["horizontal_rules"], [])

    def test_short_table_entries_are_not_cross_page_prose_continuations(self):
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="table.pdf",
            filename="table.pdf",
            blocks=[
                SourceBlock(
                    "pdf:1:1",
                    "they are (non-human)",
                    1,
                    1,
                    page=1,
                    attributes={
                        "bbox": [72, 600, 260, 615],
                        "page_size": [500, 700],
                        "font_size": 10,
                        "source_lines": 1,
                        "source_method": "native",
                        "reading_order": "top_to_bottom",
                    },
                ),
                SourceBlock(
                    "pdf:2:1",
                    "forms of address",
                    2,
                    2,
                    page=2,
                    attributes={
                        "bbox": [72, 80, 240, 95],
                        "page_size": [500, 700],
                        "font_size": 10,
                        "source_lines": 1,
                        "source_method": "native",
                        "reading_order": "top_to_bottom",
                    },
                ),
            ],
        )

        _annotate_page_continuations(document)

        self.assertNotIn("continuation_from_block_id", document.blocks[1].attributes)


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

    def test_multiline_large_heading_creates_one_chapter_marker(self):
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="multiline-heading.pdf",
            filename="multiline-heading.pdf",
        )
        fixtures = [
            ("Understanding a Command", [120, 90, 390, 112], 22),
            ("15", [50, 105, 60, 115], 10),
            ("the Condition for Our Being", [125, 114, 385, 136], 22),
            ("Able to Obey It.", [180, 138, 330, 160], 22),
            (
                (
                    "This deliberately long body paragraph establishes ordinary "
                    "typography below the heading and continues with enough narrative "
                    "text to make the page's content-opening structure unambiguous."
                ),
                [72, 190, 430, 300],
                10,
            ),
        ]
        for index, (text, bbox, font_size) in enumerate(fixtures, start=1):
            document.blocks.append(
                SourceBlock(
                    block_id=f"heading:{index}",
                    text=text,
                    line_start=index,
                    line_end=index,
                    source_index=index,
                    page=1,
                    tag="p",
                    attributes={
                        "bbox": bbox,
                        "page_size": [500, 700],
                        "font_size": font_size,
                        "source_lines": 10 if font_size == 10 and len(text) > 40 else 1,
                        "role_evidence": {},
                    },
                )
            )

        _annotate_structural_roles(document)
        marked = [
            block
            for block in document.blocks
            if block.role_score("deterministic_chapter") >= 0.85
        ]

        self.assertEqual([block.text for block in marked], ["Understanding a Command"])


    def test_spaced_ocr_folio_is_page_number_not_chapter(self):
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="ocr-folio.pdf",
            filename="ocr-folio.pdf",
        )
        for index, (text, y, font_size) in enumerate(
            [
                ("I 86", 12, 10.3),
                ("Ordinary body narration establishes the document typography.", 80, 8.2),
                ("More ordinary body narration follows on the same page.", 130, 8.2),
                ("The page continues with enough prose to establish the body font.", 180, 8.2),
            ],
            start=1,
        ):
            document.blocks.append(
                SourceBlock(
                    block_id=f"folio:{index}",
                    text=text,
                    line_start=index,
                    line_end=index,
                    source_index=index,
                    page=1,
                    tag="p",
                    attributes={
                        "bbox": [15, y, 430, y + 12],
                        "page_size": [500, 700],
                        "font_size": font_size,
                        "source_lines": 1,
                        "role_evidence": {},
                    },
                )
            )

        _annotate_structural_roles(document)
        folio = document.blocks[0]

        self.assertGreaterEqual(folio.role_score("page_number"), 0.98)
        self.assertLess(folio.role_score("deterministic_chapter"), 0.85)

    def test_small_top_act_heading_is_a_boundary_but_cast_name_is_not(self):
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="play.pdf",
            filename="play.pdf",
        )
        for index, (text, y, font_size) in enumerate(
            [
                ("ACT TWO", 56, 9.1),
                ("The scene opens with ordinary dialogue and stage directions.", 82, 8.3),
                ("ANDREW PROZOROV", 156, 10.3),
                ("The character enters and begins an ordinary line of dialogue.", 180, 8.3),
            ],
            start=1,
        ):
            document.blocks.append(
                SourceBlock(
                    block_id=f"play:{index}",
                    text=text,
                    line_start=index,
                    line_end=index,
                    source_index=index,
                    page=1,
                    tag="p",
                    attributes={
                        "bbox": [72, y, 430, y + 12],
                        "page_size": [500, 700],
                        "font_size": font_size,
                        "source_lines": 1,
                        "role_evidence": {},
                    },
                )
            )

        _annotate_structural_roles(document)
        by_text = {block.text: block for block in document.blocks}

        self.assertGreaterEqual(by_text["ACT TWO"].role_score("deterministic_chapter"), 0.85)
        self.assertLess(by_text["ACT TWO"].role_score("running_header"), 0.95)
        self.assertLess(by_text["ANDREW PROZOROV"].role_score("deterministic_chapter"), 0.85)
        analysis = SourceCleaningTools(document).analyze_chapter_structure()
        self.assertEqual(
            [item["text"] for item in analysis["likely_chapters"]],
            ["ACT TWO"],
        )

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
            noisy_page.insert_text((72, 80), "13 INTRODUCTION", fontsize=8)
            noisy_page.insert_text((72, 130), "I was thinking about ordinary narration.", fontsize=11)
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

            works_page = document.new_page(width=500, height=700)
            works_page.insert_text((72, 90), "Works by Chekhov", fontsize=18)
            works_page.insert_textbox(
                fitz.Rect(72, 140, 430, 280),
                "A publication list follows this non-narrative heading and is deliberately long enough "
                "to look like ordinary page content to the geometry-aware extractor.",
                fontsize=11,
            )

            fragment_page = document.new_page(width=500, height=700)
            fragment_page.insert_text((72, 90), "in Public Address", fontsize=18)
            fragment_page.insert_textbox(
                fitz.Rect(72, 140, 430, 280),
                "A lowercase continuation split from the preceding title must not become a second "
                "chapter boundary merely because the PDF gives it title-sized typography.",
                fontsize=11,
            )

            document.save(path)
            document.close()

            structured = build_source_document(path, pdf_config=PDFIngestionConfig(ocr_mode="off"))
            by_text = {block.text: block for block in structured.blocks}

            self.assertLess(by_text["EXAMPLE ANTHOLOGY"].role_score("deterministic_chapter"), 0.85)
            self.assertGreaterEqual(by_text["Chapter 1"].role_score("deterministic_chapter"), 0.85)
            self.assertLess(by_text["Chapter 1"].role_score("toc_candidate"), 0.85)
            self.assertGreaterEqual(by_text["A Short Story"].role_score("deterministic_chapter"), 0.85)
            self.assertLess(by_text["I was thinking about ordinary narration."].role_score("heading"), 0.45)
            self.assertLess(by_text["1 30"].role_score("heading"), 0.45)
            self.assertGreaterEqual(by_text["13 INTRODUCTION"].role_score("running_header"), 0.98)
            self.assertGreaterEqual(by_text["Select Bibliography"].role_score("non_narrative_section"), 0.85)
            self.assertLess(by_text["Select Bibliography"].role_score("deterministic_chapter"), 0.85)
            self.assertGreaterEqual(by_text["List of Abbreviations"].role_score("non_narrative_section"), 0.85)
            self.assertLess(by_text["List of Abbreviations"].role_score("deterministic_chapter"), 0.85)
            self.assertGreaterEqual(by_text["Works by Chekhov"].role_score("non_narrative_section"), 0.85)
            self.assertLess(by_text["Works by Chekhov"].role_score("deterministic_chapter"), 0.85)
            self.assertLess(by_text["in Public Address"].role_score("deterministic_chapter"), 0.85)

            likely_titles = {
                item["text"]
                for item in SourceCleaningTools(structured).analyze_chapter_structure()[
                    "likely_chapters"
                ]
            }
            self.assertIn("Chapter 1", likely_titles)
            self.assertIn("A Short Story", likely_titles)

            deleted = {
                block_id
                for operation in propose_deterministic_operations(structured)
                if operation["op"] == "delete_blocks"
                for block_id in operation["block_ids"]
            }
            self.assertIn(by_text["13 INTRODUCTION"].block_id, deleted)

    def test_front_toc_entry_with_dot_leader_is_not_a_chapter_boundary(self):
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="toc-entry.pdf",
            filename="toc-entry.pdf",
        )
        for index, (text, y, font_size) in enumerate(
            [
                ("Section V ....134", 90, 16),
                ("A dot-leader entry must not become a narration boundary.", 140, 11),
                ("Ordinary body-sized text establishes the document typography.", 190, 11),
            ],
            start=1,
        ):
            document.blocks.append(
                SourceBlock(
                    block_id=f"toc-entry:{index}",
                    text=text,
                    line_start=index,
                    line_end=index,
                    source_index=index,
                    page=1,
                    tag="p",
                    attributes={
                        "bbox": [72, y, 430, y + 18],
                        "page_size": [500, 700],
                        "font_size": font_size,
                        "source_lines": 1,
                        "role_evidence": {},
                    },
                )
            )

        _annotate_structural_roles(document)
        toc_entry = document.blocks[0]

        self.assertGreaterEqual(toc_entry.role_score("toc_candidate"), 0.7)
        self.assertLess(toc_entry.role_score("deterministic_chapter"), 0.85)

    def test_front_prose_mentioning_table_of_contents_is_not_toc(self):
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="preface.pdf",
            filename="preface.pdf",
        )
        texts = [
            "This edition includes a comprehensive table of contents, a glossary, and notes for readers.",
            "The preface continues with ordinary narrative prose that must remain available for narration.",
        ]
        for index, text in enumerate(texts, start=1):
            document.blocks.append(
                SourceBlock(
                    block_id=f"preface:{index}",
                    text=text,
                    line_start=index,
                    line_end=index,
                    source_index=index,
                    page=2,
                    tag="p",
                    attributes={
                        "bbox": [72, 80 + index * 60, 430, 110 + index * 60],
                        "page_size": [500, 700],
                        "font_size": 11,
                        "source_lines": 3,
                        "role_evidence": {},
                    },
                )
            )

        _annotate_structural_roles(document)

        self.assertTrue(
            all(block.role_score("toc") < 0.92 for block in document.blocks)
        )
        self.assertTrue(
            all(block.role_score("toc_candidate") < 0.70 for block in document.blocks)
        )


    def test_pdf_chapter_references_in_prose_are_not_chapter_boundaries(self):
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="references.pdf",
            filename="references.pdf",
        )
        texts = [
            "Ordinary body narration establishes the document's normal typography and reading flow.",
            "Chapter 4 shifts its perspective by focusing on an important conflict.",
            "Book 1 results from the fact that the editor included this passage in the final text.",
            "Further ordinary narration follows the references and continues the same discussion at length.",
            "Chapter 9: Inferences are things that have happened in the cited discussion.",
        ]
        for index, text in enumerate(texts, start=1):
            document.blocks.append(
                SourceBlock(
                    block_id=f"reference:{index}",
                    text=text,
                    line_start=index,
                    line_end=index,
                    source_index=index,
                    page=1,
                    tag="p",
                    attributes={
                        "bbox": [72, 180 + index * 70, 430, 205 + index * 70],
                        "page_size": [500, 700],
                        "font_size": 8 if index == 5 else 11,
                        "source_lines": 1,
                        "role_evidence": {},
                    },
                )
            )

        _annotate_structural_roles(document)

        for block in document.blocks[1:3]:
            self.assertLess(block.role_score("deterministic_chapter"), 0.85)
        footnote_reference = document.blocks[4]
        self.assertGreaterEqual(footnote_reference.role_score("footnote"), 0.6)
        self.assertLess(footnote_reference.role_score("deterministic_chapter"), 0.85)

    def test_repeated_small_chapter_labels_keep_first_boundary_only(self):
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="running-headers.pdf",
            filename="running-headers.pdf",
        )

        def add_block(page, suffix, text, y, font_size):
            index = len(document.blocks) + 1
            document.blocks.append(
                SourceBlock(
                    block_id=f"page-{page}:{suffix}",
                    text=text,
                    line_start=index,
                    line_end=index,
                    source_index=index,
                    page=page,
                    tag="p",
                    attributes={
                        "bbox": [72, y, 430, y + 20],
                        "page_size": [500, 700],
                        "font_size": font_size,
                        "source_lines": 1,
                        "role_evidence": {},
                    },
                )
            )

        add_block(1, "title", "Chapter 1", 70, 18)
        add_block(
            1,
            "body",
            "The actual chapter begins with substantial ordinary narration that establishes the body font.",
            130,
            11,
        )
        for page in range(2, 5):
            add_block(page, "header", "Chapter 18 \x08", 40, 11)
            add_block(
                page,
                "body",
                "Narration continues on this page with enough prose to represent an ordinary book page.",
                100,
                11,
            )
        add_block(20, "header", "Chapter 18 \x08", 40, 11)
        add_block(
            20,
            "body",
            "A later part restarts chapter numbering and must retain its first boundary.",
            100,
            11,
        )

        _annotate_structural_roles(document)
        actual_title = next(block for block in document.blocks if block.text == "Chapter 1")
        repeated_headers = [block for block in document.blocks if block.text.startswith("Chapter 18")]

        self.assertGreaterEqual(actual_title.role_score("deterministic_chapter"), 0.85)
        self.assertLess(actual_title.role_score("repeated_marginal"), 0.95)
        self.assertGreaterEqual(repeated_headers[0].role_score("deterministic_chapter"), 0.85)
        self.assertLess(repeated_headers[0].role_score("repeated_marginal"), 0.95)
        self.assertTrue(
            all(block.role_score("repeated_marginal") >= 0.95 for block in repeated_headers[1:3])
        )
        self.assertTrue(
            all(block.role_score("deterministic_chapter") < 0.85 for block in repeated_headers[1:3])
        )
        self.assertGreaterEqual(repeated_headers[3].role_score("deterministic_chapter"), 0.85)
        self.assertLess(repeated_headers[3].role_score("repeated_marginal"), 0.95)

    def test_chapter_outline_entries_are_not_chapter_boundaries(self):
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="outline.pdf",
            filename="outline.pdf",
        )
        for index, text in enumerate(
            [
                "Chapter 1: The Beginning",
                "Chapter 2: The Middle",
                "Chapter 3: The End",
                "The chapter-by-chapter outline explains the organization of the complete work.",
            ],
            start=1,
        ):
            document.blocks.append(
                SourceBlock(
                    block_id=f"outline:{index}",
                    text=text,
                    line_start=index,
                    line_end=index,
                    source_index=index,
                    page=1,
                    tag="p",
                    attributes={
                        "bbox": [72, 80 + index * 50, 430, 100 + index * 50],
                        "page_size": [500, 700],
                        "font_size": 11,
                        "source_lines": 1,
                        "role_evidence": {},
                    },
                )
            )

        _annotate_structural_roles(document)
        outline_entries = document.blocks[:3]

        self.assertTrue(
            all(block.role_score("chapter_outline") >= 0.95 for block in outline_entries)
        )
        self.assertTrue(
            all(block.role_score("deterministic_chapter") < 0.85 for block in outline_entries)
        )

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
