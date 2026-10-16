from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from ebooklib import epub

from pandrator.logic.file_handler import extract_text_from_epub
from pandrator.logic.source_cleaning.deterministic import (
    extract_clean_epub,
    extract_epub_with_diagnostics,
)
from pandrator.logic.source_cleaning.deterministic.illustrations import is_visual_material_block
from pandrator.logic.source_cleaning.deterministic.parser import (
    EPUBHTMLParser,
    parse_single_document,
    unpack_epub_structure,
)
from pandrator.logic.source_cleaning.epub_adapter import build_source_document
from pandrator.logic.source_cleaning.epub_narration import prepare_narration_html


def blocks(markup, *, tables="headers", images="descriptions", language="en"):
    parser = EPUBHTMLParser("body.xhtml")
    parser.feed(prepare_narration_html(
        markup, table_mode=tables, image_mode=images, language=language,
    ))
    parser.close()
    return [block for block in parser.blocks if block["text"]]


def lines(markup, **kwargs):
    return [block["text"] for block in blocks(markup, **kwargs)]


def write_epub(path: Path, markup: str, *, language="en", explicit_toc=True, include_note=True) -> str:
    book = epub.EpubBook()
    book.set_identifier("narration-test")
    book.set_title("Narration Fixture")
    book.set_language(language)
    chapter = epub.EpubHtml(title="Body", file_name="body.xhtml", lang=language)
    chapter.content = f"<html><body>{markup}</body></html>"
    note = epub.EpubHtml(title="Notes", file_name="notes.xhtml", lang=language)
    note.content = '<html><body><section epub:type="endnotes"><aside id="note1" epub:type="footnote">1. Real note.</aside></section></body></html>'
    book.add_item(chapter)
    if include_note:
        book.add_item(note)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    if explicit_toc:
        book.toc = [epub.Link("body.xhtml", "Body", "body")]
        if include_note:
            book.toc.append(epub.Link("notes.xhtml", "Notes", "notes"))
    book.spine = ["nav", chapter, note] if include_note else ["nav", chapter]
    epub.write_epub(str(path), book)
    return str(path)


TABLE = (
    "<table><caption>Harvest</caption><thead><tr><th>Crop</th><th>Tonnes</th></tr></thead>"
    "<tbody><tr><td>Wheat</td><td>12</td></tr><tr><td>Rye</td><td>8</td></tr></tbody></table>"
)


def test_labelled_table_has_source_order_headers_and_independent_rows():
    assert lines(TABLE) == [
        "Table. Harvest", "Crop; Tonnes", "Row 1. Crop: Wheat; Tonnes: 12",
        "Row 2. Crop: Rye; Tonnes: 8", "End table.",
    ]
    assert lines(TABLE, tables="rows") == ["Harvest", "Crop; Tonnes", "Wheat; 12", "Rye; 8"]
    assert lines(TABLE, tables="skip") == []


def test_unlabelled_poetry_and_presentation_tables_remain_plain_rows():
    markup = "<table><tr><td>A line</td><td>of verse</td></tr><tr><td>Another line</td></tr></table>"
    assert lines(markup) == ["A line; of verse", "Another line"]
    for mode in ("headers", "rows", "skip"):
        assert lines(markup.replace("<table>", '<table role="presentation">'), tables=mode) == [
            "A line", "of verse", "Another line",
        ]
    assert lines("<table><caption>A real title</caption><tr><td>Unlabelled</td></tr></table>") == [
        "A real title", "Unlabelled",
    ]
    assert lines("<table><tr><th></th></tr><tr><td>Chronology prose.</td></tr></table>") == ["Chronology prose."]


def test_headers_id_takes_precedence_over_inferred_column_headers():
    markup = (
        '<table><tr><th id="north">North</th><th id="south">South</th></tr>'
        '<tr><td headers="south">17</td><td headers="north">23</td></tr></table>'
    )
    assert lines(markup)[2] == "Row 1. South: 17; North: 23"
    assert lines('<table><thead><tr><td>Author header</td></tr></thead>'
                 '<tbody><tr><td>Source value</td></tr></tbody></table>')[2] == "Row 1. Author header: Source value"


