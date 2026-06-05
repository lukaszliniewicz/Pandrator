from __future__ import annotations

import os

from . import epub_adapter, pdf_text_adapter
from .models import SourceDocument


def build_source_document(source_path: str, extracted_text: str | None = None) -> SourceDocument:
    """Dispatches source indexing by type."""
    ext = os.path.splitext(source_path)[1].lower()
    if ext == ".epub":
        return epub_adapter.build_source_document(source_path)
    if ext == ".pdf":
        if extracted_text is None:
            from .. import file_handler

            extracted_text = file_handler.extract_text_from_pdf(source_path)
        return pdf_text_adapter.build_source_document_from_text(
            extracted_text or "",
            source_path=source_path,
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
