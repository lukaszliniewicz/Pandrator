"""Geometry-aware PDF ingestion with selective PP-OCRv6 medium OCR."""

from __future__ import annotations

import json
import os
import statistics
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from .models import SourceBlock, SourceDocument
from .pdf_cache import PDF_INGESTION_VERSION as PDF_INGESTION_VERSION
from .pdf_cache import PDFIngestionConfig as PDFIngestionConfig
from .pdf_cache import _load_cached_document as _load_cached_document
from .pdf_cache import _provenance_fingerprint as _provenance_fingerprint
from .pdf_cache import _source_fingerprint as _source_fingerprint
from .pdf_cache import _write_json as _write_json
from .pdf_native_layout import _CHAPTER_RE as _CHAPTER_RE
from .pdf_native_layout import _MAJOR_SECTION_RE as _MAJOR_SECTION_RE
from .pdf_native_layout import _NUMBERED_HEADING_RE as _NUMBERED_HEADING_RE
from .pdf_native_layout import _aligned_column_row_count as _aligned_column_row_count
from .pdf_native_layout import _bbox_area as _bbox_area
from .pdf_native_layout import _coalesce_native_line_fragments as _coalesce_native_line_fragments
from .pdf_native_layout import _column_separators as _column_separators
from .pdf_native_layout import _combined_bbox as _combined_bbox
from .pdf_native_layout import _extract_horizontal_rules as _extract_horizontal_rules
from .pdf_native_layout import _extract_native_lines as _extract_native_lines
from .pdf_native_layout import _geometry_order as _geometry_order
from .pdf_native_layout import (
    _is_explicit_chapter_heading_text as _is_explicit_chapter_heading_text,
)
from .pdf_native_layout import _is_heading_like_line as _is_heading_like_line
from .pdf_native_layout import _is_horizontal_line as _is_horizontal_line
from .pdf_native_layout import _is_major_section_heading_text as _is_major_section_heading_text
from .pdf_native_layout import _is_structural_heading_text as _is_structural_heading_text
from .pdf_native_layout import _join_lines as _join_lines
from .pdf_native_layout import _line_height as _line_height
from .pdf_native_layout import _line_vertical_center as _line_vertical_center
from .pdf_native_layout import _lines_to_blocks as _lines_to_blocks
from .pdf_native_layout import _merge_line_fragments as _merge_line_fragments
from .pdf_native_layout import _native_diagnostics as _native_diagnostics
from .pdf_native_layout import _normalize_space as _normalize_space
from .pdf_native_layout import _order_column_regions as _order_column_regions
from .pdf_native_layout import _partition_column_lines as _partition_column_lines
from .pdf_native_layout import _round_bbox as _round_bbox
from .pdf_native_layout import _round_direction as _round_direction
from .pdf_native_layout import _same_large_heading_run as _same_large_heading_run
from .pdf_native_layout import _same_line_direction as _same_line_direction
from .pdf_native_layout import _same_native_block as _same_native_block
from .pdf_native_layout import _same_paragraph as _same_paragraph
from .pdf_native_layout import _same_visual_row as _same_visual_row
from .pdf_native_layout import _sort_lines_by_visual_rows as _sort_lines_by_visual_rows
from .pdf_native_layout import _transform_direction as _transform_direction
from .pdf_native_layout import _visual_line_sort_key as _visual_line_sort_key
from .pdf_structure import _COPYRIGHT_RE as _COPYRIGHT_RE
from .pdf_structure import _LATIN_LANGUAGE_STOPWORDS as _LATIN_LANGUAGE_STOPWORDS
from .pdf_structure import _NON_NARRATIVE_HEADING_RE as _NON_NARRATIVE_HEADING_RE
from .pdf_structure import _NOTE_PREFIX_RE as _NOTE_PREFIX_RE
from .pdf_structure import _SINGLE_NOTE_MARKER_RE as _SINGLE_NOTE_MARKER_RE
from .pdf_structure import _TOC_HEADING_RE as _TOC_HEADING_RE
from .pdf_structure import _annotate_layout_continuations as _annotate_layout_continuations
from .pdf_structure import _annotate_page_continuations as _annotate_page_continuations
from .pdf_structure import _annotate_structural_roles as _annotate_structural_roles
from .pdf_structure import _boundary_narrative_block as _boundary_narrative_block
from .pdf_structure import _continuation_join_mode as _continuation_join_mode
from .pdf_structure import _dominant_latin_language as _dominant_latin_language
from .pdf_structure import _ends_with_sentence_terminal as _ends_with_sentence_terminal
from .pdf_structure import _ends_with_split_hyphen as _ends_with_split_hyphen
from .pdf_structure import _find_front_boilerplate_pages as _find_front_boilerplate_pages
from .pdf_structure import _find_toc_pages as _find_toc_pages
from .pdf_structure import _first_letter as _first_letter
from .pdf_structure import _footnote_rule_boundaries as _footnote_rule_boundaries
from .pdf_structure import _has_layout_separator_role as _has_layout_separator_role
from .pdf_structure import _has_structural_separator_after as _has_structural_separator_after
from .pdf_structure import _has_structural_separator_before as _has_structural_separator_before
from .pdf_structure import _horizontal_overlap as _horizontal_overlap
from .pdf_structure import _is_content_opener as _is_content_opener
from .pdf_structure import _is_ignorable_layout_separator as _is_ignorable_layout_separator
from .pdf_structure import _is_layout_structural_singleton as _is_layout_structural_singleton
from .pdf_structure import _is_narrative_boundary_block as _is_narrative_boundary_block
from .pdf_structure import _is_probable_page_boundary_header as _is_probable_page_boundary_header
from .pdf_structure import _is_probable_running_header as _is_probable_running_header
from .pdf_structure import _is_structural_page_separator as _is_structural_page_separator
from .pdf_structure import _is_title_followed_by_byline as _is_title_followed_by_byline
from .pdf_structure import _is_toc_heading_text as _is_toc_heading_text
from .pdf_structure import (
    _large_heading_blocks_are_contiguous as _large_heading_blocks_are_contiguous,
)
from .pdf_structure import _large_heading_continuation_ids as _large_heading_continuation_ids
from .pdf_structure import _layout_continuation_details as _layout_continuation_details
from .pdf_structure import _looks_like_byline as _looks_like_byline
from .pdf_structure import _looks_like_toc_continuation as _looks_like_toc_continuation
from .pdf_structure import _matching_horizontal_directions as _matching_horizontal_directions
from .pdf_structure import _non_narrative_page_span as _non_narrative_page_span
from .pdf_structure import _normalized_marginal_key as _normalized_marginal_key
from .pdf_structure import _page_continuation_details as _page_continuation_details
from .pdf_structure import _share_native_text_container as _share_native_text_container
from .pdf_structure import _starts_with_letter as _starts_with_letter
from .pdf_structure import _starts_with_list_or_entry_marker as _starts_with_list_or_entry_marker
from .pdf_structure import (
    _starts_with_lowercase_continuation as _starts_with_lowercase_continuation,
)
from .pdf_structure import _tabular_page_reasons as _tabular_page_reasons
from .pdf_structure import _weighted_median as _weighted_median
from .pdf_text_adapter import _front_matter_metadata, _metadata_from_filename