def test_empty_table_cells_keep_positions_without_punctuation_only_rows():
    markup = (
        '<table><tr><th></th><th>Item</th><th>Amount</th></tr>'
        '<tr><td id="blank"></td><td></td><td></td></tr>'
        '<tr><td></td><td>Wheat</td><td>12</td></tr></table>'
    )
    assert lines(markup) == [
        "Table.", "Item; Amount", "Row 1. Item: Wheat; Amount: 12", "End table.",
    ]
    parsed = EPUBHTMLParser("body.xhtml")
    parsed.feed(prepare_narration_html(
        markup, table_mode="headers", image_mode="descriptions", language="en",
    ))
    parsed.close()
    assert "blank" in parsed.ids
    assert lines('<table><tr><td></td><td> </td></tr>'
                 '<tr><td>North</td><td></td><td>South</td></tr></table>') == ["North; South"]


def test_scope_and_spans_preserve_positions_without_repeating_values():
    markup = (
        '<table><tr><th rowspan="2" scope="col">Name</th>'
        '<th colspan="2" scope="colgroup">Results</th></tr>'
        '<tr><th scope="col">Old</th><th scope="col">New</th></tr>'
        '<tbody><tr><th rowspan="2" scope="row">Alice</th><td>10</td><td>11</td></tr>'
        '<tr><td>12</td><td>13</td></tr></tbody></table>'
    )
    result = lines(markup)
    assert result[3] == "Row 1. Name: Alice; Results, Old, Alice: 10; Results, New, Alice: 11"
    assert result[4] == "Row 2. Results, Old, Alice: 12; Results, New, Alice: 13"
    assert "Name: 12" not in " ".join(result)
    grouped = (
        '<table><tbody><tr><th scope="rowgroup">North</th><td>One</td></tr>'
        '<tr><td>Two</td></tr></tbody><tbody><tr><td>Three</td></tr></tbody></table>'
    )
    assert "North: Two" in " ".join(lines(grouped))
    assert "North: Three" not in " ".join(lines(grouped))
    column_groups = (
        '<table><colgroup span="2"></colgroup><colgroup></colgroup><tr>'
        '<th scope="colgroup">Pair</th><th>Second</th><th>Third</th></tr>'
        '<tr><td>A</td><td>B</td><td>C</td></tr></table>'
    )
    assert lines(column_groups)[2] == "Row 1. Pair: A; Pair, Second: B; Third: C"


def test_header_only_nested_and_pathological_tables_preserve_content():
    assert lines("<table><tr><th>Alpha</th><th>Beta</th></tr></table>") == [
        "Table.", "Alpha; Beta", "End table.",
    ]
    markup = (
        "<table><tr><td>Outer<p>paragraph</p><table><tr><td>Inner</td></tr>"
        "</table><p>After</p></td><td>Last</td></tr></table>"
    )
    assert lines(markup) == ["Outer", "paragraph", "Inner", "After", "Last"]
    assert lines('<table><tr><th colspan="999999999999999">Header</th></tr>'
                 '<tr><td>Value</td></tr></table>') == ["Header", "Value"]


def test_table_retains_footnote_ids_ruby_and_nested_cell_blocks():
    markup = (
        '<table id="table"><tr><th id="heading">Place</th></tr><tr id="r">'
        '<td id="cell"><p id="nested"><ruby>東京<rt>とうきょう</rt></ruby></p><ul><li>First</li>'
        '<li>Second<a id="ref" href="notes.xhtml#note1" epub:type="noteref">1</a></li>'
        '</ul></td></tr></table>'
    )
    parser = EPUBHTMLParser("body.xhtml")
    parser.feed(prepare_narration_html(markup, table_mode="headers", image_mode="descriptions", language="en"))
    parser.close()
    row = next(block for block in parser.blocks if block["text"].startswith("Row 1"))
    assert row["text"] == "Row 1. Place: 東京 First Second1"
    assert {"table", "heading", "r", "cell", "ref", "nested"} <= parser.ids.keys()
    assert next(part for part in row["parts"] if part["type"] == "anchor")["href"] == "notes.xhtml#note1"


@pytest.mark.parametrize(("language", "table", "row", "end", "image"), [
    ("en-GB", "Table", "Row", "End table", "Image"),
    ("pol-PL", "Tabela", "Wiersz", "Koniec tabeli", "Ilustracja"),
    ("fra-FR", "Tableau", "Ligne", "Fin du tableau", "Illustration"),
    ("nl", "Tabel", "Rij", "Einde van de tabel", "Afbeelding"),
    ("jpn", "表", "行", "表の終わり", "図"),
    ("deu", "Tabelle", "Zeile", "Ende der Tabelle", "Abbildung"),
    ("German", "Tabelle", "Zeile", "Ende der Tabelle", "Abbildung"),
    ("xx", "Table", "Row", "End table", "Image"),
])
def test_published_language_labels(language, table, row, end, image):
    output = lines(TABLE + '<img alt="A blue lake"/>', language=language)
    assert output[0].startswith(table)
    assert output[2].startswith(f"{row} 1")
    assert output[-2].startswith(end)
    assert output[-1] == f"{image}: A blue lake"
    if table == "表":
        assert output[2] == "行 1。 Crop: Wheat、Tonnes: 12"


