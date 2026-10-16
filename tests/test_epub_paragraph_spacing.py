import os
import tempfile
import unittest
from unittest.mock import patch

from ebooklib import epub

from pandrator.logic.source_cleaning.deterministic import extract_clean_epub
from pandrator.logic.source_cleaning.deterministic.parser import unpack_epub_structure
from pandrator.logic.text_preprocessor import preprocess_text


class EpubParagraphSpacingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _write_epub(self) -> str:
        epub_path = os.path.join(self.temp_dir.name, "Paragraph Spacing.epub")
        book = epub.EpubBook()
        book.set_identifier("paragraph-spacing-fixture")
        book.set_title("Paragraph Spacing Fixture")
        book.set_language("en")

        chapter = epub.EpubHtml(title="Chapter One", file_name="chapter.xhtml", lang="en")
        chapter.content = """
        <html><body>
          <div id="chapter-wrapper">
            <p id="first-paragraph">First ordinary paragraph cites
              <a href="notes.xhtml#note-1" epub:type="noteref">1</a>.</p>
            <div id="nested-wrapper">
              <p id="second-paragraph">Second ordinary paragraph remains separate.</p>
            </div>
            <p id="scene-ornament">***</p>
            <p id="third-paragraph">Third ordinary paragraph remains separate.</p>
          </div>
        </body></html>
        """

        notes = epub.EpubHtml(title="Notes", file_name="notes.xhtml", lang="en")
        notes.content = """
        <html><body>
          <aside id="note-1" epub:type="footnote">
            1. Invented footnote target resolves once.
          </aside>
        </body></html>
        """

        book.add_item(chapter)
        book.add_item(notes)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = [
            epub.Link("chapter.xhtml", "Chapter One", "chapter-one"),
            epub.Link("notes.xhtml", "Notes", "notes"),
        ]
        book.spine = ["nav", chapter, notes]

        epub.write_epub(epub_path, book)
        return epub_path

    def test_structural_empty_blocks_do_not_create_scene_gaps(self):
        epub_path = self._write_epub()
        structure = unpack_epub_structure(epub_path)
        chapter_blocks = structure["parsed_documents"]["chapter.xhtml"]["blocks"]
        self.assertTrue(
            any(not block["text"].strip() and block["id"] for block in chapter_blocks)
        )

        extracted = extract_clean_epub(epub_path)

        self.assertIn(
            "Second ordinary paragraph remains separate.\n\n***\n\n"
            "Third ordinary paragraph remains separate.",
            extracted,
        )
        self.assertNotRegex(extracted, r"\n{4,}")
        self.assertEqual(extracted.count("Invented footnote target resolves once."), 1)
        self.assertIn("[Footnote: Invented footnote target resolves once.]", extracted)

        settings = {
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": False,
            "tts_service": "XTTS",
            "source_file": "fixture.epub",
            "audiobook_preparation": True,
            "audiobook_dialogue_flow": "auto",
        }
        with patch(
            "pandrator.logic.text_preprocessor.sentence_segmenter.split_text",
            side_effect=lambda paragraph: [paragraph],
        ):
            segments = preprocess_text(extracted, settings)

        self.assertEqual(
            [segment["original_sentence"] for segment in segments[-3:]],
            [
                "Second ordinary paragraph remains separate.",
                "***.",
                "Third ordinary paragraph remains separate.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
