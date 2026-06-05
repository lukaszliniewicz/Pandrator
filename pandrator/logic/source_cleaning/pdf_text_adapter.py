from __future__ import annotations

import os
import re
from typing import Any

from .models import SourceBlock, SourceDocument


def build_source_document_from_text(
    text: str,
    source_path: str = "",
    filename: str = "",
) -> SourceDocument:
    """Builds a searchable index from already-extracted PDF text."""
    normalized_path = os.path.abspath(source_path) if source_path else ""
    resolved_filename = filename or (os.path.basename(source_path) if source_path else "")
    metadata_candidates = _metadata_from_filename(os.path.splitext(resolved_filename)[0])
    document = SourceDocument(
        source_type="pdf_text",
        source_path=normalized_path,
        filename=resolved_filename,
        metadata_candidates=metadata_candidates,
    )

    line_number = 1
    source_index = 0
    pages = re.split(r"\f+", str(text or "").replace("\r\n", "\n").replace("\r", "\n"))
    for page_index, page_text in enumerate(pages, start=1):
        for raw_line in page_text.split("\n"):
            cleaned = _normalize_pdf_line(raw_line)
            if not cleaned:
                continue

            source_index += 1
            document.blocks.append(
                SourceBlock(
                    block_id=f"pdf:{page_index}:{source_index}",
                    text=cleaned,
                    line_start=line_number,
                    line_end=line_number,
                    source_index=source_index,
                    page=page_index,
                    role_candidates=_infer_pdf_roles(cleaned),
                )
            )
            line_number += 1

    front_matter_candidates = _front_matter_metadata(document.blocks)
    for key, values in front_matter_candidates.items():
        document.metadata_candidates.setdefault(key, []).extend(values)

    return document


def _normalize_pdf_line(line: str) -> str:
    line = re.sub(r"[\x00-\x09\x0B-\x1F\x7F]", "", str(line or ""))
    line = re.sub(r"\s+", " ", line).strip()
    return line


def _metadata_from_filename(stem: str) -> dict[str, list[dict[str, Any]]]:
    cleaned = re.sub(r"[_]+", " ", str(stem or "")).strip()
    if not cleaned:
        return {}

    candidates: dict[str, list[dict[str, Any]]] = {
        "title": [{"value": cleaned, "source": "filename", "confidence": 0.4}]
    }
    for separator in (" - ", " -- ", " by "):
        if separator not in cleaned:
            continue
        left, right = [part.strip() for part in cleaned.split(separator, 1)]
        if not left or not right:
            continue
        if separator.strip().lower() == "by":
            candidates = {
                "title": [{"value": left, "source": "filename", "confidence": 0.55}],
                "author": [{"value": right, "source": "filename", "confidence": 0.45}],
            }
        else:
            candidates = {
                "title": [{"value": right, "source": "filename", "confidence": 0.45}],
                "author": [{"value": left, "source": "filename", "confidence": 0.35}],
            }
        break
    return candidates


def _front_matter_metadata(blocks: list[SourceBlock]) -> dict[str, list[dict[str, Any]]]:
    short_blocks = [
        block.text
        for block in blocks[:30]
        if 1 <= len(block.text.split()) <= 12 and len(block.text) <= 100
    ]
    if not short_blocks:
        return {}

    candidates: dict[str, list[dict[str, Any]]] = {}
    candidates["title"] = [
        {
            "value": short_blocks[0],
            "source": "front_matter",
            "line": blocks[0].line_start if blocks else 1,
            "confidence": 0.35,
        }
    ]
    if len(short_blocks) > 1:
        candidates["author"] = [
            {
                "value": short_blocks[1],
                "source": "front_matter",
                "confidence": 0.25,
            }
        ]
    return candidates


def _infer_pdf_roles(text: str) -> list[str]:
    roles: list[str] = []
    stripped = text.strip()
    lowered = stripped.lower()
    if re.fullmatch(r"\d{1,4}", stripped):
        roles.append("page_number")
    if len(stripped) <= 140 and len(stripped.split()) <= 16:
        roles.append("heading_candidate")
    if any(token in lowered for token in ("copyright", "all rights reserved", "isbn")):
        roles.append("copyright")
    if re.match(r"^(\[\d+\]|\d{1,3}[\.)]|[*†‡])\s+\S+", stripped):
        roles.append("footnote_candidate")
    return roles
