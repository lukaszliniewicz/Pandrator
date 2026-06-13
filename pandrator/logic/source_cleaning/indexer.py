from __future__ import annotations

import os

from . import epub_adapter, pdf_adapter, pdf_text_adapter
from .models import SourceDocument


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
