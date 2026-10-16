from __future__ import annotations

import posixpath
from urllib.parse import unquote

import pytest

from pandrator.logic.source_cleaning import deterministic
from pandrator.logic.source_cleaning.deterministic import (
    NoteDocumentIndex,
    boilerplate,
    chapters,
    footnotes,
    illustrations,
    note_targets,
    toc,
)


def _brute_force_resolve(
    referencing_href: str,
    target_file: str,
    parsed_documents: dict,
) -> str | None:
    """Historical path scan retained here as the equivalence oracle."""
    if target_file:
        resolved = posixpath.normpath(
            posixpath.join(
                posixpath.dirname(str(referencing_href or "").replace("\\", "/")),
                unquote(str(target_file)).replace("\\", "/"),
            )
        )
    else:
        resolved = referencing_href

    def normalize(value: str) -> str:
        decoded = unquote(str(value or "").split("?", 1)[0]).replace("\\", "/")
        normalized = posixpath.normpath(decoded).lstrip("/")
        return "" if normalized == "." else normalized.casefold()

    normalized = normalize(resolved)
    exact = [key for key in parsed_documents if normalize(str(key)) == normalized]
    if len(exact) == 1:
        return exact[0]

    basename = posixpath.basename(normalized)
    fallback = [
        key
        for key in parsed_documents
        if posixpath.basename(normalize(str(key))) == basename
    ]
    return fallback[0] if len(fallback) == 1 else None


@pytest.mark.parametrize(
    ("referencing_href", "target_file", "document_keys"),
    [
        (
            "OPS/Text/chapter.xhtml",
            "../Notes/./note.xhtml",
            ["OPS/Notes/note.xhtml"],
        ),
        (
            "OPS/Text/chapter.xhtml",
            "/Notes/../Notes/end.xhtml",
            ["Notes/end.xhtml"],
        ),
        (
            r"OPS\Text\chapter.xhtml",
            r"..\Notes\end.xhtml",
            ["OPS/Notes/end.xhtml"],
        ),
        (
            "OPS/Text/chapter.xhtml",
            "../N%6Ftes/note.xhtml?ref=one",
            ["OPS/Notes/No%74e.xhtml?archive=one"],
        ),
        (
            "OPS/Text/chapter.xhtml?source=book",
            "../Notes/note.xhtml?ref=one",
            ["OPS/Notes/note.xhtml?archive=one"],
        ),
        (
            "OPS/Text/chapter.xhtml",
            "../notes/NOTE.XHTML",
            ["OPS/Notes/note.xhtml"],
        ),
        (
            "OPS/Text/chapter.xhtml?source=book",
            "",
            ["OPS/Text/chapter.xhtml"],
        ),
        (
            "OPS/Text/chapter.xhtml",
            "note.xhtml",
            ["OPS/Notes/note.xhtml"],
        ),
        (
            "OPS/Text/chapter.xhtml",
            "note.xhtml",
            ["OPS/Notes/note.xhtml", "Other/note.xhtml"],
        ),
        (
            "OPS/Text/chapter.xhtml",
            "../Notes/NOTE.XHTML",
            ["OPS/Notes/Note.xhtml", "ops/notes/note.xhtml"],
        ),
        (
            "OPS/Text/chapter.xhtml",
            "../Notes/note.xhtml",
            ["OPS/Notes/note.xhtml", "Other/note.xhtml"],
        ),
    ],
)
def test_resolver_matches_historical_brute_force(
    referencing_href: str,
    target_file: str,
    document_keys: list[str],
) -> None:
    parsed_documents = dict.fromkeys(document_keys, {})

    actual = footnotes._resolve_target_document(
        referencing_href,
        target_file,
        parsed_documents,
    )

    assert actual == _brute_force_resolve(
        referencing_href,
        target_file,
        parsed_documents,
    )


def test_exact_unique_path_wins_over_ambiguous_basename() -> None:
    parsed_documents = {
        "OPS/Notes/note.xhtml": {},
        "Other/note.xhtml": {},
    }

    assert footnotes._resolve_target_document(
        "OPS/Text/chapter.xhtml",
        "../Notes/note.xhtml",
        parsed_documents,
    ) == "OPS/Notes/note.xhtml"