def test_image_modes_caption_deduplication_and_distinct_descriptions():
    markup = (
        '<figure><img alt="A blue lake."/><figcaption>A blue lake!</figcaption></figure>'
        '<figure><img alt="Mountains behind the lake"/><figcaption>The lake at dawn.</figcaption></figure>'
    )
    assert lines(markup) == ["A blue lake!", "Image: Mountains behind the lake", "The lake at dawn."]
    assert lines(markup, images="captions") == ["A blue lake!", "The lake at dawn."]
    assert lines(markup, images="skip") == []
    assert all(not is_visual_material_block(block) for block in blocks(markup))


@pytest.mark.parametrize("alternative", [
    "image", "cover", "illustration", "Figure 12", "[Illustration]", "ilustracja",
    "Abbildung 3", "図", "img_123.png", "https://example.com/photo.jpg", "plates/pic",
    "Text\n\nDescription automatically generated with medium confidence",
])
def test_generic_or_filename_alternatives_are_silent(alternative):
    assert lines(f'<img alt="{alternative}"/><p>Ordinary picture prose.</p>') == ["Ordinary picture prose."]


def test_image_decorations_aria_and_dropcaps_preserve_prose_letters():
    markup = (
        '<p><img alt="H"/>ello <em>world</em>.</p>'
        '<img alt="" aria-label="Decorative blossom"/>'
        '<img aria-hidden="true" alt="Hidden landscape"/>'
        '<img role="presentation" alt="Hidden portrait"/>'
        '<img role="none" alt="Hidden frame"/>'
        '<figure hidden><img alt="Hidden lake"/><figcaption>Hidden lake caption.</figcaption></figure>'
        '<img aria-label="A waterfall"/>'
        '<img aria-labelledby="caption"/><p id="caption">A labelled waterfall.</p>'
        '<p><img alt="Already in prose."/>Already in prose.</p>'
    )
    assert lines(markup) == ["Hello world.", "Image: A waterfall", "A labelled waterfall.", "Already in prose."]
    assert lines('<p>Before <img alt="Small bird"/> after.</p>') == ["Before Image: Small bird after."]
    assert lines('<img alt="A nearby waterfall"/><p>A nearby waterfall.</p>') == ["A nearby waterfall."]
    assert lines('<img alt="A black/white cat"/>') == ["Image: A black/white cat"]
    assert lines('<p><img alt="H"/>\n  ello world.</p>') == ["Hello world."]
    assert lines('<img alt="A waterfall. Description automatically generated with medium confidence"/>') == ["Image: A waterfall."]
    assert lines('<img aria-labelledby="label"/><span id="label">A standalone label.</span>') == ["Image: A standalone label."]


@pytest.mark.parametrize("mode", ["descriptions", "captions", "skip"])
def test_dropcaps_supply_narrative_letters_in_every_image_mode(mode):
    assert lines('<p><img alt="O"/>nce upon a time.</p>', images=mode) == ["Once upon a time."]
    assert lines('<table role="presentation"><tr><td><p><img alt="O"/>nce upon a time.</p></td></tr></table>', images=mode) == ["Once upon a time."]


def test_presentation_container_does_not_hide_descendant_visual_text():
    assert lines('<figure role="presentation"><img alt="A lake"/><figcaption>Mountains behind the lake.</figcaption></figure>') == [
        "Image: A lake", "Mountains behind the lake.",
    ]


def test_file_placeholders_are_silent_but_caption_prose_with_urls_or_filenames_survives():
    caption = "Map of the route; source https://example.org/map.png"
    assert lines(f'<figure><img alt="route.png"/><figcaption>{caption}</figcaption></figure>') == [caption]
    assert lines(f'<img alt="{caption}"/>') == [f"Image: {caption}"]
    assert lines('<figcaption>The map.png file shows the original route.</figcaption>') == [
        "The map.png file shows the original route.",
    ]
    assert lines('<img alt="https://example.org/map.png"/><img alt="map.png"/>') == []


