from __future__ import annotations

import pytest
from ebooklib import epub

from pandrator.logic.source_cleaning.deterministic import extract_clean_epub
from pandrator.logic.source_cleaning.deterministic.footnotes import _resolve_fragment_index
from pandrator.logic.source_cleaning.deterministic.note_content import get_footnote_text_multi_block
from pandrator.logic.source_cleaning.deterministic.parser import EPUBHTMLParser


def parse(markup):
    parser = EPUBHTMLParser("notes.xhtml")
    parser.feed(markup)
    parser.close()
    return {"ids": parser.ids, "blocks": parser.blocks}


def note(document, fragment):
    index = _resolve_fragment_index(document, fragment)
    assert index is not None
    return get_footnote_text_multi_block(document["blocks"], index)


NESTED_NOTES = (
    '<div class="fn"><a id="FTN-1" href="body.xhtml#Ref-FTN-1">a</a> '
    '<div>First note only.</div></div>'
    '<div class="fn"><a id="FTN-2" href="body.xhtml#Ref-FTN-2">b</a> '
    '<div>Second distinct note.</div></div>'
)


def test_inline_note_ids_keep_their_position_after_parent_resumes():
    document = parse(NESTED_NOTES)
    assert document["ids"]["FTN-1"] == 0
    assert document["blocks"][0]["nested_ids"] == ["FTN-1"]
    first_text, first_consumed = note(document, "FTN-1")
    second_text, second_consumed = note(document, "FTN-2")
    assert first_text == "First note only."
    assert second_text == "Second distinct note."
    assert not set(first_consumed).intersection(second_consumed)


def test_scoped_note_preserves_multiple_paragraphs_and_incidental_ids():
    document = parse(
        '<aside id="note1"><p>First paragraph.</p>'
        '<p><span id="page7"></span>Second paragraph.</p></aside>'
        '<p>Following narrative must remain separate.</p>'
    )
    text, consumed = note(document, "note1")
    assert text == "First paragraph. Second paragraph."
    assert all("Following narrative" not in document["blocks"][index]["text"] for index in consumed)


def test_unwrapped_note_stops_at_next_inline_anchor():
    document = parse(
        '<p><a id="note1" href="body.xhtml#back1">1</a></p>'
        '<p>First body.</p><p>Continuation.</p>'
        '<p><a id="note2" href="body.xhtml#back2">2</a></p>'
        '<p>Second body.</p>'
    )
    assert note(document, "note1")[0] == "First body. Continuation."
    assert note(document, "note2")[0] == "Second body."


def test_note_aliases_on_one_container_resolve_the_same_body():
    document = parse(
        '<div id="note1"><a id="alias1"></a><p>Shared body.</p></div>'
        '<div id="note2"><p>Other body.</p></div>'
    )
    assert note(document, "note1")[0] == "Shared body."
    assert note(document, "alias1")[0] == "Shared body."
    assert note(document, "note2")[0] == "Other body."


@pytest.mark.parametrize("label", ["ee", "jl", "Back to text"])
def test_explicit_backlink_labels_are_not_spoken_as_note_text(label):
    document = parse(
        f'<div><a id="FTN-135" href="body.xhtml#Ref-FTN-135">{label}</a>'
        '<div>Actual note body.</div></div>'
    )
    assert note(document, "FTN-135")[0] == "Actual note body."


def test_hyperlinked_prose_prefix_is_preserved():
    document = parse(
        '<aside id="note1"><a href="sources.xhtml#bibliography">Evidence</a>'
        '<p>Continues in a child paragraph.</p></aside>'
    )
    assert note(document, "note1")[0] == "Evidence Continues in a child paragraph."


def test_extraction_places_each_nested_note_at_its_own_reference(tmp_path):
    book = epub.EpubBook()
    book.set_identifier("nested-note-regression")
    book.set_title("Nested Notes")
    book.set_language("en")
    body = epub.EpubHtml(title="Body", file_name="body.xhtml")
    body.content = (
        '<h1>Body</h1><p>First claim<a epub:type="noteref" href="notes.xhtml#FTN-1">1</a>.</p>'
        '<p>Second claim<a epub:type="noteref" href="notes.xhtml#FTN-2">2</a>.</p>'
    )
    notes = epub.EpubHtml(title="Notes", file_name="notes.xhtml")
    notes.content = '<section epub:type="endnotes">' + NESTED_NOTES + '</section>'
    book.add_item(body)
    book.add_item(notes)
    book.add_item(epub.EpubNcx())
    book.toc = [epub.Link("body.xhtml", "Body", "body")]
    book.spine = [body, notes]
    path = tmp_path / "nested-notes.epub"
    epub.write_epub(str(path), book)
    text = extract_clean_epub(str(path), filter_citations=False)
    assert text.count("First note only.") == 1
    assert text.count("Second distinct note.") == 1
    assert text.index("First claim") < text.index("First note only.") < text.index("Second claim")
    assert text.index("Second claim") < text.index("Second distinct note.")