def test_index_normalizes_once_per_document_and_once_per_lookup(monkeypatch) -> None:
    document_count = 300
    lookup_count = 300
    parsed_documents = {
        f"OPS/Text/notes/{index}.xhtml": {}
        for index in range(document_count)
    }
    original_normalize = note_targets.normalize_archive_path
    normalization_count = 0

    def counted_normalize(value: str) -> str:
        nonlocal normalization_count
        normalization_count += 1
        return original_normalize(value)

    monkeypatch.setattr(note_targets, "normalize_archive_path", counted_normalize)
    index = NoteDocumentIndex(parsed_documents)
    assert normalization_count == document_count

    normalization_count = 0
    for document_number in range(lookup_count):
        target = f"notes/{document_number}.xhtml"
        assert index.resolve("OPS/Text/chapter.xhtml", target) == (
            f"OPS/Text/notes/{document_number}.xhtml"
        )

    assert normalization_count == lookup_count


def test_extract_shares_one_note_document_index(monkeypatch) -> None:
    doc_href = "OPS/Text/chapter.xhtml"
    parsed_documents = {
        doc_href: {
            "size": 1,
            "ids": {},
            "blocks": [{"block_index": 0, "text": "Body text.", "parts": []}],
        }
    }
    structure = {
        "spine": [{"href": doc_href}],
        "parsed_documents": parsed_documents,
        "metadata": {},
    }
    created_indexes = []
    passed_to_backlinks = []
    passed_to_reposition = []
    original_index = NoteDocumentIndex

    class TrackingNoteDocumentIndex(original_index):
        def __init__(self, documents: dict) -> None:
            super().__init__(documents)
            created_indexes.append(self)

    monkeypatch.setattr(deterministic, "NoteDocumentIndex", TrackingNoteDocumentIndex)
    monkeypatch.setattr(
        deterministic.parser,
        "unpack_epub_structure",
        lambda _path, **_kwargs: structure,
    )
    monkeypatch.setattr(footnotes, "detect_book_language", lambda *_args: "en")
    monkeypatch.setattr(footnotes, "navigation_note_section_hrefs", lambda *_args: set())
    monkeypatch.setattr(footnotes, "is_footnote_file", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(toc, "build_global_toc_map", lambda *_args: {})
    monkeypatch.setattr(toc, "is_toc_file", lambda *_args: False)
    monkeypatch.setattr(boilerplate, "is_front_boilerplate", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(boilerplate, "is_end_boilerplate", lambda *_args: False)
    monkeypatch.setattr(deterministic, "_strip_block_boilerplate", lambda blocks, **_kwargs: blocks)
    monkeypatch.setattr(illustrations, "visual_block_indexes", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(chapters, "has_direct_chapter_semantics", lambda *_args: False)
    monkeypatch.setattr(chapters, "is_chapter_block", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(deterministic, "_ids_by_block_index", lambda _doc: {})
    monkeypatch.setattr(deterministic, "_direct_chapter_titles", lambda *_args: {})
    monkeypatch.setattr(deterministic, "_matched_toc_title", lambda *_args: "")
    monkeypatch.setattr(deterministic, "_is_standalone_chapter_number", lambda *_args: False)
    monkeypatch.setattr(deterministic, "_has_divergent_toc_title_for_number", lambda *_args: False)
    monkeypatch.setattr(deterministic, "_is_toc_validated_parent_heading", lambda *_args: False)
    monkeypatch.setattr(deterministic, "_is_toc_validated_numbered_heading", lambda *_args: False)
    monkeypatch.setattr(deterministic, "_is_toc_validated_heading", lambda *_args: False)
    monkeypatch.setattr(deterministic, "_has_meaningful_text", lambda *_args: True)
    monkeypatch.setattr(deterministic, "_usable_recovered_doc_title", lambda *_args: None)
    monkeypatch.setattr(deterministic, "_dedupe_numbered_chapter_titles", lambda titles: titles)

    original_build_backlink_map = footnotes.build_backlink_map

    def capture_backlink_map(documents: dict, *, document_index=None) -> dict:
        passed_to_backlinks.append(document_index)
        return original_build_backlink_map(documents, document_index=document_index)

    def capture_reposition(
        _doc_href: str,
        blocks: list[dict],
        _documents: dict,
        _repositioned_ids: set,
        **kwargs,
    ) -> list[str]:
        passed_to_reposition.append(kwargs["document_index"])
        return [block["text"] for block in blocks]

    monkeypatch.setattr(footnotes, "build_backlink_map", capture_backlink_map)
    monkeypatch.setattr(footnotes, "reposition_footnotes_in_document", capture_reposition)

    result = deterministic.extract_clean_epub("fixture.epub")

    assert result == "Body text."
    assert len(created_indexes) == 1
    assert passed_to_backlinks == created_indexes
    assert passed_to_reposition == created_indexes