def test_table_line_breaks_separate_words_without_splitting_inline_letters_or_ruby():
    markup = (
        '<table><tr><th>Black<br/>Men</th><th>In<em>line</em></th></tr>'
        '<tr><td>First<br/>Second</td><td><ruby>山<rt>やま</rt></ruby>路</td></tr></table>'
    )
    assert lines(markup) == [
        "Table.", "Black Men; Inline", "Row 1. Black Men: First Second; Inline: 山路", "End table.",
    ]


@pytest.mark.parametrize("layout", ["nested", "presentation", "none", "headings", "paragraphs"])
def test_layout_tables_keep_chapters_paragraph_boundaries_and_source_order(tmp_path, layout):
    content = '<h1>Chapter Seven</h1><p>First original paragraph.</p><p>Second original paragraph.</p>'
    if layout == "nested":
        markup = f'<table><tr><td><table><tr><td>{content}</td></tr></table></td></tr></table>'
    elif layout in {"presentation", "none"}:
        markup = f'<table role="{layout}"><tr><td>{content}</td></tr></table>'
    elif layout == "headings":
        markup = f'<table><tr><td>{content}</td></tr></table>'
    else:
        markup = '<h1>Chapter Seven</h1><table><tr><td><p>First original paragraph.</p><p>Second original paragraph.</p></td></tr></table>'
    expected = ["[[Chapter]]Chapter Seven", "First original paragraph.", "Second original paragraph."]
    for mode in ("headers", "rows"):
        path = write_epub(tmp_path / f"layout-{mode}.epub", markup, include_note=False)
        text = extract_clean_epub(path, epub_table_mode=mode)
        assert text.split("\n\n") == expected
        assert all(text.count(value.removeprefix("[[Chapter]]")) == 1 for value in expected)
    if layout in {"presentation", "none"}:
        assert extract_clean_epub(path, epub_table_mode="skip").split("\n\n") == expected


def test_explicit_caption_classes_survive_visual_filter_and_ruby():
    markup = '<div class="figcenter"><p class="caption">A <ruby>猫<rt>ねこ</rt></ruby> sleeps.</p></div>'
    result = blocks(markup)
    assert [block["text"] for block in result] == ["A 猫 sleeps."]
    assert result[0]["narration_kind"] == "image_caption"
    assert not is_visual_material_block(result[0])


def test_raw_structure_and_index_keep_source_markup_without_narration(tmp_path):
    path = write_epub(tmp_path / "tables.epub", TABLE + '<p class="caption">A caption.</p>')
    before = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    raw = unpack_epub_structure(path)["parsed_documents"]["body.xhtml"]["blocks"]
    assert not any(block["narration_kind"] for block in raw)
    narrated = extract_clean_epub(path)
    assert "Row 1. Crop: Wheat; Tonnes: 12" in narrated
    assert "A caption." in narrated
    document = build_source_document(path)
    cells = [block for block in document.blocks if block.tag in {"td", "th"}]
    assert [block.text for block in cells] == ["Crop", "Tonnes", "Wheat", "12", "Rye", "8"]
    assert all("data-pandrator-kind" not in block.raw_markup for block in cells)
    assert before == hashlib.sha256(Path(path).read_bytes()).hexdigest()
    with zipfile.ZipFile(path) as archive:
        raw_doc = parse_single_document(archive, "EPUB", "body.xhtml")
        assert not any(block["text"].startswith("Row 1") for block in raw_doc["blocks"])


@pytest.mark.parametrize("entry", [extract_clean_epub, extract_epub_with_diagnostics, extract_text_from_epub])
def test_public_api_modes_are_keyword_only_strict_and_forwarded(tmp_path, entry):
    path = write_epub(tmp_path / "modes.epub", '<p>Main prose.</p>' + TABLE + '<img alt="A valley"/>')
    value = entry(path, False, True, epub_table_mode="skip", epub_image_mode="skip")
    text = value if isinstance(value, str) else value.text
    assert "Main prose." in text and "Harvest" not in text and "valley" not in text
    with pytest.raises(ValueError, match="table mode"):
        entry("missing.epub", epub_table_mode="invalid")
    with pytest.raises(ValueError, match="image mode"):
        entry("missing.epub", epub_image_mode="invalid")
    with pytest.raises(TypeError):
        entry(path, False, True, "rows")


