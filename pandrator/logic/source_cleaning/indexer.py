from __future__ import annotations

import os

from . import epub_adapter, pdf_adapter, pdf_text_adapter
from .models import SourceDocument


def build_cleaned_epub_source_document(
    source_path: str,
    extracted_text: str,
) -> SourceDocument:
    """Index a deterministic EPUB baseline without reopening removed content.

    The structured EPUB index remains useful for metadata and navigation hints,
    but its raw blocks predate deterministic cleanup.  Agentic cleanup must work
    on the already-cleaned text so that a no-op model cannot reintroduce a table
    of contents, publisher boilerplate, illustrations, or removed notes.
    """
    document = pdf_text_adapter.build_source_document_from_text(
        extracted_text or "",
        source_path=source_path,
        filename=os.path.basename(source_path),
    )
    document.source_type = "epub_cleaned_text"
    for block in document.blocks:
        if block.text.startswith("[[Chapter]]"):
            block.text = block.text.removeprefix("[[Chapter]]").strip()
            block.role_candidates.append("deterministic_chapter")

    try:
        structured = epub_adapter.build_source_document(source_path)
    except Exception as error:  # noqa: BLE001 - EPUB parser failures span several libraries
        document.warnings.append(
            f"Structured EPUB metadata indexing failed; using the cleaned text baseline: {error}"
        )
        return document

    document.metadata_candidates = (
        structured.metadata_candidates or document.metadata_candidates
    )
    document.language = structured.language
    document.nav_titles = structured.nav_titles
    document.navigation_entries = structured.navigation_entries
    document.warnings.extend(structured.warnings)
    document.attributes["epub_baseline"] = {
        "structured_block_count": len(structured.blocks),
        "agent_block_count": len(document.blocks),
        "raw_markup_tools_available": False,
    }
    return document


def propose_embedded_chapter_operations(
    document: SourceDocument,
) -> list[dict[str, object]]:
    """Turn chapter roles embedded by a text adapter into reversible operations."""
    return [
        {
            "op": "mark_chapter",
            "block_id": block.block_id,
            "title": block.text,
            "reason": "deterministic embedded chapter marker",
        }
        for block in document.blocks
        if block.text and "deterministic_chapter" in block.role_candidates
    ]


def build_source_document(
    source_path: str,
    extracted_text: str | None = None,
    pdf_config: pdf_adapter.PDFIngestionConfig | None = None,
    artifact_dir: str | None = None,
    progress_callback=None,
) -> SourceDocument:
    """Dispatches source indexing by type."""
    ext = os.path.splitext(source_path)[1].lower()
    if ext == ".epub":
        try:
            document = epub_adapter.build_source_document(source_path)
        except Exception as error:
            if extracted_text is None:
                raise
            document = pdf_text_adapter.build_source_document_from_text(
                extracted_text or "",
                source_path=source_path,
                filename=os.path.basename(source_path),
            )
            document.source_type = "epub_text_fallback"
            document.warnings.append(f"Structured EPUB indexing failed; using extracted text fallback: {error}")
            return document
        if document.blocks or extracted_text is None:
            return document
        fallback = pdf_text_adapter.build_source_document_from_text(
            extracted_text or "",
            source_path=source_path,
            filename=os.path.basename(source_path),
        )
        fallback.source_type = "epub_text_fallback"
        fallback.metadata_candidates = document.metadata_candidates or fallback.metadata_candidates
        fallback.language = document.language
        fallback.nav_titles = document.nav_titles
        fallback.navigation_entries = document.navigation_entries
        fallback.warnings = document.warnings + ["Structured EPUB indexing was empty; using extracted text fallback."]
        return fallback
    if ext == ".pdf":
        return pdf_adapter.build_source_document(
            source_path,
            config=pdf_config,
            artifact_dir=artifact_dir,
            progress_callback=progress_callback,
        )
    if ext == ".txt":
        if extracted_text is None:
            with open(source_path, "r", encoding="utf-8") as file_handle:
                extracted_text = file_handle.read()
        return pdf_text_adapter.build_source_document_from_text(
            extracted_text or "",
            source_path=source_path,
        )
    raise ValueError(f"Unsupported source-cleaning input type: {ext or 'unknown'}")
