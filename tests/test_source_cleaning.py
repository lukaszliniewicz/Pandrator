import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ebooklib import epub

from pandrator.app_logic import AppLogic
from pandrator.logic import llm_handler, session_handler
from pandrator.logic.source_cleaning import (
    PHASE_ORDER,
    SourceCleaningAgentConfig,
    SourceCleaningAgentResult,
    SourceCleaningPipelineConfig,
    SourceCleaningTools,
    SourceBlock,
    SourceDocument,
    apply_cleaning_operations,
    build_source_document,
    resolve_phase_max_iterations,
    run_cleaning_pipeline,
    run_source_cleaning_agent,
    validate_cleaning_result,
)
from pandrator.logic.source_cleaning.agent import parse_json_command
from pandrator.logic.source_cleaning.pdf_text_adapter import build_source_document_from_text


class SourceCleaningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _write_epub_fixture(self) -> str:
        epub_path = os.path.join(self.temp_dir.name, "Example Author - Strange Book.epub")

        book = epub.EpubBook()
        book.set_identifier("fixture-id")
        book.set_title("Strange Book")
        book.set_language("pl")
        book.add_author("Example Author")

        chapter = epub.EpubHtml(title="Rozdzial pierwszy", file_name="chapter01.xhtml", lang="pl")
        chapter.content = """
        <html>
          <body>
            <div class="x7"><span>Rozdzial pierwszy</span></div>
            <p class="bodytext">To jest pierwsze zdanie powiesci.</p>
            <figure>
              <img src="cover.jpg" alt="Mapa starego miasta" />
            </figure>
            <aside id="a12" class="zz-footish">
              1. Przypis, ktory nie powinien byc czytany.
            </aside>
          </body>
        </html>
        """

        second = epub.EpubHtml(title="Drugi rozdzial", file_name="chapter02.xhtml", lang="pl")
        second.content = """
        <html>
          <body>
            <div class="totally-random">Drugi rozdzial</div>
            <p>Dalsza czesc tekstu.</p>
          </body>
        </html>
        """

        book.add_item(chapter)
        book.add_item(second)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (
            epub.Link("chapter01.xhtml", "Rozdzial pierwszy", "chapter01"),
            epub.Link("chapter02.xhtml", "Drugi rozdzial", "chapter02"),
        )
        book.spine = ["nav", chapter, second]

        epub.write_epub(epub_path, book)
        return epub_path

    def test_pipeline_uses_exact_per_phase_turn_limits(self):
        document = build_source_document_from_text("Title\nChapter One\nNarration.", filename="sample.pdf")
        limits = {
            "metadata": 2,
            "navigation": 3,
            "boilerplate": 4,
            "repeated_elements": 5,
            "chapter_marking": 6,
        }

        with patch(
            "pandrator.logic.source_cleaning.pipeline.run_source_cleaning_agent",
            return_value=SourceCleaningAgentResult(summary="done"),
        ) as run_agent:
            result = run_cleaning_pipeline(
                document,
                config=SourceCleaningPipelineConfig(phase_max_iterations=limits),
            )

        configured_limits = [
            call.kwargs["config"].max_iterations
            for call in run_agent.call_args_list
        ]
        self.assertEqual(configured_limits, [limits[name] for name in PHASE_ORDER])
        self.assertEqual(
            [phase.max_iterations for phase in result.phases],
            [limits[name] for name in PHASE_ORDER],
        )

    def test_legacy_total_turn_limit_is_distributed_across_phases(self):
        self.assertEqual(
            resolve_phase_max_iterations(total=53),
            {
                "metadata": 4,
                "navigation": 11,
                "boilerplate": 11,
                "repeated_elements": 8,
                "chapter_marking": 19,
            },
        )

    def _write_single_file_multilingual_epub_fixture(self) -> str:
        epub_path = os.path.join(self.temp_dir.name, "Yuki Tanaka - 雨の町.epub")

        book = epub.EpubBook()
        book.set_identifier("fixture-cjk")
        book.set_title("雨の町")
        book.set_language("ja")
        book.add_author("Yuki Tanaka")

        chapter = epub.EpubHtml(title="本文", file_name="body.xhtml", lang="ja")
        chapter.content = """
        <html>
          <body>
            <div class="c1">第一章 雨の町</div>
            <div class="c2">雨は静かに屋根を叩いていた。</div>
            <div class="c1">第二章 海辺</div>
            <div class="c2">海から冷たい風が吹いてきた。</div>
            <div class="zz9">© 2026 Example Publisher</div>
          </body>
        </html>
        """

        book.add_item(chapter)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (epub.Link("body.xhtml", "本文", "body"),)
        book.spine = ["nav", chapter]

        epub.write_epub(epub_path, book)
        return epub_path

    def _write_figure_notes_epub_fixture(self) -> str:
        epub_path = os.path.join(self.temp_dir.name, "Figure Notes.epub")

        book = epub.EpubBook()
        book.set_identifier("fixture-notes")
        book.set_title("Figure Notes")
        book.set_language("en")
        book.add_author("Example Author")

        chapter = epub.EpubHtml(title="Opening", file_name="opening.xhtml", lang="en")
        chapter.content = """
        <html>
          <body>
            <h1>Opening</h1>
            <p>The story begins without ceremony.</p>
            <figure>
              <img src="map.jpg" alt="A map of the valley" />
              <figcaption>Figure 1. The valley at dawn.</figcaption>
            </figure>
            <aside id="note-a" class="opaque">
              1. This is an endnote-like aside.
            </aside>
            <p>The narrator returns to the road.</p>
          </body>
        </html>
        """

        book.add_item(chapter)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (epub.Link("opening.xhtml", "Opening", "opening"),)
        book.spine = ["nav", chapter]

        epub.write_epub(epub_path, book)
        return epub_path

    def _write_out_of_order_manifest_epub_fixture(self) -> str:
        epub_path = os.path.join(self.temp_dir.name, "Spine Order.epub")

        book = epub.EpubBook()
        book.set_identifier("fixture-spine-order")
        book.set_title("Spine Order")
        book.set_language("en")

        second = epub.EpubHtml(title="Second", file_name="second.xhtml", lang="en")
        second.content = "<html><body><p>Second in reading order.</p></body></html>"
        first = epub.EpubHtml(title="First", file_name="first.xhtml", lang="en")
        first.content = "<html><body><p>First in reading order.</p></body></html>"

        book.add_item(second)
        book.add_item(first)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav", first, second]

        epub.write_epub(epub_path, book)
        return epub_path

    def _write_large_toc_epub_fixture(self) -> str:
        epub_path = os.path.join(self.temp_dir.name, "Large Toc.epub")

        book = epub.EpubBook()
        book.set_identifier("fixture-large-toc")
        book.set_title("Large Toc")
        book.set_language("en")
        book.add_author("Example Author")

        chapter = epub.EpubHtml(title="Body", file_name="body.xhtml", lang="en")
        chapter.content = """
        <html>
          <body>
            <h1>Chapter One</h1>
            <p>The story begins here.</p>
            <h1>Chapter Two</h1>
            <p>The story continues here.</p>
          </body>
        </html>
        """

        toc_items = "\n".join(
            f'<li class="toc-line"><a href="body.xhtml#c{index}">Chapter {index}</a></li>'
            for index in range(1, 41)
        )
        toc = epub.EpubHtml(title="Contents", file_name="toc.xhtml", lang="en")
        toc.content = f"""
        <html>
          <body>
            <nav id="toc" class="pg-toc">
              <h1>Contents</h1>
              <ol>{toc_items}</ol>
            </nav>
          </body>
        </html>
        """

        book.add_item(chapter)
        book.add_item(toc)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (
            epub.Link("body.xhtml#c1", "Chapter One", "chapter-one"),
            epub.Link("body.xhtml#c2", "Chapter Two", "chapter-two"),
        )
        book.spine = ["nav", toc, chapter]

        epub.write_epub(epub_path, book)
        return epub_path

    def test_epub_index_preserves_metadata_markup_and_nonsemantic_blocks(self):
        epub_path = self._write_epub_fixture()

        document = build_source_document(epub_path)

        self.assertEqual(document.source_type, "epub")
        self.assertEqual(document.language, "pl")
        self.assertIn("title", document.metadata_candidates)
        self.assertEqual(document.metadata_candidates["title"][0]["value"], "Strange Book")
        self.assertEqual(document.metadata_candidates["author"][0]["value"], "Example Author")
        self.assertIn("Rozdzial pierwszy", document.nav_titles)
        self.assertEqual(document.navigation_entries[0]["href_path"], "chapter01.xhtml")

        block_texts = [block.text for block in document.blocks]
        self.assertIn("Rozdzial pierwszy", block_texts)
        self.assertIn("Mapa starego miasta", block_texts)

        alt_block = next(block for block in document.blocks if block.text == "Mapa starego miasta")
        self.assertIn("image_alt", alt_block.role_candidates)
        self.assertIsNotNone(alt_block.raw_markup)

        tools = SourceCleaningTools(document)
        markup = tools.get_epub_markup_for_text("Rozdzial pierwszy")
        self.assertEqual(markup["match_count"], 2)
        occurrence_markups = [
            tools.get_epub_markup_for_text("Rozdzial pierwszy", occurrence=occurrence)["target"]["raw_markup"]
            for occurrence in range(1, markup["match_count"] + 1)
        ]
        self.assertTrue(any("class=\"x7\"" in raw_markup for raw_markup in occurrence_markups))

    def test_epub_index_uses_spine_reading_order_before_manifest_order(self):
        document = build_source_document(self._write_out_of_order_manifest_epub_fixture())
        narrative_blocks = [
            block.text
            for block in document.blocks
            if "reading order" in block.text
        ]

        self.assertEqual(
            narrative_blocks,
            ["First in reading order.", "Second in reading order."],
        )

    def test_structure_and_navigation_tools_handle_nonsemantic_epub_headings(self):
        epub_path = self._write_epub_fixture()
        document = build_source_document(epub_path)
        tools = SourceCleaningTools(document)

        overview = tools.inspect_document_structure(scope={"href": "chapter01.xhtml"})
        navigation = tools.inspect_navigation()

        self.assertEqual(overview["heading_tag_count"], 0)
        self.assertTrue(any("No semantic h1-h6" in item for item in overview["diagnostics"]))
        first_entry = next(item for item in navigation["entries"] if item["title"] == "Rozdzial pierwszy")
        self.assertGreaterEqual(first_entry["match_count"], 1)
        self.assertIn(
            "normalized_title_match",
            first_entry["matches"][0]["match_reasons"],
        )

    def test_epub_index_falls_back_to_extracted_text_after_structured_parser_failure(self):
        epub_path = self._write_epub_fixture()

        with patch(
            "pandrator.logic.source_cleaning.indexer.epub_adapter.build_source_document",
            side_effect=ValueError("broken package"),
        ):
            document = build_source_document(epub_path, extracted_text="Fallback title\nFallback narration.")

        self.assertEqual(document.source_type, "epub_text_fallback")
        self.assertEqual(len(document.blocks), 2)
        self.assertTrue(any("Structured EPUB indexing failed" in item for item in document.warnings))

    def test_single_file_multilingual_epub_finds_cjk_heading_candidates(self):
        epub_path = self._write_single_file_multilingual_epub_fixture()

        document = build_source_document(epub_path)
        tools = SourceCleaningTools(document)
        headings = tools.find_heading_candidates()
        heading_texts = {item["text"] for item in headings}

        self.assertEqual(document.language, "ja")
        self.assertIn("第一章 雨の町", heading_texts)
        self.assertIn("第二章 海辺", heading_texts)

        first_chapter = next(block for block in document.blocks if block.text == "第一章 雨の町")
        second_chapter = next(block for block in document.blocks if block.text == "第二章 海辺")
        result = apply_cleaning_operations(
            document,
            [
                {"op": "set_metadata", "title": "雨の町", "author": "Yuki Tanaka", "language": "ja"},
                {"op": "mark_chapter", "block_id": first_chapter.block_id, "title": first_chapter.text},
                {"op": "mark_chapter", "block_id": second_chapter.block_id, "title": second_chapter.text},
            ],
        )

        self.assertIn("[[Chapter]]第一章 雨の町", result.cleaned_text)
        self.assertIn("[[Chapter]]第二章 海辺", result.cleaned_text)
        self.assertEqual(result.metadata["artist"], "Yuki Tanaka")

    def test_epub_fixture_supports_removing_image_alt_caption_and_note_blocks(self):
        epub_path = self._write_figure_notes_epub_fixture()

        document = build_source_document(epub_path)
        tools = SourceCleaningTools(document)
        footnotes = tools.find_footnote_candidates()
        caption_block = next(block for block in document.blocks if block.text.startswith("Figure 1."))
        alt_block = next(block for block in document.blocks if block.text == "A map of the valley")
        note_block = next(block for block in document.blocks if "endnote-like" in block.text)

        self.assertIn("caption", caption_block.role_candidates)
        self.assertIn("image_alt", alt_block.role_candidates)
        self.assertIn(note_block.block_id, {item["block_id"] for item in footnotes})

        result = apply_cleaning_operations(
            document,
            [
                {
                    "op": "delete_blocks",
                    "block_ids": [caption_block.block_id, alt_block.block_id, note_block.block_id],
                    "reason": "non-audiobook figure and note material",
                }
            ],
        )

        self.assertNotIn("A map of the valley", result.cleaned_text)
        self.assertNotIn("Figure 1.", result.cleaned_text)
        self.assertNotIn("endnote-like", result.cleaned_text)
        self.assertIn("The narrator returns to the road.", result.cleaned_text)

    def test_selector_tools_preview_and_delete_entire_epub_toc(self):
        epub_path = self._write_large_toc_epub_fixture()

        document = build_source_document(epub_path)
        tools = SourceCleaningTools(document)
        selectors = tools.list_epub_selectors(min_count=2)
        href_selectors = selectors["selectors"]["href"]
        toc_selector = {"href": "toc.xhtml"}
        toc_preview = tools.preview_selector(toc_selector, max_blocks=5, include_raw_markup=True)
        capped_preview = tools.preview_selector(toc_selector, max_blocks=40, include_raw_markup=True)
        raw_range = tools.preview_raw_markup_range(
            start_line=toc_preview["first_line"],
            end_line=toc_preview["last_line"],
            max_blocks=5,
        )

        self.assertIn(toc_selector, [item["selector"] for item in href_selectors])
        self.assertGreaterEqual(toc_preview["matched_blocks"], 40)
        self.assertTrue(any("raw_markup" in block for block in toc_preview["blocks"]))
        self.assertTrue(any("raw_markup" in block for block in raw_range["blocks"]))
        self.assertEqual(capped_preview["returned_blocks"], 12)
        self.assertEqual(capped_preview["blocks"][-1]["text"], "Chapter 40")

        result = apply_cleaning_operations(
            document,
            [
                {
                    "op": "delete_by_selector",
                    "selector": toc_selector,
                    "reason": "complete TOC document confirmed by selector preview",
                }
            ],
        )

        self.assertNotIn("Chapter 40", result.cleaned_text)
        self.assertIn("The story begins here.", result.cleaned_text)
        self.assertEqual(result.applied_operations[0]["details"]["deleted_blocks"], toc_preview["matched_blocks"])

    def test_bulk_chapter_marking_supports_selector_exclusions(self):
        epub_path = self._write_large_toc_epub_fixture()
        document = build_source_document(epub_path)
        selector = {"tag": "h1", "exclude_hrefs": ["toc.xhtml"]}

        preview = SourceCleaningTools(document).preview_selector(selector)
        result = apply_cleaning_operations(
            document,
            [
                {
                    "op": "mark_chapters_by_selector",
                    "selector": selector,
                    "reason": "body chapter headings, excluding the TOC document",
                }
            ],
        )

        self.assertEqual(preview["matched_blocks"], 2)
        self.assertEqual(result.report["chapter_count"], 2)
        self.assertIn("[[Chapter]]Chapter One", result.cleaned_text)
        self.assertIn("[[Chapter]]Chapter Two", result.cleaned_text)
        self.assertNotIn("[[Chapter]]Contents", result.cleaned_text)

    def test_chapter_structure_analysis_suggests_complete_narrative_selector(self):
        document = SourceDocument(
            source_type="epub",
            source_path="sample.epub",
            filename="sample.epub",
            nav_titles=["Chapter I", "Chapter II"],
            blocks=[
                SourceBlock(
                    block_id="toc-1",
                    text="Chapter I",
                    line_start=1,
                    line_end=1,
                    tag="p",
                    classes=["toc"],
                    role_candidates=["toc", "heading_candidate"],
                ),
                SourceBlock(
                    block_id="chapter-1",
                    text="Chapter I",
                    line_start=2,
                    line_end=2,
                    tag="h2",
                    role_candidates=["heading", "heading_candidate"],
                ),
                SourceBlock(
                    block_id="chapter-2",
                    text="Chapter II",
                    line_start=3,
                    line_end=3,
                    tag="h2",
                    role_candidates=["heading", "heading_candidate"],
                ),
            ],
        )

        analysis = SourceCleaningTools(document).analyze_chapter_structure()

        self.assertEqual(analysis["numbered_heading_count"], 2)
        self.assertEqual(analysis["selector_suggestions"][0]["matched_blocks"], 2)
        self.assertEqual(analysis["selector_suggestions"][0]["likely_chapter_matches"], 2)

    def test_cleanup_structure_finds_inline_toc_and_complete_license_document(self):
        document = SourceDocument(
            source_type="epub",
            source_path="sample.epub",
            filename="sample.epub",
            blocks=[
                SourceBlock("title", "Sample Book", 1, 1, href="body.xhtml", tag="h1", role_candidates=["heading"]),
                SourceBlock("toc-1", "Chapter I", 2, 2, href="body.xhtml", tag="p", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("toc-2", "Chapter II", 3, 3, href="body.xhtml", tag="p", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("toc-3", "Chapter III", 4, 4, href="body.xhtml", tag="p", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("toc-4", "Chapter IV", 5, 5, href="body.xhtml", tag="p", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("body", "The story begins.", 6, 6, href="body.xhtml", tag="p"),
                SourceBlock("license-1", "START: FULL LICENSE", 7, 7, href="license.xhtml", tag="div"),
                SourceBlock("license-2", "THE FULL PROJECT GUTENBERG LICENSE", 8, 8, href="license.xhtml", tag="h2"),
                SourceBlock("license-3", "Project Gutenberg terms of use", 9, 9, href="license.xhtml", tag="div"),
                SourceBlock("license-4", "Redistributing Project Gutenberg works", 10, 10, href="license.xhtml", tag="div"),
            ],
        )

        analysis = SourceCleaningTools(document).analyze_cleanup_structure()
        selectors = [item["selector"] for item in analysis["candidate_groups"]]

        self.assertIn({"class": "toc"}, selectors)
        self.assertIn({"href": "license.xhtml"}, selectors)
        self.assertNotIn({"href": "body.xhtml"}, selectors)
        self.assertEqual(analysis["toc_block_count"], 4)
        self.assertEqual(analysis["likely_boilerplate_block_count"], 4)

    def test_tools_search_preview_repeated_lines_and_footnotes(self):
        text = "\f".join(
            [
                "\n".join(
                    [
                        "Strange Book",
                        "1",
                        "Running Header",
                        "Chapter One",
                        "Story begins here.",
                        "1. A footnote on page one.",
                    ]
                ),
                "\n".join(
                    [
                        "2",
                        "Running Header",
                        "Chapter Two",
                        "Story continues here.",
                        "2. A footnote on page two.",
                    ]
                ),
                "\n".join(
                    [
                        "3",
                        "Running Header",
                        "The end.",
                    ]
                ),
            ]
        )
        document = build_source_document_from_text(text, filename="Author - Strange Book.pdf")
        tools = SourceCleaningTools(document)

        hits = tools.search("begins OR continues")
        self.assertEqual(len(hits), 2)

        preview = tools.preview(around_hit_id=hits[0]["hit_id"], before=1, after=1)
        preview_text = "\n".join(block["text"] for block in preview["blocks"])
        self.assertIn("Chapter One", preview_text)
        self.assertIn("Story begins here.", preview_text)

        repeated = tools.list_repeated_lines(min_repeats=3)
        self.assertEqual(repeated[0]["text"], "Running Header")
        self.assertEqual(repeated[0]["count"], 3)

        footnotes = tools.find_footnote_candidates()
        self.assertEqual(len(footnotes), 2)

        headings = tools.find_heading_candidates()
        heading_texts = {item["text"] for item in headings}
        self.assertIn("Chapter One", heading_texts)
        self.assertIn("Chapter Two", heading_texts)

    def test_preview_caps_large_ranges_and_returns_structural_summary(self):
        document = build_source_document_from_text(
            "\n".join(f"Line {index}" for index in range(1, 101)),
            filename="large-preview.txt",
        )

        preview = SourceCleaningTools(document).preview(
            start_line=1,
            end_line=100,
            max_blocks=10,
        )

        self.assertEqual(preview["matched_blocks"], 100)
        self.assertEqual(preview["returned_blocks"], 10)
        self.assertTrue(preview["truncated"])
        self.assertEqual(preview["blocks"][0]["line_start"], 1)
        self.assertEqual(preview["blocks"][-1]["line_start"], 100)
        self.assertEqual(preview["tag_counts"]["<none>"], 100)

    def test_pdf_fixture_detects_page_numbers_repeated_headers_and_removes_them(self):
        text = "\f".join(
            [
                "\n".join(["1", "Le Livre Etrange", "Chapitre I", "Le vent se leva."]),
                "\n".join(["2", "Le Livre Etrange", "Chapitre II", "La porte s'ouvrit."]),
                "\n".join(["3", "Le Livre Etrange", "Fin", "Tout redevint calme."]),
            ]
        )
        document = build_source_document_from_text(text, filename="Auteur - Le Livre Etrange.pdf")
        tools = SourceCleaningTools(document)
        repeated = tools.list_repeated_lines(min_repeats=3)
        page_number_blocks = [
            block
            for block in document.blocks
            if "page_number" in block.role_candidates
        ]

        self.assertEqual(repeated[0]["text"], "Le Livre Etrange")
        self.assertEqual(len(page_number_blocks), 3)

        header_blocks = repeated[0]["block_ids"]
        result = apply_cleaning_operations(
            document,
            [
                {"op": "delete_blocks", "block_ids": header_blocks, "reason": "running header"},
                {
                    "op": "delete_blocks",
                    "block_ids": [block.block_id for block in page_number_blocks],
                    "reason": "page numbers",
                },
            ],
        )

        self.assertNotIn("Le Livre Etrange", result.cleaned_text)
        self.assertNotRegex(result.cleaned_text, r"(^|\n\n)[123](\n\n|$)")
        self.assertIn("Chapitre I", result.cleaned_text)

    def test_apply_cleaning_operations_writes_artifacts_and_diff(self):
        document = build_source_document_from_text(
            "\n".join(
                [
                    "Copyright 2026 Publisher",
                    "Chapter One",
                    "Narration begins.",
                    "1. Footnote to remove.",
                    "Typoo line.",
                ]
            ),
            filename="Author - Title.pdf",
        )
        output_dir = os.path.join(self.temp_dir.name, "_source_cleaning")
        chapter_block = next(block for block in document.blocks if block.text == "Chapter One")
        typo_block = next(block for block in document.blocks if block.text == "Typoo line.")

        result = apply_cleaning_operations(
            document,
            [
                {"op": "set_metadata", "title": "Title", "author": "Author", "language": "en"},
                {"op": "delete_range", "start_line": 1, "end_line": 1, "reason": "copyright"},
                {"op": "mark_chapter", "block_id": chapter_block.block_id, "title": "Chapter One"},
                {"op": "delete_blocks", "block_ids": [document.blocks[3].block_id], "reason": "footnote"},
                {
                    "op": "replace_range",
                    "start_line": typo_block.line_start,
                    "end_line": typo_block.line_end,
                    "replacement": "Typo line.",
                    "reason": "small OCR typo",
                },
            ],
            output_dir=output_dir,
        )

        self.assertIn("[[Chapter]]Chapter One", result.cleaned_text)
        self.assertIn("Narration begins.", result.cleaned_text)
        self.assertIn("Typo line.", result.cleaned_text)
        self.assertNotIn("Copyright", result.cleaned_text)
        self.assertNotIn("Footnote", result.cleaned_text)
        self.assertEqual(result.metadata["artist"], "Author")
        self.assertIn("-Copyright 2026 Publisher", result.diff_text)
        self.assertIn("+[[Chapter]]Chapter One", result.diff_text)

        expected_files = {
            "raw_index.json",
            "raw_text.txt",
            "cleaned_text.txt",
            "cleaning_rules.json",
            "cleaning_report.json",
            "diff.patch",
        }
        self.assertTrue(expected_files.issubset(set(os.listdir(output_dir))))
        with open(os.path.join(output_dir, "cleaning_report.json"), "r", encoding="utf-8") as file_handle:
            report = json.load(file_handle)
        self.assertEqual(report["chapter_count"], 1)
        self.assertEqual(report["deleted_block_count"], 2)

    def test_large_replace_operation_is_guarded(self):
        document = build_source_document_from_text(
            "\n".join(["line 1", "line 2", "line 3", "line 4", "line 5", "line 6"]),
            filename="sample.pdf",
        )

        result = apply_cleaning_operations(
            document,
            [
                {
                    "op": "replace_range",
                    "start_line": 1,
                    "end_line": 6,
                    "replacement": "too much",
                }
            ],
            max_replace_lines=2,
        )

        self.assertEqual(len(result.applied_operations), 0)
        self.assertEqual(len(result.skipped_operations), 1)
        self.assertIn("exceeds guard", result.skipped_operations[0]["reason"])
        self.assertIn("line 6", result.cleaned_text)

    def test_agent_runs_json_tool_loop_with_fake_completion(self):
        document = build_source_document_from_text(
            "\n".join(
                [
                    "Copyright 2026 Publisher",
                    "Chapter One",
                    "Narration begins.",
                ]
            ),
            filename="Author - Title.pdf",
        )
        chapter_block = next(block for block in document.blocks if block.text == "Chapter One")
        responses = iter(
            [
                '{"action":"search","arguments":{"query":"Copyright OR Chapter","max_hits":5}}',
                (
                    "```json\n"
                    "{"
                    '"action":"finish",'
                    '"summary":"Found copyright and chapter heading.",'
                    '"confidence":0.82,'
                    '"operations":['
                    '{"op":"delete_range","start_line":1,"end_line":1,"reason":"copyright"},'
                    '{"op":"mark_chapter","block_id":"' + chapter_block.block_id + '","title":"Chapter One"}'
                    "]"
                    "}\n"
                    "```"
                ),
            ]
        )

        def fake_completion(**_kwargs):
            return next(responses)

        result = run_source_cleaning_agent(
            document,
            config=SourceCleaningAgentConfig(max_iterations=4),
            completion_func=fake_completion,
        )

        self.assertEqual(len(result.tool_trace), 1)
        self.assertEqual(result.tool_trace[0]["action"], "search")
        self.assertEqual(len(result.operations), 2)
        self.assertAlmostEqual(result.confidence, 0.82)

    def test_agent_dispatches_selector_preview_tool(self):
        document = build_source_document_from_text(
            "\n".join(["Contents", "Chapter One", "Story."]),
            filename="sample.pdf",
        )
        responses = iter(
            [
                '{"action":"preview_selector","arguments":{"selector":{"text_regex":"Chapter"},"max_blocks":5}}',
                '{"action":"finish","summary":"done","confidence":0.5,"operations":[]}',
            ]
        )

        def fake_completion(**_kwargs):
            return next(responses)

        result = run_source_cleaning_agent(
            document,
            config=SourceCleaningAgentConfig(max_iterations=3),
            completion_func=fake_completion,
        )

        self.assertEqual(result.tool_trace[0]["action"], "preview_selector")
        self.assertEqual(result.tool_trace[0]["observation"]["matched_blocks"], 1)

    def test_agent_rejects_incomplete_chapter_finish_then_accepts_bulk_selector(self):
        document = build_source_document_from_text(
            "\n".join(
                [
                    "Chapter I",
                    "Story one.",
                    "Chapter II",
                    "Story two.",
                    "Chapter III",
                    "Story three.",
                    "Chapter IV",
                    "Story four.",
                ]
            ),
            filename="sample.pdf",
        )
        first_chapter = next(block for block in document.blocks if block.text == "Chapter I")
        responses = iter(
            [
                (
                    '{"action":"finish","summary":"sampled chapters","confidence":0.5,"operations":['
                    '{"op":"mark_chapter","block_id":"' + first_chapter.block_id + '","title":"Chapter I"}'
                    "]}"
                ),
                (
                    '{"action":"finish","summary":"complete chapter pattern","confidence":0.9,"operations":['
                    '{"op":"mark_chapters_by_selector","selector":{"text_regex":"^Chapter [IV]+$"},'
                    '"reason":"all narrative chapter headings"}'
                    "]}"
                ),
            ]
        )

        result = run_source_cleaning_agent(
            document,
            config=SourceCleaningAgentConfig(max_iterations=3),
            completion_func=lambda **_kwargs: next(responses),
        )
        cleaned = apply_cleaning_operations(document, result.operations)

        self.assertEqual(len(result.finish_reviews), 2)
        self.assertIn("finish_review", [item["action"] for item in result.tool_trace])
        self.assertEqual(cleaned.report["chapter_count"], 4)

    def test_agent_rejects_finish_with_remaining_toc_and_license(self):
        document = SourceDocument(
            source_type="epub",
            source_path="sample.epub",
            filename="sample.epub",
            blocks=[
                SourceBlock("toc-1", "Chapter I", 1, 1, href="body.xhtml", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("toc-2", "Chapter II", 2, 2, href="body.xhtml", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("toc-3", "Chapter III", 3, 3, href="body.xhtml", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("toc-4", "Chapter IV", 4, 4, href="body.xhtml", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("body", "The story begins.", 5, 5, href="body.xhtml"),
                SourceBlock("license-1", "START: FULL LICENSE", 6, 6, href="license.xhtml"),
                SourceBlock("license-2", "THE FULL PROJECT GUTENBERG LICENSE", 7, 7, href="license.xhtml"),
                SourceBlock("license-3", "Project Gutenberg terms of use", 8, 8, href="license.xhtml"),
                SourceBlock("license-4", "Redistributing Project Gutenberg works", 9, 9, href="license.xhtml"),
            ],
        )
        responses = iter(
            [
                '{"action":"finish","summary":"done","confidence":0.8,"operations":[]}',
                (
                    '{"action":"finish","summary":"complete cleanup","confidence":0.9,"operations":['
                    '{"op":"delete_by_selector","selector":{"role":"toc"},"reason":"all TOC variants"},'
                    '{"op":"delete_by_selector","selector":{"href":"license.xhtml"},"reason":"complete license document"}'
                    "]}"
                ),
            ]
        )

        result = run_source_cleaning_agent(
            document,
            config=SourceCleaningAgentConfig(max_iterations=3),
            completion_func=lambda **_kwargs: next(responses),
        )
        cleaned = apply_cleaning_operations(document, result.operations)

        self.assertEqual(len(result.finish_reviews), 2)
        self.assertEqual(cleaned.cleaned_text, "The story begins.")

    def test_long_source_requires_inspection_then_auto_evaluates_finish(self):
        document = build_source_document_from_text(
            "\n".join(f"Line {index}" for index in range(1, 51)),
            filename="long-source.txt",
        )
        operations = [{"op": "mark_chapter", "start_line": 1, "title": "Line 1"}]
        operations_json = json.dumps(operations)
        responses = iter(
            [
                '{"action":"finish","summary":"too soon","confidence":0.2,"operations":' + operations_json + "}",
                '{"action":"inspect_document_structure","arguments":{"max_documents":10}}',
                '{"action":"finish","summary":"inspected and evaluated","confidence":0.8,"operations":'
                + operations_json
                + "}",
            ]
        )
        result = run_source_cleaning_agent(
            document,
            config=SourceCleaningAgentConfig(max_iterations=4, max_finish_reviews=0),
            completion_func=lambda **_kwargs: next(responses),
        )

        actions = [item["action"] for item in result.tool_trace]
        self.assertEqual(actions, ["workflow_review", "inspect_document_structure"])
        self.assertEqual(result.operations, operations)
        self.assertEqual(result.summary, "inspected and evaluated")

    def test_long_source_batch_inspection_reduces_turns_and_satisfies_workflow(self):
        document = build_source_document_from_text(
            "\n".join(f"Line {index}" for index in range(1, 101)),
            filename="long-source.txt",
        )
        operations = [{"op": "mark_chapter", "start_line": 1, "title": "Line 1"}]
        operations_json = json.dumps(operations)
        responses = iter(
            [
                (
                    '{"action":"batch","arguments":{"commands":['
                    '{"action":"preview","arguments":{"start_line":1,"end_line":100,"max_blocks":8}},'
                    '{"action":"regex_search","arguments":{"pattern":"^Line (1|100)$","max_hits":5}}'
                    "]}}"
                ),
                '{"action":"finish","summary":"batched and evaluated","confidence":0.8,"operations":'
                + operations_json
                + "}",
            ]
        )
        request_messages = []

        def fake_completion(**kwargs):
            request_messages.append(kwargs["messages"])
            return next(responses)

        result = run_source_cleaning_agent(
            document,
            config=SourceCleaningAgentConfig(max_iterations=4, max_finish_reviews=0),
            completion_func=fake_completion,
        )

        self.assertEqual(result.iterations, 2)
        self.assertEqual(result.operations, operations)
        batch = result.tool_trace[0]["observation"]
        self.assertEqual(batch["executed_commands"], 2)
        self.assertEqual(batch["results"][0]["observation"]["returned_blocks"], 8)
        batch_feedback = request_messages[1][-1]["content"]
        self.assertNotIn("<truncated>", batch_feedback)
        self.assertIn('"action": "preview"', batch_feedback)
        self.assertIn('"action": "regex_search"', batch_feedback)

    def test_agent_compacts_older_tool_turns_into_bounded_evidence_ledger(self):
        document = build_source_document_from_text(
            "\n".join(f"Line {index} " + ("narrative " * 20) for index in range(1, 101)),
            filename="long-source.txt",
        )
        responses = iter(
            [
                '{"action":"preview","arguments":{"start_line":1,"end_line":100,"max_blocks":16}}',
                '{"action":"preview","arguments":{"start_line":1,"end_line":50,"max_blocks":16}}',
                '{"action":"preview","arguments":{"start_line":51,"end_line":100,"max_blocks":16}}',
                '{"action":"preview","arguments":{"start_line":20,"end_line":80,"max_blocks":16}}',
                '{"action":"finish","summary":"done","confidence":0.5,"operations":[]}',
            ]
        )
        request_sizes = []

        def fake_completion(**kwargs):
            request_sizes.append(sum(len(str(item.get("content") or "")) for item in kwargs["messages"]))
            return next(responses)

        result = run_source_cleaning_agent(
            document,
            config=SourceCleaningAgentConfig(
                max_iterations=5,
                require_verified_finish_for_long_sources=False,
                recent_detailed_turns=1,
                max_evidence_ledger_chars=1800,
                max_finish_reviews=0,
            ),
            completion_func=fake_completion,
        )

        self.assertEqual(len(request_sizes), 5)
        self.assertLess(max(request_sizes), request_sizes[0] + 16000)
        self.assertEqual(result.llm_usage["max_request_context_chars"], max(request_sizes))

    def test_long_source_does_not_accept_unverified_finish_at_iteration_limit(self):
        document = build_source_document_from_text(
            "\n".join(f"Line {index}" for index in range(1, 51)),
            filename="long-source.txt",
        )

        result = run_source_cleaning_agent(
            document,
            config=SourceCleaningAgentConfig(max_iterations=1),
            completion_func=lambda **_kwargs: (
                '{"action":"finish","summary":"too soon","confidence":0.2,'
                '"operations":[{"op":"delete_range","start_line":1,"end_line":20}]}'
            ),
        )

        self.assertFalse(result.operations)
        self.assertEqual(result.tool_trace[0]["action"], "workflow_review")
        self.assertTrue(any("inspection tool" in warning for warning in result.warnings))

    def test_agent_aggregates_litellm_usage_and_cost(self):
        document = build_source_document_from_text("Title\nNarration.", filename="sample.pdf")
        responses = iter(
            [
                llm_handler.ChatCompletionResult(
                    content='{"action":"find_metadata_candidates","arguments":{}}',
                    model="openai/test-model",
                    usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                    cost=0.001,
                    cost_source="litellm_hidden_params",
                ),
                llm_handler.ChatCompletionResult(
                    content='{"action":"finish","summary":"done","confidence":0.8,"operations":[]}',
                    model="openai/test-model",
                    usage={"prompt_tokens": 140, "completion_tokens": 10, "total_tokens": 150},
                    cost=0.002,
                    cost_source="litellm_hidden_params",
                ),
            ]
        )

        result = run_source_cleaning_agent(
            document,
            config=SourceCleaningAgentConfig(max_iterations=3),
            completion_func=lambda **_kwargs: next(responses),
        )

        self.assertEqual(result.llm_usage["call_count"], 2)
        self.assertEqual(result.llm_usage["prompt_tokens"], 240)
        self.assertEqual(result.llm_usage["completion_tokens"], 30)
        self.assertEqual(result.llm_usage["total_tokens"], 270)
        self.assertEqual(result.llm_usage["uncached_prompt_tokens"], 240)
        self.assertAlmostEqual(result.llm_usage["cost_usd"], 0.003)
        self.assertEqual(result.llm_usage["cost_available_calls"], 2)

    def test_parse_json_command_extracts_fenced_json(self):
        command, error = parse_json_command(
            "Here is the command:\n```json\n{\"action\":\"find_metadata_candidates\",\"arguments\":{}}\n```"
        )

        self.assertFalse(error)
        self.assertEqual(command["action"], "find_metadata_candidates")

    def test_parse_json_command_accepts_extra_text_and_adjacent_objects(self):
        command, error = parse_json_command(
            '{"action":"search","arguments":{"query":"chapter"}}\n'
            '{"action":"find_heading_candidates","arguments":{"max_candidates":20}}'
        )

        self.assertFalse(error)
        self.assertEqual(command["action"], "search")

        command_with_tail, tail_error = parse_json_command(
            '```json\n{"action":"preview","arguments":{"start_line":1,"end_line":5}}\n```\n'
            "I will inspect this before deciding."
        )

        self.assertFalse(tail_error)
        self.assertEqual(command_with_tail["action"], "preview")

    def test_parse_json_command_rejects_object_without_action(self):
        command, error = parse_json_command('{"summary":"missing action","operations":[]}')

        self.assertEqual(command, {})
        self.assertIn("non-empty action", error)

    def test_parse_json_command_infers_finish_for_complete_final_payload(self):
        command, error = parse_json_command(
            '{"summary":"done","confidence":0.9,"operations":[{"op":"delete_range","start_line":1,"end_line":2}]}'
        )

        self.assertFalse(error)
        self.assertEqual(command["action"], "finish")

    def test_litellm_response_metadata_extracts_usage_and_hidden_cost(self):
        class _FakeResponse:
            _hidden_params = {"response_cost": 0.0123}

            def model_dump(self, mode=None):
                return {
                    "id": "response-1",
                    "model": "openai/test-model",
                    "choices": [{"message": {"content": "hello"}}],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 4,
                        "total_tokens": 15,
                    },
                }

        result = llm_handler._extract_chat_completion_result(_FakeResponse())

        self.assertEqual(result.content, "hello")
        self.assertEqual(result.usage["total_tokens"], 15)
        self.assertAlmostEqual(result.cost, 0.0123)
        self.assertEqual(result.cost_source, "litellm_hidden_params")

    def test_validator_warns_about_high_deletion_and_missing_chapters(self):
        document = build_source_document_from_text(
            "\n".join(f"line {index}" for index in range(1, 51)),
            filename="sample.pdf",
        )
        result = apply_cleaning_operations(
            document,
            [{"op": "delete_range", "start_line": 1, "end_line": 25, "reason": "test"}],
        )

        validation = validate_cleaning_result(document, result, remove_footnotes=False)

        self.assertFalse(validation.errors)
        joined_warnings = "\n".join(validation.warnings)
        self.assertIn("deletion ratio", joined_warnings.lower())
        self.assertIn("No chapter markers", joined_warnings)
        self.assertFalse(validation.blocking_warnings)

    def test_validator_warns_about_suspiciously_low_chapter_count(self):
        document = build_source_document_from_text(
            "\n".join(["Chapter One"] + [f"line {index}" for index in range(2, 401)]),
            filename="sample.pdf",
        )
        document.nav_titles = ["Chapter One", "Chapter Two", "Chapter Three", "Chapter Four"]
        result = apply_cleaning_operations(
            document,
            [{"op": "mark_chapter", "start_line": 1, "title": "Chapter One"}],
        )

        validation = validate_cleaning_result(document, result, remove_footnotes=False)

        joined_warnings = "\n".join(validation.warnings)
        self.assertIn("Only one chapter marker", joined_warnings)
        self.assertIn("EPUB navigation title", joined_warnings)

    def test_validator_warns_about_remaining_structured_cleanup_and_removed_bookends(self):
        document = SourceDocument(
            source_type="epub",
            source_path="sample.epub",
            filename="sample.epub",
            metadata_candidates={"title": [{"value": "Sample Book"}]},
            blocks=[
                SourceBlock("title", "Sample Book", 1, 1, href="body.xhtml", tag="h1", role_candidates=["heading"]),
                SourceBlock("toc-1", "Chapter I", 2, 2, href="body.xhtml", tag="p", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("toc-2", "Chapter II", 3, 3, href="body.xhtml", tag="p", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("toc-3", "Chapter III", 4, 4, href="body.xhtml", tag="p", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("toc-4", "Chapter IV", 5, 5, href="body.xhtml", tag="p", classes=["toc"], role_candidates=["toc"]),
                SourceBlock("body", "The story begins.", 6, 6, href="body.xhtml", tag="p"),
                SourceBlock("end", "THE END", 7, 7, href="body.xhtml", tag="h3", role_candidates=["heading"]),
                SourceBlock("license-1", "START: FULL LICENSE", 8, 8, href="license.xhtml", tag="div"),
                SourceBlock("license-2", "THE FULL PROJECT GUTENBERG LICENSE", 9, 9, href="license.xhtml", tag="h2"),
                SourceBlock("license-3", "Project Gutenberg terms of use", 10, 10, href="license.xhtml", tag="div"),
                SourceBlock("license-4", "Redistributing Project Gutenberg works", 11, 11, href="license.xhtml", tag="div"),
            ],
        )
        result = apply_cleaning_operations(
            document,
            [
                {"op": "delete_blocks", "block_ids": ["title", "end"], "reason": "bad broad cleanup"},
            ],
        )

        validation = validate_cleaning_result(document, result)
        joined_warnings = "\n".join(validation.warnings).lower()

        self.assertIn("structured toc/navigation", joined_warnings)
        self.assertIn("boilerplate/license", joined_warnings)
        self.assertIn("book title heading", joined_warnings)
        self.assertIn("narrative closing marker", joined_warnings)
        self.assertEqual(validation.blocking_warnings, validation.warnings)

    def test_app_logic_source_cleaning_writes_augmented_artifacts_without_network(self):
        outputs_dir = os.path.join(self.temp_dir.name, "Outputs")
        os.makedirs(outputs_dir, exist_ok=True)
        source_path = os.path.join(self.temp_dir.name, "Author - Demo.txt")
        raw_text = "\n".join(
            [
                "Copyright 2026 Publisher",
                "Chapter One",
                "Narration begins.",
            ]
        )
        with open(source_path, "w", encoding="utf-8") as file_handle:
            file_handle.write(raw_text)

        class _DummySignal:
            def __init__(self):
                self.calls = []

            def emit(self, *args):
                self.calls.append(args)

        harness = SimpleNamespace(
            state=SimpleNamespace(
                session_name="Demo Session",
                raw_text=raw_text,
                source_file_path=source_path,
                metadata={},
                llm=SimpleNamespace(),
            ),
            log_message=_DummySignal(),
            _is_named_session_active=lambda: True,
        )

        fake_agent_result = SourceCleaningAgentResult(
            operations=[
                {"op": "set_metadata", "title": "Demo", "author": "Author", "language": "en"},
                {"op": "delete_range", "start_line": 1, "end_line": 1, "reason": "copyright"},
                {"op": "mark_chapter", "line_start": 2, "title": "Chapter One"},
            ],
            summary="Fixture cleaning plan.",
            confidence=0.9,
        )

        with patch.object(session_handler, "OUTPUTS_DIR", outputs_dir):
            with patch(
                "pandrator.logic.source_cleaning.pipeline.run_source_cleaning_agent",
                return_value=fake_agent_result,
            ) as run_agent:
                phase_limits = {
                    "metadata": 2,
                    "navigation": 3,
                    "boilerplate": 4,
                    "repeated_elements": 5,
                    "chapter_marking": 6,
                }
                result = AppLogic.run_source_cleaning(
                    harness,
                    model_name="default",
                    phase_max_iterations=phase_limits,
                )

        self.assertEqual(run_agent.call_count, 5)
        self.assertEqual(
            [call.kwargs["config"].max_iterations for call in run_agent.call_args_list],
            [phase_limits[name] for name in PHASE_ORDER],
        )
        self.assertTrue(result["success"])
        self.assertIn("[[Chapter]]Chapter One", result["cleaned_text"])
        self.assertNotIn("Copyright", result["cleaned_text"])
        self.assertEqual(result["metadata"]["artist"], "Author")

        artifacts_dir = result["artifacts_dir"]
        self.assertTrue(os.path.isdir(artifacts_dir))
        expected_files = {
            "raw_index.json",
            "raw_text.txt",
            "cleaned_text.txt",
            "cleaning_rules.json",
            "cleaning_report.json",
            "diff.patch",
        }
        self.assertTrue(expected_files.issubset(set(os.listdir(artifacts_dir))))

        with open(os.path.join(artifacts_dir, "cleaning_report.json"), "r", encoding="utf-8") as file_handle:
            report = json.load(file_handle)

        self.assertIn("pipeline", report)
        self.assertIn("llm_usage", report)
        self.assertIn("validation", report)
        self.assertEqual(len(report["pipeline"]["phases"]), 5)
        self.assertEqual(report["chapter_count"], 1)

    def test_source_cleaning_dialog_accepts_and_returns_user_edited_cleaned_text(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        from pandrator.app_state import SourceCleaningSettings
        from pandrator.gui.dialogs.source_cleaning_dialog import SourceCleaningDialog

        app = QApplication.instance() or QApplication([])
        logic = SimpleNamespace(
            state=SimpleNamespace(
                raw_text="Raw source preview",
                llm=SimpleNamespace(default_model="default"),
                source_cleaning=SourceCleaningSettings(),
            ),
            list_llm_models=lambda: ["default"],
        )
        dialog = SourceCleaningDialog(logic)
        self.assertTrue(dialog.accept_button.isEnabled())

        dialog._on_cleaning_finished(
            {
                "success": True,
                "cleaned_text": "Generated cleaned text",
                "metadata": {},
                "warnings": [],
            }
        )
        self.assertTrue(dialog.accept_button.isEnabled())

        dialog.text_edit.clear()
        self.assertFalse(dialog.accept_button.isEnabled())

        dialog.text_edit.setPlainText("User edited cleaned text")
        self.assertTrue(dialog.accept_button.isEnabled())
        dialog._accept_cleaned_text()

        data = dialog.get_data()
        self.assertEqual(dialog.choice(), "accept")
        self.assertEqual(data["text"], "User edited cleaned text")
        self.assertEqual(data["result"]["cleaned_text"], "User edited cleaned text")
        self.assertTrue(data["result"]["user_edited"])
        app.processEvents()

    def test_source_cleaning_dialog_expands_and_saves_per_phase_defaults(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        from pandrator.app_state import SourceCleaningSettings
        from pandrator.gui.dialogs.source_cleaning_dialog import SourceCleaningDialog

        app = QApplication.instance() or QApplication([])
        persist_calls = []
        settings = SourceCleaningSettings()
        logic = SimpleNamespace(
            state=SimpleNamespace(
                raw_text="Raw source preview",
                metadata={},
                llm=SimpleNamespace(default_model="default", reasoning_effort=""),
                source_cleaning=settings,
                text_processing=SimpleNamespace(remove_footnotes=False, filter_citations=True),
            ),
            list_llm_models=lambda: ["default"],
            _persist_global_settings=lambda: persist_calls.append(True),
        )
        dialog = SourceCleaningDialog(logic, source_path_hint="sample.epub")

        self.assertTrue(dialog.phase_limits_content.isHidden())
        self.assertIn("up to 53 LLM turns total", dialog.phase_limits_summary.text())
        dialog.phase_limits_toggle.click()
        app.processEvents()
        self.assertFalse(dialog.phase_limits_content.isHidden())

        dialog.phase_iteration_spinboxes["metadata"].setValue(12)
        self.assertIn("up to 61 LLM turns total", dialog.phase_limits_summary.text())
        self.assertGreaterEqual(dialog.phase_iteration_spinboxes["metadata"].minimumWidth(), 120)

        dialog.save_phase_defaults_button.click()
        self.assertEqual(settings.phase_max_iterations["metadata"], 12)
        self.assertEqual(settings.max_iterations, 61)
        self.assertEqual(len(persist_calls), 1)
        self.assertIn("saved as defaults", dialog.status_label.text())

    def test_source_cleaning_dialog_refreshes_preview_in_background_with_page_progress(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        from pandrator.app_state import SourceCleaningSettings
        from pandrator.gui.dialogs.source_cleaning_dialog import SourceCleaningDialog

        app = QApplication.instance() or QApplication([])
        started = threading.Event()
        release = threading.Event()

        def extract_preview(*_args, progress_callback=None, **_kwargs):
            started.set()
            if progress_callback is not None:
                progress_callback("Running OCR on PDF page 2/4...")
            release.wait(timeout=2)
            return "Refreshed PDF preview"

        logic = SimpleNamespace(
            state=SimpleNamespace(
                raw_text="Raw source preview",
                metadata={},
                llm=SimpleNamespace(default_model="default", reasoning_effort=""),
                source_cleaning=SourceCleaningSettings(),
                text_processing=SimpleNamespace(remove_footnotes=False, filter_citations=True),
            ),
            list_llm_models=lambda: ["default"],
            extract_deterministic_clean_text=extract_preview,
            get_pdf_ingestion_review_data=lambda: {},
        )
        dialog = SourceCleaningDialog(logic, source_path_hint="sample.pdf")

        dialog._on_deterministic_settings_changed()
        self.assertTrue(started.wait(timeout=1))
        app.processEvents()

        self.assertTrue(dialog._running)
        self.assertEqual(dialog.progress_bar.maximum(), 4)
        self.assertEqual(dialog.progress_bar.value(), 2)
        self.assertIn("Running OCR", dialog.status_label.text())

        release.set()
        deadline = time.time() + 2
        while dialog._running and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)

        self.assertFalse(dialog._running)
        self.assertEqual(dialog.text_edit.toPlainText(), "Refreshed PDF preview")
        self.assertIn("Preview refreshed", dialog.status_label.text())

    @patch("pandrator.logic.source_cleaning.epub_adapter.epub.read_epub")
    @patch("pandrator.logic.source_cleaning.deterministic.parser.unpack_epub_structure")
    def test_epub_adapter_pre_annotations(self, mock_unpack, mock_read_epub):
        from unittest.mock import MagicMock
        
        # Setup mock book
        import ebooklib
        mock_item = MagicMock()
        mock_item.get_name.return_value = "text/chap1.xhtml"
        mock_item.get_type.return_value = ebooklib.ITEM_DOCUMENT
        mock_item.get_id.return_value = "chap1"
        mock_item.get_content.return_value = b"<html><body><h1>Chapter 1</h1><p class='footnote'>Footnote text</p></body></html>"
        
        mock_book = MagicMock()
        mock_book.get_items.return_value = [mock_item]
        mock_book.spine = [("chap1", "yes")]
        mock_read_epub.return_value = mock_book
        
        # Setup mock unpacked EPUB structure
        mock_unpack.return_value = {
            "spine": [
                {"href": "text/chap1.xhtml", "media_type": "application/xhtml+xml", "linear": "yes"},
                {"href": "text/notes.xhtml", "media_type": "application/xhtml+xml", "linear": "no"}
            ],
            "parsed_documents": {
                "text/chap1.xhtml": {
                    "size": 100,
                    "blocks": [{"tag": "h1", "text": "Chapter 1", "classes": []}]
                },
                "text/notes.xhtml": {
                    "size": 500,
                    "blocks": []
                }
            }
        }
        
        from pandrator.logic.source_cleaning.epub_adapter import build_source_document
        doc = build_source_document("dummy.epub")
        
        # Verify the blocks got their deterministic roles
        self.assertTrue(len(doc.blocks) > 0)
        # Find chapter block
        chapter_block = next((b for b in doc.blocks if b.tag == "h1"), None)
        self.assertIsNotNone(chapter_block)
        self.assertIn("deterministic_chapter", chapter_block.role_candidates)

    def test_extract_deterministic_clean_text_pdf_fallback(self):
        from pandrator.app_logic import AppLogic
        from pandrator.app_state import AppState
        
        logic = AppLogic()
        logic.state = AppState()
        logic.state.raw_text = "Standard extracted text"
        
        # Calling extract_deterministic_clean_text with non-existent file path should fall back to state.raw_text
        text = logic.extract_deterministic_clean_text("non_existent.epub", remove_footnotes=True, filter_citations=True)
        self.assertEqual(text, "Standard extracted text")


if __name__ == "__main__":
    unittest.main()