def test_footnotes_resolve_once_in_generated_rows(tmp_path):
    path = write_epub(tmp_path / "notes.epub", (
        '<table><tr><th>Claim</th></tr><tr><td>Source'
        '<a id="reference" href="notes.xhtml#note1" epub:type="noteref">1</a>'
        '</td></tr></table>'
    ))
    text = extract_clean_epub(path)
    assert "Row 1. Claim: Source" in text
    assert text.count("[Footnote: Real note.]") == 1
    assert "Row 1. Claim: Source1 [Footnote: Real note.]" in text


@pytest.mark.parametrize("prefix", ["", "[[Chapter]]"])
def test_anchor_offsets_use_parts_not_repeated_digits(prefix):
    from pandrator.logic.source_cleaning.deterministic.footnotes import _anchor_position_in_block

    anchor = {"type": "anchor", "content": "1"}
    block = {
        "text": prefix + "Row 1. First 1 then note1.",
        "parts": [{"type": "text", "content": "  Row 1.  First 1 then note"}, anchor,
                  {"type": "text", "content": "."}],
    }
    assert _anchor_position_in_block(block, anchor) == block["text"].index("note1") + 4
    block["text"] = "Rewritten chapter"
    assert _anchor_position_in_block(block, anchor) is None


def test_page_list_cannot_create_duplicate_consumed_note_chapter(tmp_path):
    path = write_epub(tmp_path / "page-list.epub", (
        '<table><tr><th>Claim</th></tr><tr><td>Source'
        '<a id="reference" href="notes.xhtml#note1" epub:type="noteref">1</a>'
        '</td></tr></table>'
    ), explicit_toc=False)
    raw = unpack_epub_structure(path)
    assert any("page-list" in block["epub_types"] for block in raw["parsed_documents"]["nav.xhtml"]["blocks"])
    text = extract_clean_epub(path)
    assert text.count("Real note.") == 1
    assert "[[Chapter]]1. Real note." not in text


def test_toc_map_ignores_page_landmark_blocks_but_keeps_real_toc_and_ncx(tmp_path):
    from pandrator.logic.source_cleaning.deterministic.toc import build_global_toc_map

    parser = EPUBHTMLParser("nav.xhtml")
    parser.feed(
        '<nav epub:type="page-list"><p><a href="body.xhtml#page">17</a></p></nav>'
        '<nav epub:type="landmarks"><p><a href="body.xhtml#start">Start</a></p></nav>'
        '<nav role="doc-pagelist"><p><a href="body.xhtml#other">18</a></p></nav>'
        '<nav role="doc-landmarks"><p><a href="body.xhtml#landmark">Landmark</a></p></nav>'
        '<nav epub:type="toc"><p><a href="body.xhtml#chapter">Real Chapter</a></p></nav>'
    )
    parser.close()
    structure = {
        "spine": [{"href": "nav.xhtml"}, {"href": "body.xhtml"}],
        "parsed_documents": {"nav.xhtml": {"blocks": parser.blocks}},
        "ncx_toc": {"other.xhtml": "NCX Chapter"},
    }
    assert build_global_toc_map(structure) == {
        "body.xhtml#chapter": "Real Chapter", "body.xhtml": "Real Chapter",
        "other.xhtml": "NCX Chapter",
    }
    path = write_epub(tmp_path / "real-toc.epub", "<p>Ordinary chapter prose.</p>")
    assert "[[Chapter]]Body" in extract_clean_epub(path)


def test_structured_quoted_rows_do_not_merge_in_audiobook_preprocessing(tmp_path):
    from pandrator.logic.text_preprocessor import preprocess_text

    path = write_epub(tmp_path / "quotes.epub", (
        '<table><tr><th>Quotation</th></tr><tr><td>“First quotation.”</td></tr>'
        '<tr><td>“Second quotation.”</td></tr></table>'
    ))
    text = extract_clean_epub(path)
    settings = {
        "language": "en", "max_sentence_length": 500, "enable_sentence_splitting": True,
        "enable_sentence_appending": False, "enable_nemo_normalization": False,
        "tts_service": "XTTS", "source_file": "fixture.epub", "audiobook_preparation": True,
        "audiobook_dialogue_flow": "auto",
    }
    with patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text", side_effect=lambda value: [value]):
        segments = preprocess_text(text, settings)
    row_segments = [segment["original_sentence"] for segment in segments if segment["original_sentence"].startswith("Row ")]
    assert len(row_segments) == 2
    assert "First quotation" in row_segments[0] and "Second quotation" not in row_segments[0]
    assert "Second quotation" in row_segments[1] and "First quotation" not in row_segments[1]
