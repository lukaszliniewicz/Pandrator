import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ebooklib import epub

from pandrator.app_logic import AppLogic
from pandrator.logic import session_handler
from pandrator.logic.source_cleaning import (
    SourceCleaningAgentConfig,
    SourceCleaningAgentResult,
    SourceCleaningTools,
    apply_cleaning_operations,
    build_source_document,
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

    def test_epub_index_preserves_metadata_markup_and_nonsemantic_blocks(self):
        epub_path = self._write_epub_fixture()

        document = build_source_document(epub_path)

        self.assertEqual(document.source_type, "epub")
        self.assertEqual(document.language, "pl")
        self.assertIn("title", document.metadata_candidates)
        self.assertEqual(document.metadata_candidates["title"][0]["value"], "Strange Book")
        self.assertEqual(document.metadata_candidates["author"][0]["value"], "Example Author")
        self.assertIn("Rozdzial pierwszy", document.nav_titles)

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

    def test_parse_json_command_extracts_fenced_json(self):
        command, error = parse_json_command(
            "Here is the command:\n```json\n{\"action\":\"find_metadata_candidates\",\"arguments\":{}}\n```"
        )

        self.assertFalse(error)
        self.assertEqual(command["action"], "find_metadata_candidates")

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
                "pandrator.app_logic.source_cleaning.run_source_cleaning_agent",
                return_value=fake_agent_result,
            ) as run_agent:
                result = AppLogic.run_source_cleaning(harness, model_name="default")

        run_agent.assert_called_once()
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

        self.assertIn("agent", report)
        self.assertIn("validation", report)
        self.assertEqual(report["agent"]["summary"], "Fixture cleaning plan.")
        self.assertEqual(report["chapter_count"], 1)


if __name__ == "__main__":
    unittest.main()
