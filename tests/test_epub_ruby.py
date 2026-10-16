import os
import tempfile
import unittest

from bs4 import BeautifulSoup
from ebooklib import epub

from pandrator.logic.source_cleaning.deterministic import extract_clean_epub
from pandrator.logic.source_cleaning.deterministic.parser import EPUBHTMLParser
from pandrator.logic.source_cleaning.epub_adapter import (
    _extract_tag_text_entries,
    _iter_textual_tags,
    build_source_document,
)


class EpubRubyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _parse(self, markup: str) -> list[dict]:
        parser = EPUBHTMLParser("chapter.xhtml")
        parser.feed(markup)
        parser.close()
        return parser.blocks

    def _write_epub(self, chapter_content: str) -> str:
        epub_path = os.path.join(self.temp_dir.name, "Ruby Fixture.epub")
        book = epub.EpubBook()
        book.set_identifier("ruby-fixture")
        book.set_title("Ruby Fixture")
        book.set_language("ja")

        chapter = epub.EpubHtml(title="本文", file_name="chapter.xhtml", lang="ja")
        chapter.content = chapter_content
        book.add_item(chapter)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = []
        book.spine = ["nav", chapter]

        epub.write_epub(epub_path, book)
        return epub_path

    def test_ruby_reading_and_fallback_parentheses_are_suppressed(self):
        blocks = self._parse(
            "<p><ruby>山路<rp>（</rp><rt>やまみち</rt><rp>）</rp>"
            "</ruby>を登る。</p>"
        )

        self.assertEqual([block["text"] for block in blocks], ["山路を登る。"])

    def test_explicit_rb_keeps_base_and_suppresses_nested_rt_and_rtc_text(self):
        blocks = self._parse(
            "<p>駅前は<ruby><rb>東京</rb><rt><span>とう</span>"
            "<em>きょう</em></rt><rtc><strong>別読み</strong></rtc>"
            "</ruby>駅。</p>"
        )

        self.assertEqual([block["text"] for block in blocks], ["駅前は東京駅。"])

    def test_ruby_inside_anchor_keeps_link_content_without_reading(self):
        blocks = self._parse(
            '<p>前<a href="/place"><ruby><rb>山路</rb><rt>やまみち</rt>'
            "</ruby>駅</a>後。</p>"
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["text"], "前山路駅後。")
        anchor = next(part for part in blocks[0]["parts"] if part["type"] == "anchor")
        self.assertEqual(anchor["content"], "山路駅")

    def test_english_inline_formatting_and_ruby_preserve_character_sequence(self):
        blocks = self._parse(
            "<p>Before <em>the</em> <ruby>main <strong>street</strong>"
            "<rt>unread</rt></ruby> after.</p>"
        )

        self.assertEqual(
            [block["text"] for block in blocks],
            ["Before the main street after."],
        )

    def test_clean_epub_extracts_ruby_base_and_ignores_head_text(self):
        epub_path = self._write_epub(
            "<html><head><title>HEAD_ONLY_SENTINEL</title></head>"
            "<body><p><ruby>山路<rt>やまみち</rt></ruby>を登る。</p></body></html>"
        )

        extracted = extract_clean_epub(epub_path)

        self.assertEqual(extracted, "山路を登る。")
        self.assertNotIn("やまみち", extracted)
        self.assertNotIn("HEAD_ONLY_SENTINEL", extracted)

    def test_structured_epub_text_excludes_reading_and_keeps_raw_ruby_markup(self):
        markup = (
            "<html><head><title>HEAD_ONLY_SENTINEL</title></head><body>"
            "<p><ruby><rb>山路</rb><rp>（</rp><rt><span>やま</span>"
            "みち</rt><rp>）</rp></ruby>を登る。</p></body></html>"
        )
        soup = BeautifulSoup(markup, "html.parser")
        paragraph = soup.find("p")
        self.assertIsNotNone(paragraph)

        text_entries = _extract_tag_text_entries(paragraph)
        self.assertEqual(text_entries[0][0], "山路 を登る。")
        self.assertIn("<rt>", text_entries[0][2])
        self.assertIn("やま", text_entries[0][2])
        self.assertEqual([tag.name for tag in _iter_textual_tags(soup)], ["p"])

        epub_path = self._write_epub(markup)
        document = build_source_document(epub_path)
        block = next(block for block in document.blocks if block.tag == "p")
        self.assertEqual(block.text, "山路 を登る。")
        self.assertIn("<rt>", block.raw_markup)
        self.assertIn("やま", block.raw_markup)
        self.assertNotIn("やまみち", block.text)

    def test_structured_fallback_text_excludes_reading_and_keeps_raw_markup(self):
        epub_path = self._write_epub(
            "<html><body><ruby>山路<rt>やまみち</rt></ruby>を登る。</body></html>"
        )

        document = build_source_document(epub_path)

        chapter_blocks = [block for block in document.blocks if block.href == "chapter.xhtml"]
        self.assertEqual(len(chapter_blocks), 1)
        self.assertEqual(chapter_blocks[0].tag, "document")
        self.assertEqual(chapter_blocks[0].text, "山路 を登る。")
        self.assertIn("<rt>", chapter_blocks[0].raw_markup)
        self.assertNotIn("やまみち", chapter_blocks[0].text)


if __name__ == "__main__":
    unittest.main()