ProgressCallback = Callable[[str], None]
_LATIN_V6_LANGUAGES = {
    "auto", "latin", "en", "af", "az", "bs", "ca", "cs", "cy", "da", "de", "es",
    "et", "eu", "fi", "fr", "ga", "gl", "hr", "hu", "id", "is", "it", "ku", "la",
    "lb", "lt", "lv", "mi", "ms", "mt", "nl", "no", "oc", "pl", "pt", "qu", "rm",
    "ro", "rs_latin", "sk", "sl", "sq", "sv", "sw", "tl", "tr", "uz", "vi",
    "french", "german", "ch", "chinese_cht", "japan",
}
class PaddleOCRMediumEngine:
    """Lazy CPU ONNX OCR engine. PP-OCRv6 medium is used whenever it supports the script."""

    def __init__(self):
        self._engines: dict[tuple[str, str], Any] = {}

    def recognize(self, page: Any, language: str, dpi: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        import fitz
        import numpy as np

        engine, engine_name = self._get_engine(language)
        pixmap = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
        channels = pixmap.n
        image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, channels)
        if channels == 4:
            image = image[:, :, :3]
        result = next(iter(engine.predict(image)))
        texts = list(result.get("rec_texts") or [])
        scores = list(result.get("rec_scores") or [])
        polygons = list(result.get("rec_polys") or [])
        if not polygons:
            boxes = list(result.get("rec_boxes") or [])
            polygons = [
                [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
                for box in boxes
            ]

        scale_x = float(page.rect.width) / max(1, pixmap.width)
        scale_y = float(page.rect.height) / max(1, pixmap.height)
        lines: list[dict[str, Any]] = []
        for polygon, text, score in zip(polygons, texts, scores, strict=False):
            cleaned = _normalize_space(str(text))
            if not cleaned:
                continue
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
            bbox = [
                min(xs) * scale_x,
                min(ys) * scale_y,
                max(xs) * scale_x,
                max(ys) * scale_y,
            ]
            lines.append(
                {
                    "text": cleaned,
                    "bbox": _round_bbox(bbox),
                    "font_size": round(max(5.0, bbox[3] - bbox[1]) * 0.75, 3),
                    "font": "PP-OCR",
                    "confidence": round(float(score), 4),
                }
            )
        return lines, {
            "engine": engine_name,
            "model": "PP-OCRv6_medium_det + PP-OCRv6_medium_rec"
            if engine_name == "ppocrv6_medium"
            else "PP-OCRv5 language-specific",
            "dpi": dpi,
            "line_count": len(lines),
            "mean_confidence": round(statistics.fmean(line["confidence"] for line in lines), 4)
            if lines
            else 0.0,
        }

    def _get_engine(self, language: str) -> tuple[Any, str]:
        cache_root = str(os.environ.get("XDG_CACHE_HOME") or "").strip()
        if cache_root:
            os.environ.setdefault("PADDLE_PDX_CACHE_HOME", os.path.join(cache_root, "paddlex"))
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        from paddleocr import PaddleOCR

        normalized = str(language or "auto").lower()
        if normalized in _LATIN_V6_LANGUAGES:
            key = ("v6-medium", "shared")
            if key not in self._engines:
                self._engines[key] = PaddleOCR(
                    text_detection_model_name="PP-OCRv6_medium_det",
                    text_recognition_model_name="PP-OCRv6_medium_rec",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    engine="onnxruntime",
                    device="cpu",
                )
            return self._engines[key], "ppocrv6_medium"

        key = ("v5-language", normalized)
        if key not in self._engines:
            self._engines[key] = PaddleOCR(
                lang=normalized,
                ocr_version="PP-OCRv5",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                engine="onnxruntime",
                device="cpu",
            )
        return self._engines[key], "ppocrv5_language"


def build_source_document(
    pdf_path: str,
    config: PDFIngestionConfig | None = None,
    artifact_dir: str | None = None,
    progress_callback: ProgressCallback | None = None,
    ocr_engine: Any | None = None,
) -> SourceDocument:
    import fitz

    resolved = (config or PDFIngestionConfig()).normalized()
    normalized_path = os.path.abspath(pdf_path)
    cache_path = (
        os.path.join(artifact_dir, "source_document.json") if artifact_dir else ""
    )
    source_fingerprint = _source_fingerprint(normalized_path)
    if resolved.use_cache and cache_path:
        cached = _load_cached_document(cache_path, source_fingerprint, resolved)
        if cached is not None:
            _emit(progress_callback, "Using cached structured PDF ingestion.")
            return cached

    _emit(progress_callback, "Inspecting PDF text layers and geometry...")
    document = SourceDocument(
        source_type="pdf_structured",
        source_path=normalized_path,
        filename=os.path.basename(normalized_path),
        metadata_candidates=_metadata_from_filename(
            os.path.splitext(os.path.basename(normalized_path))[0]
        ),
        attributes={
            "pdf_ingestion": {
                "version": PDF_INGESTION_VERSION,
                "config": asdict(resolved),
                "source_fingerprint": source_fingerprint,
                "pages": [],
            }
        },
    )
    provenance_path = f"{normalized_path}.pycroppdf.json"
    if os.path.isfile(provenance_path):
        try:
            with open(provenance_path, "r", encoding="utf-8") as file_handle:
                document.attributes["pycroppdf_provenance"] = json.load(file_handle)
        except (OSError, ValueError) as error:
            document.warnings.append(
                f"Could not read PyCropPDF provenance manifest: {error}"
            )
    engine = ocr_engine or PaddleOCRMediumEngine()
    pdf = fitz.open(normalized_path)
    line_number = 1
    source_index = 0
    try:
        for page_index, page in enumerate(pdf.pages()):
            _emit(
                progress_callback,
                f"Ingesting PDF page {page_index + 1}/{pdf.page_count}...",
            )
            native_lines = _extract_native_lines(page)
            diagnostics = _native_diagnostics(page, native_lines)
            horizontal_rules = _extract_horizontal_rules(page)
            use_ocr = resolved.ocr_mode == "force" or (
                resolved.ocr_mode == "auto" and diagnostics["auto_ocr"]
            )
            source_method = "native"
            ocr_report: dict[str, Any] | None = None
            lines = native_lines
            if use_ocr:
                source_method = "ocr"
                try:
                    _emit(
                        progress_callback,
                        f"Running OCR on PDF page {page_index + 1}/{pdf.page_count}...",
                    )
                    lines, ocr_report = engine.recognize(
                        page, resolved.ocr_language, resolved.ocr_dpi
                    )
                except Exception as error:  # noqa: BLE001 - OCR backends expose varied failures
                    document.warnings.append(
                        f"OCR failed on page {page_index + 1}; retained native extraction: {error}"
                    )
                    source_method = "native_fallback"
                    lines = native_lines
                    ocr_report = {"error": f"{type(error).__name__}: {error}"}

            page_blocks = _lines_to_blocks(lines, page.rect, source_method)
            for page_block_index, payload in enumerate(page_blocks, start=1):
                text = _normalize_space(payload["text"])
                if not text:
                    continue
                source_index += 1
                block = SourceBlock(
                    block_id=f"pdf:{page_index + 1}:{page_block_index}",
                    text=text,
                    line_start=line_number,
                    line_end=line_number,
                    source_index=source_index,
                    page=page_index + 1,
                    tag="p",
                    attributes={
                        "bbox": payload["bbox"],
                        "page_size": [
                            round(float(page.rect.width), 3),
                            round(float(page.rect.height), 3),
                        ],
                        "source_method": source_method,
                        "font_size": payload.get("font_size", 0.0),
                        "fonts": payload.get("fonts", []),
                        "confidence": payload.get("confidence"),
                        "source_lines": payload.get("source_lines", 1),
                        "reading_order": payload.get("reading_order", "top_to_bottom"),
                        "direction": payload.get("direction", [1.0, 0.0]),
                        "native_block_indexes": payload.get(
                            "native_block_indexes", []
                        ),
                        "first_line_bbox": payload.get("first_line_bbox"),
                        "last_line_bbox": payload.get("last_line_bbox"),
                        "role_evidence": {},
                    },
                )
                document.blocks.append(block)
                line_number += 1

            document.attributes["pdf_ingestion"]["pages"].append(
                {
                    "page": page_index + 1,
                    "source_method": source_method,
                    "native_diagnostics": diagnostics,
                    "horizontal_rules": horizontal_rules,
                    "ocr": ocr_report,
                    "block_count": len(page_blocks),
                }
            )
    finally:
        pdf.close()

    _emit(progress_callback, "Analyzing PDF structure and layout...")
    _annotate_structural_roles(document)
    _annotate_layout_continuations(document)
    _annotate_page_continuations(document)
    repeated_marginal_count = sum(
        block.role_score("repeated_marginal") >= 0.95 for block in document.blocks
    )
    page_continuation_count = sum(
        block.role_score("page_continuation") >= 0.92 for block in document.blocks
    )
    layout_continuation_count = sum(
        block.role_score("layout_continuation") >= 0.92 for block in document.blocks
    )
    ocr_page_count = sum(
        page.get("source_method") == "ocr"
        for page in document.attributes["pdf_ingestion"]["pages"]
    )
    recommendations: list[str] = []
    if repeated_marginal_count >= 3:
        recommendations.append(
            "PyCropPDF may improve extraction by removing the repeated marginal region before ingestion."
        )
    if ocr_page_count:
        recommendations.append(
            "PyCropPDF can improve OCR on scans with large borders, gutters, or unwanted marginal content."
        )
    document.attributes["pdf_ingestion"]["summary"] = {
        "page_count": len(document.attributes["pdf_ingestion"]["pages"]),
        "ocr_page_count": ocr_page_count,
        "native_page_count": len(document.attributes["pdf_ingestion"]["pages"])
        - ocr_page_count,
        "block_count": len(document.blocks),
        "repeated_marginal_block_count": repeated_marginal_count,
        "page_continuation_count": page_continuation_count,
        "layout_continuation_count": layout_continuation_count,
        "recommendations": recommendations,
    }
    front_matter = _front_matter_metadata(document.blocks)
    for key, values in front_matter.items():
        document.metadata_candidates.setdefault(key, []).extend(values)
    if cache_path:
        _emit(progress_callback, "Saving structured PDF ingestion cache...")
        os.makedirs(artifact_dir or "", exist_ok=True)
        _write_json(cache_path, document.to_dict())
        _write_json(
            os.path.join(artifact_dir or "", "ingestion_report.json"),
            document.attributes.get("pdf_ingestion", {}),
        )
    return document


def propose_deterministic_operations(
    document: SourceDocument,
    remove_footnotes: bool = False,
    remove_toc: bool = True,
    remove_repeated_marginals: bool = True,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    deletion_groups: list[tuple[str, list[str], float]] = []
    if remove_repeated_marginals:
        repeated = [
            block.block_id
            for block in document.blocks
            if (
                block.role_score("repeated_marginal") >= 0.95
                or block.role_score("running_header") >= 0.98
                or block.role_score("page_number") >= 0.98
            )
        ]
        if repeated:
            deletion_groups.append(("high-confidence repeated margins and page numbers", repeated, 0.98))
        boilerplate = [
            block.block_id for block in document.blocks if block.role_score("boilerplate") >= 0.98
        ]
        if boilerplate:
            deletion_groups.append(("high-confidence front-matter publishing boilerplate", boilerplate, 0.98))
    if remove_toc:
        toc = [block.block_id for block in document.blocks if block.role_score("toc") >= 0.92]
        if toc:
            deletion_groups.append(("high-confidence table of contents", toc, 0.94))
    if remove_footnotes:
        notes = [block.block_id for block in document.blocks if block.role_score("footnote") >= 0.92]
        if notes:
            deletion_groups.append(("high-confidence footnotes", notes, 0.93))
    for reason, block_ids, confidence in deletion_groups:
        operations.append(
            {"op": "delete_blocks", "block_ids": block_ids, "reason": reason, "confidence": confidence}
        )
    chapter_ids = [
        block.block_id for block in document.blocks if block.role_score("deterministic_chapter") >= 0.85
    ]
    for block_id in chapter_ids:
        operations.append(
            {
                "op": "mark_chapter",
                "block_id": block_id,
                "reason": "high-confidence PDF heading",
                "confidence": 0.88,
            }
        )
    return operations


def _emit(callback: ProgressCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
