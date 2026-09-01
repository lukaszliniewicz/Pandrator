"""Geometry-aware PDF ingestion with selective PP-OCRv6 medium OCR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import Any

from .models import SourceBlock, SourceDocument
from .pdf_text_adapter import _front_matter_metadata, _metadata_from_filename

ProgressCallback = Callable[[str], None]
PDF_INGESTION_VERSION = 12
_LATIN_V6_LANGUAGES = {
    "auto", "latin", "en", "af", "az", "bs", "ca", "cs", "cy", "da", "de", "es",
    "et", "eu", "fi", "fr", "ga", "gl", "hr", "hu", "id", "is", "it", "ku", "la",
    "lb", "lt", "lv", "mi", "ms", "mt", "nl", "no", "oc", "pl", "pt", "qu", "rm",
    "ro", "rs_latin", "sk", "sl", "sq", "sv", "sw", "tl", "tr", "uz", "vi",
    "french", "german", "ch", "chinese_cht", "japan",
}
_CHAPTER_RE = re.compile(
    r"^(?:chapter|book|part|volume|section|chapitre|kapitel|capitulo|rozdzia[lł]|cz[eę][sś][cć]|"
    r"tom|ksi[eę]ga|prologue|epilogue|prolog|epilog|wst[eę]p|pos[lł]owie)\s+"
    r"(?:[ivxlcdm]+|\d{1,4})\b",
    re.IGNORECASE,
)
_NUMBERED_HEADING_RE = re.compile(
    # A bare ``I `` is ordinarily a sentence pronoun, and OCR often inserts
    # spaces into page numbers (``1 30``). Both were previously treated as
    # numbered headings. Permit an undelimited form only for Arabic numbers
    # followed by a word, while Roman numerals require an explicit delimiter.
    r"^(?:\d{1,4}(?:[.)]\s+|\s*[-–—]\s+|\s+(?=[^\W\d_])\S+)|"
    r"[ivxlcdm]{1,8}(?:[.)]\s+|\s*[-–—]\s+))",
    re.IGNORECASE,
)
_MAJOR_SECTION_RE = re.compile(
    r"^(?:acknowledg(?:e)?ments?|preface|foreword|introduction|prologue|epilogue|afterword|"
    r"conclusion|appendi(?:x|ces)|postscript|chapter|book|part|volume|section|act|"
    r"pr[eé]face|avant-propos|postface|chapitre|kapitel|vorwort|einleitung|nachwort|"
    r"prolog|epilog|cap[ií]tulo|introducci[oó]n|pr[oó]logo|ep[ií]logo|"
    r"wst[eę]p|przedmowa|pos[lł]owie|podzi[eę]kowania|rozdzia[lł]|cz[eę][sś][cć]|tom|ksi[eę]ga|"
    r"предисловие|введение|послесловие|глава|часть)\b",
    re.IGNORECASE,
)
_NOTE_PREFIX_RE = re.compile(
    r"^(?:\[\d{1,3}\]|\d{1,3}[.)]|[*†‡]|[ivxlcdm]{1,8})\s+\S+",
    re.IGNORECASE,
)
_SINGLE_NOTE_MARKER_RE = re.compile(r"^[*†‡]$")
_TOC_HEADING_RE = re.compile(
    r"\b(?:table of contents|contents|spis tre[sś]ci|sommaire|inhaltsverzeichnis|indice|índice|содержание)\b",
    re.IGNORECASE,
)
_COPYRIGHT_RE = re.compile(
    r"(?:\bcopyright\b|©|\ball rights reserved\b|\bisbn\b|\blibrary of congress\b|"
    r"\bcatalog(?:ue|uing|ing)?\b|\bno part of this publication\b|\bprinted in\b|"
    r"\bfirst published\b|\bpublished by\b)",
    re.IGNORECASE,
)
_NON_NARRATIVE_HEADING_RE = re.compile(
    r"^(?:(?:select\s+)?(?:bibliography|references|works cited|index|glossary|"
    r"notes?|footnotes?|endnotes?|colophon|copyright|list of abbreviations|abbreviations)|"
    r"(?:other\s+)?works\s+(?:by|about)|"
    r"(?:wykaz|spis)\s+skr[oó]t[oó]w|ключи|.*(?:мини[-*])?словар\w*)\b",
    re.IGNORECASE,
)
_LATIN_LANGUAGE_STOPWORDS = {
    "en": {
        "the", "and", "of", "to", "in", "is", "that", "for", "with", "as", "on", "this",
        "it", "be", "by", "from", "at", "or", "an", "are", "not", "which", "but", "we",
    },
    "fr": {
        "le", "la", "les", "de", "des", "du", "et", "en", "un", "une", "que", "qui", "dans",
        "pour", "est", "pas", "sur", "avec", "par", "au", "aux", "ce", "cette", "il", "elle",
    },
    "de": {
        "der", "die", "das", "den", "dem", "des", "und", "oder", "aber", "in", "im", "ist",
        "sind", "mit", "von", "zu", "auf", "für", "nicht", "ein", "eine", "einer", "als", "auch",
    },
}


@dataclass
class PDFIngestionConfig:
    ocr_mode: str = "auto"
    ocr_language: str = "auto"
    ocr_dpi: int = 200
    use_cache: bool = True

    def normalized(self) -> PDFIngestionConfig:
        mode = str(self.ocr_mode or "auto").lower()
        mode = {"always": "force", "never": "off"}.get(mode, mode)
        if mode not in {"auto", "off", "force"}:
            mode = "auto"
        return PDFIngestionConfig(
            ocr_mode=mode,
            ocr_language=str(self.ocr_language or "auto").lower(),
            ocr_dpi=max(120, min(400, int(self.ocr_dpi or 200))),
            use_cache=bool(self.use_cache),
        )


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
        for polygon, text, score in zip(polygons, texts, scores):
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
        for page_index, page in enumerate(pdf):
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


def _extract_native_lines(page: Any) -> list[dict[str, Any]]:
    import fitz

    lines: list[dict[str, Any]] = []
    rotation_matrix = page.rotation_matrix
    for block_index, block in enumerate(page.get_text("dict").get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = _normalize_space(
                "".join(str(span.get("text", "")) for span in spans)
            )
            if not text:
                continue
            char_count = max(1, sum(len(str(span.get("text", ""))) for span in spans))
            bbox = fitz.Rect(line["bbox"])
            if int(page.rotation or 0) % 360:
                bbox *= rotation_matrix
            lines.append(
                {
                    "text": text,
                    "bbox": _round_bbox(bbox),
                    "block_index": block_index,
                    "direction": _transform_direction(
                        line.get("dir") or (1.0, 0.0), rotation_matrix
                    ),
                    "font_size": round(
                        sum(
                            float(span.get("size", 0.0))
                            * len(str(span.get("text", "")))
                            for span in spans
                        )
                        / char_count,
                        3,
                    ),
                    "font": ",".join(
                        sorted(
                            {
                                str(span.get("font", ""))
                                for span in spans
                                if span.get("font")
                            }
                        )
                    ),
                    "confidence": None,
                    "source_lines": 1,
                }
            )
    return lines


def _extract_horizontal_rules(page: Any) -> list[list[float]]:
    """Return thin horizontal vector rules that may delimit footnotes."""
    rules: list[list[float]] = []
    rotation_matrix = page.rotation_matrix
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001 - malformed PDF drawing streams vary
        return rules
    for drawing in drawings:
        for item in drawing.get("items") or []:
            if not item or item[0] != "l" or len(item) < 3:
                continue
            start, end = item[1], item[2]
            if int(page.rotation or 0) % 360:
                start = start * rotation_matrix
                end = end * rotation_matrix
            if abs(float(start.y) - float(end.y)) > 1.0:
                continue
            x0, x1 = sorted((float(start.x), float(end.x)))
            if x1 - x0 < 8.0:
                continue
            rules.append(
                [
                    round(x0, 3),
                    round((float(start.y) + float(end.y)) / 2.0, 3),
                    round(x1, 3),
                ]
            )
    return rules


def _native_diagnostics(page: Any, lines: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(line["text"] for line in lines)
    compact = "".join(text.split())
    alpha_numeric = sum(char.isalnum() for char in compact)
    bad_chars = sum(char == "\ufffd" or unicodedata.category(char) == "Cc" for char in compact)
    one_token_lines = sum(len(line["text"].split()) <= 1 for line in lines)
    auto_ocr = len(compact) < 40 or alpha_numeric < 20
    reasons: list[str] = []
    if auto_ocr:
        reasons.append("too_little_native_text")
    if compact and bad_chars / len(compact) > 0.02:
        auto_ocr = True
        reasons.append("invalid_character_ratio")
    if len(lines) >= 20 and one_token_lines / len(lines) > 0.65:
        auto_ocr = True
        reasons.append("fragmented_native_text")
    image_area = sum(_bbox_area(info.get("bbox", (0, 0, 0, 0))) for info in page.get_image_info())
    page_area = max(1.0, float(page.rect.width * page.rect.height))
    return {
        "chars": len(text),
        "line_count": len(lines),
        "alpha_numeric_ratio": round(alpha_numeric / max(1, len(compact)), 4),
        "bad_character_ratio": round(bad_chars / max(1, len(compact)), 4),
        "image_coverage": round(min(1.0, image_area / page_area), 4),
        "auto_ocr": auto_ocr,
        "decision_reasons": reasons or ["native_text_is_plausible"],
    }


def _lines_to_blocks(
    lines: list[dict[str, Any]], page_rect: Any, source_method: str
) -> list[dict[str, Any]]:
    if not lines:
        return []
    lines = _coalesce_native_line_fragments(lines)
    ordered, reading_order = _geometry_order(lines, float(page_rect.width))
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for line in ordered:
        if current and not _same_paragraph(current[-1], line):
            groups.append(current)
            current = []
        current.append(line)
    if current:
        groups.append(current)
    blocks: list[dict[str, Any]] = []
    for group in groups:
        text = _join_lines(group)
        if not text:
            continue
        confidences = [
            float(line["confidence"])
            for line in group
            if line.get("confidence") is not None
        ]
        blocks.append(
            {
                "text": text,
                "bbox": _combined_bbox(group),
                "font_size": round(
                    statistics.fmean(
                        float(line.get("font_size") or 0.0) for line in group
                    ),
                    3,
                ),
                "fonts": sorted(
                    {str(line.get("font") or "") for line in group if line.get("font")}
                ),
                "confidence": round(statistics.fmean(confidences), 4)
                if confidences
                else None,
                "source_lines": sum(
                    max(1, int(line.get("source_lines") or 1)) for line in group
                ),
                "reading_order": reading_order,
                "source_method": source_method,
                "direction": list(group[0].get("direction") or [1.0, 0.0]),
                "native_block_indexes": sorted(
                    {
                        int(line["block_index"])
                        for line in group
                        if line.get("block_index") is not None
                    }
                ),
                "first_line_bbox": list(group[0]["bbox"]),
                "last_line_bbox": list(group[-1]["bbox"]),
            }
        )
    return blocks


def _coalesce_native_line_fragments(
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild visual rows split into word-sized records by a PDF text layer.

    Some embedded text layers expose every word as a separate ``line`` within
    one native text block. Treating those fragments as independent geometry can
    turn an ordinary justified paragraph into two fictitious columns. Native
    block identity and orientation let us reassemble those rows without joining
    separate real columns. OCR lines deliberately lack block identity and pass
    through unchanged.
    """
    native = [line for line in lines if line.get("block_index") is not None]
    if not native:
        return lines

    rebuilt: list[dict[str, Any]] = []
    passthrough = [line for line in lines if line.get("block_index") is None]
    keys = {
        (int(line["block_index"]), tuple(line.get("direction") or [1.0, 0.0]))
        for line in native
    }
    for block_index, direction in sorted(keys):
        block_lines = [
            line
            for line in native
            if int(line["block_index"]) == block_index
            and tuple(line.get("direction") or [1.0, 0.0]) == direction
        ]
        rows: list[list[dict[str, Any]]] = []
        for line in sorted(block_lines, key=_visual_line_sort_key):
            center = _line_vertical_center(line)
            height = _line_height(line)
            matching_row: list[dict[str, Any]] | None = None
            for row in reversed(rows[-3:]):
                row_center = statistics.fmean(
                    _line_vertical_center(item) for item in row
                )
                row_height = statistics.fmean(_line_height(item) for item in row)
                if abs(center - row_center) <= max(height, row_height) * 0.58:
                    matching_row = row
                    break
            if matching_row is None:
                rows.append([line])
            else:
                matching_row.append(line)

        for row in rows:
            run: list[dict[str, Any]] = []
            for line in sorted(row, key=lambda item: item["bbox"][0]):
                if run:
                    gap = float(line["bbox"][0]) - float(run[-1]["bbox"][2])
                    if gap > max(_line_height(line), _line_height(run[-1])) * 3.0:
                        rebuilt.append(_merge_line_fragments(run))
                        run = []
                run.append(line)
            if run:
                rebuilt.append(_merge_line_fragments(run))
    return rebuilt + passthrough


def _merge_line_fragments(lines: list[dict[str, Any]]) -> dict[str, Any]:
    if len(lines) == 1:
        return dict(lines[0])
    char_counts = [max(1, len(str(line.get("text") or ""))) for line in lines]
    total_chars = sum(char_counts)
    confidences = [
        float(line["confidence"])
        for line in lines
        if line.get("confidence") is not None
    ]
    merged = dict(lines[0])
    merged.update(
        {
            "text": " ".join(
                _normalize_space(line.get("text") or "") for line in lines
            ).strip(),
            "bbox": _combined_bbox(lines),
            "font_size": round(
                sum(
                    float(line.get("font_size") or 0.0) * count
                    for line, count in zip(lines, char_counts)
                )
                / max(1, total_chars),
                3,
            ),
            "font": ",".join(
                sorted(
                    {
                        font
                        for line in lines
                        for font in str(line.get("font") or "").split(",")
                        if font
                    }
                )
            ),
            "confidence": round(statistics.fmean(confidences), 4)
            if confidences
            else None,
            "source_lines": sum(
                max(1, int(line.get("source_lines") or 1)) for line in lines
            ),
        }
    )
    return merged


def _geometry_order(
    lines: list[dict[str, Any]], page_width: float
) -> tuple[list[dict[str, Any]], str]:
    horizontal = [line for line in lines if _is_horizontal_line(line)]
    rotated = [line for line in lines if line not in horizontal]
    if len(horizontal) < 8:
        return _sort_lines_by_visual_rows(horizontal) + _sort_lines_by_visual_rows(
            rotated
        ), "top_to_bottom"
    separators = _column_separators(horizontal, page_width)
    if separators:
        lanes, spanning = _partition_column_lines(horizontal, separators, page_width)
        if (
            all(len(lane) >= 4 for lane in lanes)
            and all(
                _aligned_column_row_count(left, right) >= 3
                for left, right in pairwise(lanes)
            )
            and len(spanning) <= len(horizontal) * 0.45
        ):
            ordered = _order_column_regions(lanes, spanning, page_width)
            ordered += _sort_lines_by_visual_rows(rotated)
            label = "two_columns" if len(lanes) == 2 else "multi_columns"
            return ordered, label
    return _sort_lines_by_visual_rows(horizontal) + _sort_lines_by_visual_rows(
        rotated
    ), "top_to_bottom"


def _column_separators(
    lines: list[dict[str, Any]], page_width: float
) -> list[float]:
    """Find persistent vertical gutters without assuming exactly two columns."""
    tolerance = max(1.0, page_width * 0.006)
    crossing_limit = max(2, int(len(lines) * 0.10))
    samples: list[tuple[float, int]] = []
    for step in range(8, 93):
        position = page_width * step / 100.0
        crossing = sum(
            float(line["bbox"][0]) + tolerance
            < position
            < float(line["bbox"][2]) - tolerance
            for line in lines
        )
        left = sum(float(line["bbox"][2]) <= position + tolerance for line in lines)
        right = sum(float(line["bbox"][0]) >= position - tolerance for line in lines)
        if crossing <= crossing_limit and left >= 4 and right >= 4:
            samples.append((position, crossing))
    if not samples:
        return []

    bands: list[list[tuple[float, int]]] = []
    for sample in samples:
        if bands and sample[0] - bands[-1][-1][0] <= page_width * 0.011:
            bands[-1].append(sample)
        else:
            bands.append([sample])

    candidates: list[tuple[float, int, float]] = []
    for band in bands:
        minimum = min(value for _, value in band)
        best = [position for position, value in band if value == minimum]
        position = statistics.median(best)
        candidates.append((float(position), minimum, band[-1][0] - band[0][0]))

    selected: list[float] = []
    for position, _, _ in sorted(candidates, key=lambda item: item[0]):
        if selected and position - selected[-1] < page_width * 0.09:
            continue
        selected.append(position)
    return selected[:5]


def _partition_column_lines(
    lines: list[dict[str, Any]], separators: list[float], page_width: float
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    tolerance = max(1.0, page_width * 0.012)
    lanes: list[list[dict[str, Any]]] = [[] for _ in range(len(separators) + 1)]
    spanning: list[dict[str, Any]] = []
    for line in lines:
        x0 = float(line["bbox"][0])
        x1 = float(line["bbox"][2])
        crossed = [
            separator
            for separator in separators
            if x0 + tolerance < separator < x1 - tolerance
        ]
        if crossed:
            spanning.append(line)
            continue
        center = (x0 + x1) / 2.0
        lane_index = sum(center > separator for separator in separators)
        lanes[lane_index].append(line)
    return lanes, spanning


def _order_column_regions(
    lanes: list[list[dict[str, Any]]],
    spanning: list[dict[str, Any]],
    page_width: float,
) -> list[dict[str, Any]]:
    """Read column bands around full-width titles and section separators."""
    anchors = [
        line
        for line in spanning
        if (
            float(line["bbox"][2]) - float(line["bbox"][0]) >= page_width * 0.42
            or _is_heading_like_line(line)
        )
    ]
    non_anchors = [line for line in spanning if line not in anchors]
    if non_anchors:
        lane_centers = [
            statistics.median(
                (float(line["bbox"][0]) + float(line["bbox"][2])) / 2.0
                for line in lane
            )
            for lane in lanes
        ]
        for line in non_anchors:
            center = (float(line["bbox"][0]) + float(line["bbox"][2])) / 2.0
            nearest = min(
                range(len(lanes)), key=lambda index: abs(center - lane_centers[index])
            )
            lanes[nearest].append(line)

    remaining = [list(_sort_lines_by_visual_rows(lane)) for lane in lanes]
    ordered: list[dict[str, Any]] = []
    for anchor in _sort_lines_by_visual_rows(anchors):
        anchor_top = float(anchor["bbox"][1])
        for lane in remaining:
            before = [line for line in lane if float(line["bbox"][1]) < anchor_top]
            ordered.extend(before)
            del lane[: len(before)]
        ordered.append(anchor)
    for lane in remaining:
        ordered.extend(lane)
    return ordered


def _aligned_column_row_count(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> int:
    matches = 0
    unused = set(range(len(right)))
    for left_line in sorted(left, key=_visual_line_sort_key):
        left_center = _line_vertical_center(left_line)
        left_height = _line_height(left_line)
        candidate = next(
            (
                index
                for index in sorted(unused)
                if abs(left_center - _line_vertical_center(right[index]))
                <= max(left_height, _line_height(right[index])) * 0.60
            ),
            None,
        )
        if candidate is not None:
            matches += 1
            unused.remove(candidate)
    return matches


def _sort_lines_by_visual_rows(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order fragments on the same visual baseline from left to right.

    Native PDF text dictionaries sometimes represent one printed heading as
    several line records with tiny baseline differences.  Sorting solely by y
    can scramble those fragments before paragraph grouping sees them.
    """
    rows: list[list[dict[str, Any]]] = []
    for line in sorted(lines, key=_visual_line_sort_key):
        center = _line_vertical_center(line)
        height = _line_height(line)
        if rows:
            row = rows[-1]
            row_center = statistics.fmean(_line_vertical_center(item) for item in row)
            row_height = statistics.fmean(_line_height(item) for item in row)
            if (
                _same_native_block(row[-1], line)
                and _same_line_direction(row[-1], line)
                and abs(center - row_center) <= max(height, row_height) * 0.35
            ):
                row.append(line)
                continue
        rows.append([line])
    return [
        line for row in rows for line in sorted(row, key=lambda item: item["bbox"][0])
    ]


def _same_paragraph(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if not _same_line_direction(previous, current):
        return False
    if not _same_native_block(previous, current):
        return _same_large_heading_run(previous, current)
    if _same_visual_row(previous, current):
        return True
    prev_box = previous["bbox"]
    box = current["bbox"]
    height = max(5.0, prev_box[3] - prev_box[1], box[3] - box[1])
    vertical_gap = box[1] - prev_box[3]
    left_gap = abs(box[0] - prev_box[0])
    if _is_heading_like_line(previous) or _is_heading_like_line(current):
        return False
    previous_font = float(previous.get("font_size") or 0.0)
    current_font = float(current.get("font_size") or 0.0)
    if previous_font and current_font:
        font_ratio = max(previous_font, current_font) / max(
            0.1, min(previous_font, current_font)
        )
        if font_ratio >= 1.18 and vertical_gap > height * 0.45:
            return False
    if box[1] < prev_box[1] - height:
        return False
    return vertical_gap <= height * 1.15 and left_gap <= height * 2.5


def _same_visual_row(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if not _same_native_block(previous, current) or not _same_line_direction(
        previous, current
    ):
        return False
    previous_box = previous["bbox"]
    current_box = current["bbox"]
    previous_height = max(1.0, previous_box[3] - previous_box[1])
    current_height = max(1.0, current_box[3] - current_box[1])
    previous_center = (previous_box[1] + previous_box[3]) / 2.0
    current_center = (current_box[1] + current_box[3]) / 2.0
    horizontally_adjacent = (
        current_box[0] <= previous_box[2] + max(previous_height, current_height) * 2.5
    )
    return (
        horizontally_adjacent
        and abs(previous_center - current_center)
        <= max(previous_height, current_height) * 0.35
    )


def _same_native_block(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_index = previous.get("block_index")
    current_index = current.get("block_index")
    return (
        previous_index is None
        or current_index is None
        or previous_index == current_index
    )


def _same_large_heading_run(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_text = _normalize_space(previous.get("text") or "")
    current_text = _normalize_space(current.get("text") or "")
    if (
        not previous_text
        or not current_text
        or len(previous_text) > 160
        or len(current_text) > 160
        or len(previous_text.split()) > 18
        or len(current_text.split()) > 18
    ):
        return False
    previous_font = float(previous.get("font_size") or 0.0)
    current_font = float(current.get("font_size") or 0.0)
    if min(previous_font, current_font) < 14.0:
        return False
    if (
        max(previous_font, current_font) / max(0.1, min(previous_font, current_font))
        > 1.18
    ):
        return False
    previous_box = previous["bbox"]
    current_box = current["bbox"]
    height = max(_line_height(previous), _line_height(current))
    vertical_gap = float(current_box[1]) - float(previous_box[3])
    if not -height * 0.10 <= vertical_gap <= height * 0.55:
        return False
    previous_center = (float(previous_box[0]) + float(previous_box[2])) / 2.0
    current_center = (float(current_box[0]) + float(current_box[2])) / 2.0
    return abs(previous_center - current_center) <= height * 1.75


def _same_line_direction(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_direction = tuple(previous.get("direction") or [1.0, 0.0])
    current_direction = tuple(current.get("direction") or [1.0, 0.0])
    return all(
        abs(float(left) - float(right)) <= 0.05
        for left, right in zip(previous_direction, current_direction)
    )


def _is_horizontal_line(line: dict[str, Any]) -> bool:
    direction = line.get("direction") or [1.0, 0.0]
    return abs(float(direction[0])) >= 0.90 and abs(float(direction[1])) <= 0.20


def _visual_line_sort_key(line: dict[str, Any]) -> tuple[float, float]:
    return (_line_vertical_center(line), float(line["bbox"][0]))


def _line_vertical_center(line: dict[str, Any]) -> float:
    return (float(line["bbox"][1]) + float(line["bbox"][3])) / 2.0


def _line_height(line: dict[str, Any]) -> float:
    return max(1.0, float(line["bbox"][3]) - float(line["bbox"][1]))


def _round_direction(direction: Iterable[float]) -> list[float]:
    values = list(direction)
    if len(values) < 2:
        return [1.0, 0.0]
    return [round(float(values[0]), 4), round(float(values[1]), 4)]


def _transform_direction(direction: Iterable[float], matrix: Any) -> list[float]:
    values = list(direction)
    if len(values) < 2:
        values = [1.0, 0.0]
    transformed_x = float(matrix.a) * float(values[0]) + float(matrix.c) * float(
        values[1]
    )
    transformed_y = float(matrix.b) * float(values[0]) + float(matrix.d) * float(
        values[1]
    )
    length = max(1e-9, (transformed_x**2 + transformed_y**2) ** 0.5)
    return _round_direction((transformed_x / length, transformed_y / length))


def _is_heading_like_line(line: dict[str, Any]) -> bool:
    if _is_structural_heading_text(_normalize_space(line.get("text") or "")):
        return True
    text = _normalize_space(line.get("text") or "")
    if not text or len(text) > 180 or len(text.split()) > 18:
        return False
    return bool(
        _is_explicit_chapter_heading_text(text)
        or _NUMBERED_HEADING_RE.match(text)
        or _is_major_section_heading_text(text)
        or (len(text) >= 4 and text.isupper() and re.search(r"[^\W\d_]", text))
    )


def _is_structural_heading_text(text: str) -> bool:
    normalized = _normalize_space(text)
    if not normalized or len(normalized) > 180 or len(normalized.split()) > 18:
        return False
    return bool(
        _is_explicit_chapter_heading_text(normalized)
        or _NUMBERED_HEADING_RE.match(normalized)
        or _is_major_section_heading_text(normalized)
    )


def _is_explicit_chapter_heading_text(text: str) -> bool:
    """Accept a chapter label, not an ordinary sentence that cites a chapter."""
    normalized = _normalize_space(text)
    match = _CHAPTER_RE.match(normalized)
    if not match:
        return False
    remainder = normalized[match.end() :].strip()
    return bool(
        not remainder
        or remainder[0] in ".:;,-–—"
        or normalized.isupper()
    )


def _is_major_section_heading_text(text: str) -> bool:
    normalized = _normalize_space(text)
    if not normalized or not normalized[0].isupper():
        return False
    match = _MAJOR_SECTION_RE.match(normalized)
    if not match:
        return False
    if _CHAPTER_RE.match(normalized):
        return _is_explicit_chapter_heading_text(normalized)
    remainder = normalized[match.end() :].strip()
    return bool(
        not remainder
        or remainder[0] in ".:;,-–—"
        or normalized.isupper()
    )


def _join_lines(lines: list[dict[str, Any]]) -> str:
    text = ""
    for line in lines:
        current = _normalize_space(line["text"])
        if not current:
            continue
        if text and re.search(r"[\w\u00c0-\u024f]-$", text) and re.match(r"^[a-z\u00df-\u024f]", current):
            text = text[:-1] + current
        else:
            text = f"{text} {current}".strip()
    return text


def _annotate_structural_roles(document: SourceDocument) -> None:
    if not document.blocks:
        return
    font_samples = [
        (
            float(block.attributes.get("font_size") or 0.0),
            max(1, int(block.attributes.get("source_lines") or 1)),
        )
        for block in document.blocks
        if float(block.attributes.get("font_size") or 0.0) > 0
    ]
    body_font = _weighted_median(font_samples) if font_samples else 10.0
    page_count = max((block.page or 0 for block in document.blocks), default=1)
    blocks_by_page: dict[int, list[SourceBlock]] = defaultdict(list)
    for block in document.blocks:
        if block.page:
            blocks_by_page[block.page].append(block)
    horizontal_blocks_by_page = {
        page: sum(
            abs(float((block.attributes.get("direction") or [1.0, 0.0])[0])) >= 0.90
            and abs(float((block.attributes.get("direction") or [1.0, 0.0])[1])) <= 0.20
            for block in blocks
        )
        for page, blocks in blocks_by_page.items()
    }
    tabular_pages = _tabular_page_reasons(blocks_by_page)
    footnote_rule_boundaries = _footnote_rule_boundaries(
        document, blocks_by_page, body_font, set(tabular_pages)
    )
    large_heading_continuation_ids = _large_heading_continuation_ids(
        blocks_by_page, body_font
    )
    chapter_outline_pages = {
        page
        for page, blocks in blocks_by_page.items()
        if sum(
            len(block.text) <= 160
            and len(block.text.split()) <= 18
            and _is_explicit_chapter_heading_text(block.text)
            for block in blocks
        )
        >= 3
    }

    marginal_occurrences: dict[str, list[SourceBlock]] = defaultdict(list)
    structural_marginal_occurrences: dict[str, list[SourceBlock]] = defaultdict(list)
    for block in document.blocks:
        bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
        page_size = block.attributes.get("page_size") or [1, 1]
        y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
        y1 = float(bbox[3]) / max(1.0, float(page_size[1]))
        if y1 <= 0.16 or y0 >= 0.84:
            key = _normalized_marginal_key(block.text)
            font_size = float(block.attributes.get("font_size") or body_font)
            if key and not _is_structural_heading_text(block.text):
                marginal_occurrences[key].append(block)
            elif key and font_size <= body_font * 1.10:
                exact_key = re.sub(
                    r"[^\w]+", "", _normalize_space(block.text).casefold()
                )
                if exact_key:
                    structural_marginal_occurrences[exact_key].append(block)
    repeated_threshold = max(3, min(8, int(page_count * 0.15) or 3))
    repeated_ids = {
        block.block_id
        for blocks in marginal_occurrences.values()
        if len({block.page for block in blocks}) >= repeated_threshold
        for block in blocks
    }
    for blocks in structural_marginal_occurrences.values():
        previous_page = -100
        for block in sorted(
            blocks, key=lambda item: (item.page or 0, item.source_index)
        ):
            page = block.page or 0
            if page - previous_page <= 6:
                repeated_ids.add(block.block_id)
            previous_page = page

    front_limit = max(5, min(30, int(page_count * 0.2) + 1))
    toc_pages = _find_toc_pages(blocks_by_page, front_limit)
    boilerplate_pages = _find_front_boilerplate_pages(blocks_by_page, page_count)
    for block in document.blocks:
        evidence: dict[str, dict[str, Any]] = {}
        text = block.text.strip()
        bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
        page_size = block.attributes.get("page_size") or [1, 1]
        x0 = float(bbox[0]) / max(1.0, float(page_size[0]))
        x1 = float(bbox[2]) / max(1.0, float(page_size[0]))
        y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
        y1 = float(bbox[3]) / max(1.0, float(page_size[1]))
        direction = block.attributes.get("direction") or [1.0, 0.0]
        rotated_edge_marginal = bool(
            (abs(float(direction[0])) < 0.90 or abs(float(direction[1])) > 0.20)
            and (x1 <= 0.10 or x0 >= 0.90)
            and horizontal_blocks_by_page.get(block.page or 0, 0) >= 1
        )
        normalized_text = _normalize_space(text)
        marginal_url = bool(
            (y1 <= 0.10 or y0 >= 0.90)
            and len(normalized_text) <= 160
            and re.match(r"^(?:https?://|www\.)\S+$", normalized_text, re.IGNORECASE)
        )
        edge_glyph_artifact = bool(
            len(normalized_text) <= 2
            and not any(char.isalnum() for char in normalized_text)
            and (x1 <= 0.08 or x0 >= 0.92 or y1 <= 0.06 or y0 >= 0.94)
            and horizontal_blocks_by_page.get(block.page or 0, 0) >= 4
        )
        font_size = float(block.attributes.get("font_size") or body_font)
        font_ratio = font_size / max(1.0, body_font)
        short = len(text) <= 160 and len(text.split()) <= 18
        page_blocks = blocks_by_page.get(block.page or 0, [])
        is_toc_page = (block.page or 0) in toc_pages
        is_content_opener = _is_content_opener(block, page_blocks, body_font)
        is_title_heading = _is_title_followed_by_byline(block, page_blocks, body_font)
        is_running_header = _is_probable_running_header(
            text, short, y1, font_size, body_font
        )
        note_marker = bool(
            _NOTE_PREFIX_RE.match(text) or _SINGLE_NOTE_MARKER_RE.fullmatch(text)
        )
        bottom_plain_note_marker = bool(
            y0 >= 0.72 and re.match(r"^\d{1,3}\s+\S", text)
        )
        toc_like = bool(re.search(r"\.{3,}\s*\d{1,4}$", text)) or (
            short
            and not _is_structural_heading_text(text)
            and bool(re.search(r"\s+\d{1,4}$", text))
        )
        is_toc_candidate = bool(
            not is_toc_page
            and (block.page or 1) <= front_limit
            and (toc_like or _is_toc_heading_text(text))
        )
        footnote_reasons: list[str] = []
        footnote_score = 0.0
        if y0 >= 0.80:
            footnote_score += 0.45
            footnote_reasons.append("bottom_page_region")
        elif y0 >= 0.72:
            footnote_score += 0.30
            footnote_reasons.append("lower_page_region")
        if font_size <= body_font * 0.90:
            footnote_score += 0.3
            footnote_reasons.append("smaller_than_body_font")
        if note_marker or bottom_plain_note_marker:
            footnote_score += 0.35
            footnote_reasons.append(
                "bottom_plain_note_marker"
                if bottom_plain_note_marker and not note_marker
                else "note_marker_prefix"
            )
        likely_footnote = footnote_score >= 0.6
        footnote_rule_y = footnote_rule_boundaries.get(block.page or 0)
        if footnote_rule_y is not None and y0 >= footnote_rule_y:
            footnote_score = max(footnote_score, 0.92)
            footnote_reasons.append("below_horizontal_footnote_rule")
            likely_footnote = True

        page_number_shape = bool(
            re.fullmatch(
                r"(?:\d{1,4}|[ivxlcdm]{1,8}|[1il|]\s+\d{2,4})",
                text,
                re.IGNORECASE,
            )
        )
        # OCR and embedded subset fonts sometimes make a folio appear slightly
        # larger than the surrounding body even though it is physically tiny.
        page_number_size = font_size <= body_font * 1.35
        if page_number_shape and page_number_size and (y1 <= 0.18 or y0 >= 0.80):
            evidence["page_number"] = {
                "score": 0.99,
                "reasons": ["numeric_or_roman", "marginal_position"],
            }
        if rotated_edge_marginal:
            evidence["repeated_marginal"] = {
                "score": 0.99,
                "reasons": ["rotated_text", "outer_page_edge"],
            }
        elif marginal_url:
            evidence["repeated_marginal"] = {
                "score": 0.98,
                "reasons": ["standalone_url", "marginal_position"],
            }
        elif edge_glyph_artifact:
            evidence["repeated_marginal"] = {
                "score": 0.98,
                "reasons": ["isolated_non_alphanumeric_glyph", "outer_page_edge"],
            }
        elif block.block_id in repeated_ids:
            evidence["repeated_marginal"] = {
                "score": 0.98,
                "reasons": [
                    "normalized_text_repeats_across_pages",
                    "consistent_marginal_position",
                ],
            }
        if is_running_header:
            evidence["running_header"] = {
                "score": 0.98,
                "reasons": [
                    "top_marginal_position",
                    "smaller_than_body_font",
                    "variable_header_shape",
                ],
            }
        if is_title_heading:
            evidence["title_heading"] = {
                "score": 0.96,
                "reasons": ["large_front_matter_text", "followed_by_smaller_byline"],
            }
        if (block.page or 0) in boilerplate_pages:
            reasons = ["front_matter_page_with_multiple_publishing_signals"]
            if _COPYRIGHT_RE.search(text):
                reasons.insert(0, "explicit_publishing_or_copyright_signal")
            evidence["boilerplate"] = {"score": 0.98, "reasons": reasons}

        heading_score = 0.0
        heading_reasons: list[str] = []
        explicit_chapter = (
            short and not likely_footnote and _is_explicit_chapter_heading_text(text)
        )
        is_chapter_outline_entry = (
            explicit_chapter and (block.page or 0) in chapter_outline_pages
        )
        numbered_heading = (
            short and not likely_footnote and bool(_NUMBERED_HEADING_RE.match(text))
        )
        major_section = short and _is_major_section_heading_text(text)
        non_narrative_section = short and bool(_NON_NARRATIVE_HEADING_RE.match(text))
        if short and font_ratio >= 1.45:
            heading_score += 0.65
            heading_reasons.append("substantially_larger_than_body_font")
        elif short and font_ratio >= 1.18:
            heading_score += 0.45
            heading_reasons.append("larger_than_body_font")
        if explicit_chapter:
            heading_score += 0.75
            heading_reasons.append("explicit_numbered_chapter")
        elif numbered_heading:
            heading_score += 0.45
            heading_reasons.append("numbered_heading_with_safe_delimiter")
        if major_section:
            heading_score += 0.55
            heading_reasons.append("named_major_section")
        if short and text.isupper() and len(text) >= 4:
            heading_score += 0.20
            heading_reasons.append("all_caps")
        if short and y0 < 0.35:
            heading_score += 0.10
            heading_reasons.append("upper_page_position")
        if short and is_content_opener:
            heading_score += 0.15
            heading_reasons.append("opens_substantial_page_content")
        if numbered_heading and text.isupper() and is_content_opener:
            heading_score += 0.10
            heading_reasons.append("isolated_uppercase_numbered_heading")
        if heading_score >= 0.45:
            evidence["heading"] = {
                "score": min(0.99, heading_score),
                "reasons": heading_reasons,
            }
        if non_narrative_section:
            evidence["non_narrative_section"] = {
                "score": 0.90,
                "reasons": ["bibliographic_or_note_section_heading"],
            }
        if is_chapter_outline_entry:
            evidence["chapter_outline"] = {
                "score": 0.95,
                "reasons": ["several_chapter_labels_on_same_page"],
            }
        if (
            heading_score >= 0.85
            and not evidence.get("repeated_marginal")
            and not evidence.get("running_header")
            and not evidence.get("title_heading")
            and not evidence.get("boilerplate")
            and not evidence.get("page_number")
            and not likely_footnote
            and not is_chapter_outline_entry
            and not is_toc_page
            and not is_toc_candidate
            and not non_narrative_section
            and block.block_id not in large_heading_continuation_ids
            and (
                explicit_chapter
                or major_section
                or (
                    is_content_opener
                    and (
                        numbered_heading
                        or (
                            font_ratio >= 1.45 and len(text) >= 4 and text[:1].isupper()
                        )
                    )
                )
            )
        ):
            evidence["deterministic_chapter"] = {
                "score": min(0.96, heading_score),
                "reasons": heading_reasons + ["global_pdf_heading_policy"],
            }

        if likely_footnote and "page_number" not in evidence:
            evidence["footnote"] = {
                "score": min(0.98, footnote_score),
                "reasons": footnote_reasons,
            }

        tabular_reasons = tabular_pages.get(block.page or 0)
        if tabular_reasons:
            evidence["tabular_region"] = {
                "score": 0.90,
                "reasons": tabular_reasons,
            }

        if is_toc_page:
            evidence["toc"] = {
                "score": 0.94,
                "reasons": ["front_matter", "toc_heading_or_continuation_page"],
            }
        elif is_toc_candidate:
            evidence["toc_candidate"] = {
                "score": 0.7 if toc_like else 0.85,
                "reasons": [
                    "front_matter",
                    "toc_entry_shape" if toc_like else "toc_heading",
                ],
            }
        block.attributes["role_evidence"] = evidence
        block.role_candidates = sorted(set(block.role_candidates + list(evidence)))


def _tabular_page_reasons(
    blocks_by_page: dict[int, list[SourceBlock]],
) -> dict[int, list[str]]:
    """Identify dense tables, dictionaries, indexes, and multi-column notes.

    The role is preservation-only: it blocks speculative paragraph reflow but
    never deletes content.
    """
    result: dict[int, list[str]] = {}
    for page, blocks in blocks_by_page.items():
        substantive = [block for block in blocks if block.text.strip()]
        if len(substantive) < 12:
            continue
        word_counts = [len(block.text.split()) for block in substantive]
        short_fraction = sum(count <= 8 for count in word_counts) / len(word_counts)
        source_lines = [
            max(1, int(block.attributes.get("source_lines") or 1))
            for block in substantive
        ]
        wide_fraction = sum(
            (
                float((block.attributes.get("bbox") or [0, 0, 0, 0])[2])
                - float((block.attributes.get("bbox") or [0, 0, 0, 0])[0])
            )
            / max(
                1.0,
                float((block.attributes.get("page_size") or [1.0, 1.0])[0]),
            )
            >= 0.45
            for block in substantive
        ) / len(substantive)
        left_slot_counts = Counter(
            round(
                float((block.attributes.get("bbox") or [0, 0, 0, 0])[0])
                / max(
                    1.0,
                    float(
                        (block.attributes.get("page_size") or [1.0, 1.0])[0]
                    ),
                ),
                2,
            )
            for block in substantive
        )
        left_slots = set(left_slot_counts)
        repeated_left_slots = sum(count >= 3 for count in left_slot_counts.values())
        reasons: list[str] = []
        if any(
            block.attributes.get("reading_order") == "multi_columns"
            for block in substantive
        ):
            reasons.append("three_or_more_text_columns")
        if (
            len(substantive) >= 18
            and short_fraction >= 0.62
            and statistics.median(source_lines) <= 2
            and repeated_left_slots >= 2
            and wide_fraction <= 0.55
        ):
            reasons.append("dense_short_aligned_records")
        if (
            len(substantive) >= 24
            and short_fraction >= 0.50
            and len(left_slots) >= 4
            and wide_fraction <= 0.55
        ):
            reasons.append("many_repeated_record_columns")
        if reasons:
            result[page] = reasons
    return result


def _footnote_rule_boundaries(
    document: SourceDocument,
    blocks_by_page: dict[int, list[SourceBlock]],
    body_font: float,
    tabular_pages: set[int],
) -> dict[int, float]:
    """Locate conventional short rules separating lower-page footnote areas."""
    page_records = {
        int(record.get("page") or 0): record
        for record in document.attributes.get("pdf_ingestion", {}).get("pages", [])
        if isinstance(record, dict)
    }
    result: dict[int, float] = {}
    for page, blocks in blocks_by_page.items():
        if page in tabular_pages or not blocks:
            continue
        page_size = blocks[0].attributes.get("page_size") or [1.0, 1.0]
        width = max(1.0, float(page_size[0]))
        height = max(1.0, float(page_size[1]))
        candidates: list[float] = []
        for rule in page_records.get(page, {}).get("horizontal_rules") or []:
            if not isinstance(rule, list) or len(rule) < 3:
                continue
            x0, y, x1 = map(float, rule[:3])
            width_ratio = (x1 - x0) / width
            y_ratio = y / height
            if not (0.06 <= width_ratio <= 0.38 and 0.45 <= y_ratio <= 0.90):
                continue
            if x0 / width > 0.42:
                continue
            below = [
                block
                for block in blocks
                if float((block.attributes.get("bbox") or [0, 0, 0, 0])[1])
                >= y + 1.0
            ]
            if not below:
                continue
            below_fonts = [
                float(block.attributes.get("font_size") or 0.0)
                for block in below
                if float(block.attributes.get("font_size") or 0.0) > 0
            ]
            if below_fonts and statistics.median(below_fonts) > body_font * 1.08:
                continue
            candidates.append(y_ratio)
        if candidates:
            result[page] = min(candidates)
    return result


def _annotate_layout_continuations(document: SourceDocument) -> None:
    """Persist auditable same-page paragraph seams before cleanup is applied."""
    blocks_by_page: dict[int, list[SourceBlock]] = defaultdict(list)
    for block in document.blocks:
        if block.page:
            blocks_by_page[block.page].append(block)
    non_narrative_pages = _non_narrative_page_span(blocks_by_page)
    for page, blocks in blocks_by_page.items():
        previous: SourceBlock | None = None
        for current in blocks:
            if _is_ignorable_layout_separator(current):
                continue
            if previous is not None and page not in non_narrative_pages:
                continuation = _layout_continuation_details(previous, current)
                if continuation is not None:
                    mode, score, reasons = continuation
                    evidence = current.attributes.setdefault("role_evidence", {})
                    evidence["layout_continuation"] = {
                        "score": score,
                        "reasons": reasons,
                    }
                    current.attributes["layout_continuation_from_block_id"] = (
                        previous.block_id
                    )
                    current.attributes["layout_continuation_join"] = mode
                    current.role_candidates = sorted(
                        set(current.role_candidates + ["layout_continuation"])
                    )
            previous = current


def _is_ignorable_layout_separator(block: SourceBlock) -> bool:
    return any(
        block.role_score(role) >= score
        for role, score in (
            ("repeated_marginal", 0.95),
            ("running_header", 0.98),
            ("page_number", 0.98),
        )
    )


def _layout_continuation_details(
    previous: SourceBlock, current: SourceBlock
) -> tuple[str, float, list[str]] | None:
    if previous.page != current.page:
        return None
    if previous.attributes.get("source_method") != current.attributes.get(
        "source_method"
    ):
        return None
    if previous.attributes.get("reading_order") != current.attributes.get(
        "reading_order"
    ):
        return None
    if not _matching_horizontal_directions(previous, current):
        return None
    if _has_layout_separator_role(previous) or _has_layout_separator_role(current):
        return None

    same_native_container = _share_native_text_container(previous, current)
    tabular = (
        previous.role_score("tabular_region") >= 0.90
        or current.role_score("tabular_region") >= 0.90
    )
    if tabular:
        return None

    previous_box = previous.attributes.get("bbox")
    current_box = current.attributes.get("bbox")
    previous_line = previous.attributes.get("last_line_bbox") or previous_box
    current_line = current.attributes.get("first_line_bbox") or current_box
    previous_size = previous.attributes.get("page_size")
    current_size = current.attributes.get("page_size")
    if (
        previous_box is None
        or current_box is None
        or previous_line is None
        or current_line is None
        or previous_size is None
        or current_size is None
    ):
        return None

    page_width = max(float(previous_size[0]), float(current_size[0]), 1.0)
    previous_font = float(previous.attributes.get("font_size") or 0.0)
    current_font = float(current.attributes.get("font_size") or 0.0)
    if previous_font and current_font:
        font_ratio = max(previous_font, current_font) / max(
            0.1, min(previous_font, current_font)
        )
        if font_ratio > 1.25:
            return None
    line_height = max(previous_font, current_font, 5.0) * 1.35
    vertical_gap = float(current_line[1]) - float(previous_line[3])
    if vertical_gap < -line_height * 0.25 or vertical_gap > line_height * 1.80:
        return None
    if not same_native_container and vertical_gap > line_height * 0.55:
        return None
    left_delta = abs(float(current_line[0]) - float(previous_box[0]))
    if left_delta > max(page_width * 0.08, line_height * 2.5):
        return None
    if _horizontal_overlap(previous_box, current_box) < 0.55:
        return None

    current_first_indent = float(current_line[0]) - float(current_box[0])
    if current_first_indent > max(page_width * 0.008, current_font * 0.55, 4.0):
        return None
    if (
        not same_native_container
        and (float(current_box[2]) - float(current_box[0])) / page_width >= 0.80
    ):
        return None

    left = previous.text.rstrip()
    right = current.text.lstrip()
    if not left or not right or _ends_with_sentence_terminal(left):
        return None
    if _is_layout_structural_singleton(previous) or _is_layout_structural_singleton(
        current
    ):
        return None
    hyphenated = _ends_with_split_hyphen(left) and _starts_with_letter(right)
    lowercase = _starts_with_lowercase_continuation(right)
    full_previous_line = float(previous_line[2]) >= float(previous_box[2]) - max(
        line_height * 2.5, page_width * 0.03
    )
    strong_geometry = bool(
        full_previous_line
        and vertical_gap <= line_height * 1.30
        and len(left.split()) >= 8
        and not _starts_with_list_or_entry_marker(right)
    )
    if not hyphenated and not lowercase and not (same_native_container and strong_geometry):
        return None
    if (
        not hyphenated
        and not same_native_container
        and int(previous.attributes.get("source_lines") or 1) <= 2
        and int(current.attributes.get("source_lines") or 1) <= 2
        and len(left.split()) <= 12
        and len(right.split()) <= 12
    ):
        return None
    if (
        not hyphenated
        and not same_native_container
        and int(previous.attributes.get("source_lines") or 1) <= 1
        and len(left.split()) < 8
    ):
        return None

    reasons = [
        "same_page",
        "matching_source_method_layout_and_direction",
        "adjacent_visual_lines",
        "compatible_typography_and_margins",
    ]
    if same_native_container:
        reasons.append("shared_native_text_container")
    if hyphenated:
        reasons.append("hyphenated_seam")
    elif lowercase:
        reasons.append("lowercase_continuation")
    else:
        reasons.append("full_line_geometric_continuation")
    mode = _continuation_join_mode(left, same_native_container)
    return mode, (0.96 if same_native_container else 0.93), reasons


def _has_layout_separator_role(block: SourceBlock) -> bool:
    return any(
        block.role_score(role) >= score
        for role, score in (
            ("deterministic_chapter", 0.85),
            ("footnote", 0.60),
            ("heading", 0.45),
            ("title_heading", 0.95),
            ("toc", 0.92),
            ("toc_candidate", 0.70),
            ("non_narrative_section", 0.85),
            ("metadata", 0.90),
            ("boilerplate", 0.98),
        )
    )


def _is_layout_structural_singleton(block: SourceBlock) -> bool:
    """Reject terse metadata and unrecognized headings at a paragraph seam."""
    text = _normalize_space(block.text)
    if not text:
        return True
    source_lines = int(block.attributes.get("source_lines") or 1)
    bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
    page_size = block.attributes.get("page_size") or [1, 1]
    top = float(bbox[1]) / max(1.0, float(page_size[1]))
    if top <= 0.09 and source_lines <= 2 and len(text.split()) <= 5:
        return True
    if source_lines > 1:
        return False
    if "@" in text and re.fullmatch(r"\S+@\S+", text):
        return True
    letters = [char for char in text if char.isalpha()]
    return bool(
        letters
        and len(text.split()) <= 14
        and all(char.isupper() for char in letters)
    )


def _matching_horizontal_directions(
    previous: SourceBlock, current: SourceBlock
) -> bool:
    previous_direction = previous.attributes.get("direction") or [1.0, 0.0]
    current_direction = current.attributes.get("direction") or [1.0, 0.0]
    return bool(
        abs(float(previous_direction[0])) >= 0.90
        and abs(float(previous_direction[1])) <= 0.20
        and all(
            abs(float(left) - float(right)) <= 0.05
            for left, right in zip(previous_direction, current_direction)
        )
    )


def _share_native_text_container(
    previous: SourceBlock, current: SourceBlock
) -> bool:
    previous_indexes = {
        int(value) for value in previous.attributes.get("native_block_indexes") or []
    }
    current_indexes = {
        int(value) for value in current.attributes.get("native_block_indexes") or []
    }
    return bool(previous_indexes and current_indexes and previous_indexes & current_indexes)


def _horizontal_overlap(previous_box: list[Any], current_box: list[Any]) -> float:
    previous_width = max(1.0, float(previous_box[2]) - float(previous_box[0]))
    current_width = max(1.0, float(current_box[2]) - float(current_box[0]))
    overlap = max(
        0.0,
        min(float(previous_box[2]), float(current_box[2]))
        - max(float(previous_box[0]), float(current_box[0])),
    )
    return overlap / min(previous_width, current_width)


def _starts_with_list_or_entry_marker(text: str) -> bool:
    return bool(
        re.match(
            r"^[\s\"'“‘(\[]*(?:[-–—•▪◦*†‡]|\d{1,4}[.)]|[a-z][.)]\s)",
            text,
            re.IGNORECASE,
        )
    )


def _continuation_join_mode(text: str, remove_printed_hyphen: bool) -> str:
    stripped = text.rstrip()
    if stripped.endswith("\u00ad"):
        return "remove_hyphen"
    if stripped.endswith("-"):
        return "remove_hyphen" if remove_printed_hyphen else "keep_hyphen"
    return "space"


def _annotate_page_continuations(document: SourceDocument) -> None:
    """Mark only high-confidence narrative continuations across adjacent pages.

    PDF layout normally creates a new extraction block at every page boundary.
    Keeping the source blocks separate preserves page-level provenance for review,
    while the annotation lets the cleaned-text writer reflow safe continuations.
    """
    blocks_by_page: dict[int, list[SourceBlock]] = defaultdict(list)
    for block in document.blocks:
        if block.page:
            blocks_by_page[block.page].append(block)

    non_narrative_pages = _non_narrative_page_span(blocks_by_page)
    pages = sorted(blocks_by_page)
    for previous_page, current_page in pairwise(pages):
        if current_page != previous_page + 1:
            continue
        if previous_page in non_narrative_pages or current_page in non_narrative_pages:
            continue
        previous = _boundary_narrative_block(blocks_by_page[previous_page], reverse=True)
        current = _boundary_narrative_block(blocks_by_page[current_page])
        if previous is None or current is None:
            continue
        if _has_structural_separator_after(blocks_by_page[previous_page], previous):
            continue
        if _has_structural_separator_before(blocks_by_page[current_page], current):
            continue
        continuation = _page_continuation_details(previous, current)
        if continuation is None:
            continue
        mode, reasons = continuation
        evidence = current.attributes.setdefault("role_evidence", {})
        evidence["page_continuation"] = {"score": 0.96, "reasons": reasons}
        current.attributes["continuation_from_block_id"] = previous.block_id
        current.attributes["continuation_join"] = mode
        current.role_candidates = sorted(set(current.role_candidates + ["page_continuation"]))


def _non_narrative_page_span(blocks_by_page: dict[int, list[SourceBlock]]) -> set[int]:
    """Track end-matter/list sections so their entries are never reflowed as prose."""
    active = False
    pages: set[int] = set()
    for page in sorted(blocks_by_page):
        for block in blocks_by_page[page]:
            if block.role_score("toc") >= 0.92:
                continue
            if block.role_score("deterministic_chapter") >= 0.85:
                active = False
            if block.role_score("non_narrative_section") >= 0.85:
                active = True
        if active:
            pages.add(page)
    return pages


def _boundary_narrative_block(blocks: list[SourceBlock], reverse: bool = False) -> SourceBlock | None:
    ordered = list(reversed(blocks)) if reverse else blocks
    for index, block in enumerate(ordered):
        if _is_narrative_boundary_block(block):
            if not reverse and _is_probable_page_boundary_header(
                block, ordered[index + 1 :]
            ):
                continue
            return block
    return None


def _is_narrative_boundary_block(block: SourceBlock) -> bool:
    if len(block.text.strip()) < 3:
        return False
    excluded_roles = (
        ("repeated_marginal", 0.95),
        ("running_header", 0.98),
        ("page_number", 0.98),
        ("boilerplate", 0.98),
        ("toc", 0.92),
        ("toc_candidate", 0.70),
        ("footnote", 0.60),
        ("heading", 0.45),
        ("non_narrative_section", 0.85),
    )
    return not any(
        block.role_score(role) >= score for role, score in excluded_roles
    )


def _is_probable_page_boundary_header(
    block: SourceBlock, following_blocks: list[SourceBlock]
) -> bool:
    """Skip a terse top line only when substantial page content follows it."""
    bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
    page_size = block.attributes.get("page_size") or [1, 1]
    top = float(bbox[1]) / max(1.0, float(page_size[1]))
    if not (
        top <= 0.09
        and int(block.attributes.get("source_lines") or 1) <= 2
        and len(block.text.split()) <= 5
        and not _is_major_section_heading_text(block.text)
    ):
        return False
    return any(
        len(candidate.text) >= 40
        and int(candidate.attributes.get("source_lines") or 1) >= 2
        and float(
            (candidate.attributes.get("bbox") or [0, 0, 0, 0])[1]
        )
        > float(bbox[3])
        for candidate in following_blocks
    )


def _has_structural_separator_after(blocks: list[SourceBlock], candidate: SourceBlock) -> bool:
    try:
        candidate_index = next(
            index for index, block in enumerate(blocks) if block.block_id == candidate.block_id
        )
    except StopIteration:
        return True
    return any(_is_structural_page_separator(block) for block in blocks[candidate_index + 1:])


def _has_structural_separator_before(blocks: list[SourceBlock], candidate: SourceBlock) -> bool:
    try:
        candidate_index = next(
            index for index, block in enumerate(blocks) if block.block_id == candidate.block_id
        )
    except StopIteration:
        return True
    return any(_is_structural_page_separator(block) for block in blocks[:candidate_index])


def _is_structural_page_separator(block: SourceBlock) -> bool:
    if any(
        block.role_score(role) >= score
        for role, score in (
            ("repeated_marginal", 0.95),
            ("running_header", 0.98),
            ("page_number", 0.98),
        )
    ):
        return False
    if re.match(
        r"^(?:[ivxlcdm]+\s+)?acknowledg(?:e)?ments?\b",
        _normalize_space(block.text),
        re.IGNORECASE,
    ):
        return True
    return any(
        block.role_score(role) >= score
        for role, score in (
            ("deterministic_chapter", 0.85),
            ("heading", 0.45),
            ("non_narrative_section", 0.85),
            ("toc", 0.92),
            ("boilerplate", 0.98),
        )
    )


def _page_continuation_details(
    previous: SourceBlock, current: SourceBlock
) -> tuple[str, list[str]] | None:
    if (
        previous.role_score("tabular_region") >= 0.90
        or current.role_score("tabular_region") >= 0.90
    ):
        return None
    previous_box = previous.attributes.get("bbox") or [0, 0, 0, 0]
    current_box = current.attributes.get("bbox") or [0, 0, 0, 0]
    previous_size = previous.attributes.get("page_size") or [1, 1]
    current_size = current.attributes.get("page_size") or [1, 1]
    previous_bottom = float(previous_box[3]) / max(1.0, float(previous_size[1]))
    current_top = float(current_box[1]) / max(1.0, float(current_size[1]))
    if previous_bottom < 0.64 or current_top > 0.32:
        return None
    if previous.attributes.get("source_method") != current.attributes.get(
        "source_method"
    ):
        return None
    if previous.attributes.get("reading_order") != current.attributes.get(
        "reading_order"
    ):
        return None
    if not _matching_horizontal_directions(previous, current):
        return None

    previous_font = float(previous.attributes.get("font_size") or 0.0)
    current_font = float(current.attributes.get("font_size") or 0.0)
    if previous_font and current_font:
        font_ratio = current_font / previous_font
        if not 0.78 <= font_ratio <= 1.28:
            return None
    previous_left = float(previous_box[0]) / max(1.0, float(previous_size[0]))
    current_left = float(current_box[0]) / max(1.0, float(current_size[0]))
    if abs(previous_left - current_left) > 0.14:
        return None

    previous_text = previous.text.rstrip()
    current_text = current.text.lstrip()
    if not previous_text or not current_text:
        return None
    previous_language = _dominant_latin_language(previous_text)
    current_language = _dominant_latin_language(current_text)
    if previous_language and current_language and previous_language != current_language:
        return None
    reasons = [
        "adjacent_pages",
        "body_blocks_touch_page_boundary",
        "matching_source_method_and_layout",
        "matching_typography_and_left_margin",
    ]
    if _ends_with_split_hyphen(previous_text) and _starts_with_letter(current_text):
        return _continuation_join_mode(
            previous_text, remove_printed_hyphen=False
        ), reasons + ["hyphenated_word_continues"]
    previous_method = str(previous.attributes.get("source_method") or "")
    if (
        previous_method != "ocr"
        and int(previous.attributes.get("source_lines") or 1) <= 1
        and len(previous_text.split()) < 8
    ):
        return None
    if (
        int(current.attributes.get("source_lines") or 1) <= 1
        and len(current_text.split()) < 4
    ):
        return None
    if _ends_with_sentence_terminal(
        previous_text
    ) or not _starts_with_lowercase_continuation(current_text):
        return None
    return "space", reasons + ["unfinished_sentence_with_lowercase_continuation"]


def _ends_with_split_hyphen(text: str) -> bool:
    stripped = text.rstrip()
    return bool(
        len(stripped) >= 2
        and stripped[-1] in {"-", "\u00ad"}
        and stripped[-2].isalpha()
    )


def _starts_with_letter(text: str) -> bool:
    return bool(_first_letter(text))


def _starts_with_lowercase_continuation(text: str) -> bool:
    first_letter = _first_letter(text)
    return bool(first_letter and first_letter.islower())


def _first_letter(text: str) -> str:
    for char in str(text or "").lstrip(" \t\"'“”‘’([{<"):
        if char.isalpha():
            return char
        if not char.isspace() and char not in "\"'“”‘’([{<":
            return ""
    return ""


def _ends_with_sentence_terminal(text: str) -> bool:
    return str(text or "").rstrip().endswith((".", "!", "?", "…", ":", ";"))


def _dominant_latin_language(text: str) -> str:
    """Return a language only when simple stop-word evidence is decisive.

    It is intentionally limited to English/French: those can share the same
    page geometry in bilingual source editions, while a weak guess must never
    suppress a valid continuation.
    """
    tokens = re.findall(r"[^\W\d_]+", str(text or "").casefold())
    if not tokens:
        return ""
    scores = {
        language: sum(token in words for token in tokens)
        for language, words in _LATIN_LANGUAGE_STOPWORDS.items()
    }
    language, score = max(scores.items(), key=lambda item: item[1])
    other_score = max(value for other, value in scores.items() if other != language)
    return language if score >= 2 and score >= other_score + 2 else ""


def _find_toc_pages(
    blocks_by_page: dict[int, list[SourceBlock]], front_limit: int
) -> set[int]:
    """Find an anchored TOC and its short, numbered continuation pages."""
    anchors = {
        page
        for page, blocks in blocks_by_page.items()
        if page <= front_limit
        and any(_is_toc_heading_text(block.text) for block in blocks)
    }
    toc_pages = set(anchors)
    for anchor in anchors:
        for page in range(anchor + 1, min(front_limit, anchor + 5) + 1):
            if not _looks_like_toc_continuation(blocks_by_page.get(page, [])):
                break
            toc_pages.add(page)
    return toc_pages


def _is_toc_heading_text(text: str) -> bool:
    """Accept an actual TOC heading, not prose that merely mentions one."""
    normalized = _normalize_space(text).strip()
    normalized = re.sub(r"\s*[:.\-\u2013\u2014]+\s*$", "", normalized).strip()
    return bool(
        normalized
        and len(normalized) <= 80
        and len(normalized.split()) <= 8
        and _TOC_HEADING_RE.fullmatch(normalized)
    )


def _looks_like_toc_continuation(blocks: list[SourceBlock]) -> bool:
    if not blocks:
        return False
    texts = [_normalize_space(block.text) for block in blocks if _normalize_space(block.text)]
    if not texts:
        return False
    long_narration = any(len(text) > 300 or len(text.split()) > 50 for text in texts)
    if long_narration:
        return False
    entry_count = sum(
        len(re.findall(r"(?:\.{3,}\s*|\s+)\d{1,4}(?=\s|$)", text)) for text in texts
    )
    page_number_column = any(
        re.fullmatch(r"(?:\d{1,4}\s+){3,}\d{1,4}", text) is not None for text in texts
    )
    return page_number_column or entry_count >= 2


def _find_front_boilerplate_pages(
    blocks_by_page: dict[int, list[SourceBlock]], page_count: int
) -> set[int]:
    front_limit = min(12, max(5, int(page_count * 0.05)))
    return {
        page
        for page, blocks in blocks_by_page.items()
        if page <= front_limit and sum(bool(_COPYRIGHT_RE.search(block.text)) for block in blocks) >= 2
    }


def _is_content_opener(
    block: SourceBlock, page_blocks: list[SourceBlock], body_font: float
) -> bool:
    bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
    page_size = block.attributes.get("page_size") or [1, 1]
    y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
    if y0 > 0.42:
        return False
    bottom = float(bbox[3])
    for other in page_blocks:
        if other.block_id == block.block_id or len(other.text) < 140:
            continue
        other_bbox = other.attributes.get("bbox") or [0, 0, 0, 0]
        other_font = float(other.attributes.get("font_size") or body_font)
        if float(other_bbox[1]) >= bottom - 1.0 and other_font <= body_font * 1.15:
            return True
    return False


def _large_heading_continuation_ids(
    blocks_by_page: dict[int, list[SourceBlock]], body_font: float
) -> set[str]:
    """Identify later visual lines of one large, centered heading.

    Native PDF blocks can split a multiline title at font or editing boundaries,
    and tiny superscript/marginal records may occur between its lines. Those
    lines remain separately inspectable, but only the first substantive line
    should become a chapter marker.
    """
    continuation_ids: set[str] = set()
    for blocks in blocks_by_page.values():
        candidates: list[SourceBlock] = []
        for block in blocks:
            bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
            page_size = block.attributes.get("page_size") or [1, 1]
            y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
            font_size = float(block.attributes.get("font_size") or 0.0)
            text = block.text.strip()
            if (
                len(text) >= 4
                and len(text) <= 160
                and len(text.split()) <= 18
                and y0 <= 0.42
                and font_size >= body_font * 1.45
            ):
                candidates.append(block)
        previous: SourceBlock | None = None
        for block in sorted(
            candidates,
            key=lambda item: (
                float((item.attributes.get("bbox") or [0, 0, 0, 0])[1]),
                float((item.attributes.get("bbox") or [0, 0, 0, 0])[0]),
            ),
        ):
            if previous is not None and _large_heading_blocks_are_contiguous(
                previous, block, body_font
            ):
                continuation_ids.add(block.block_id)
            else:
                previous = block
                continue
            previous = block
    return continuation_ids


def _large_heading_blocks_are_contiguous(
    previous: SourceBlock, current: SourceBlock, body_font: float
) -> bool:
    previous_box = previous.attributes.get("bbox") or [0, 0, 0, 0]
    current_box = current.attributes.get("bbox") or [0, 0, 0, 0]
    previous_font = float(previous.attributes.get("font_size") or body_font)
    current_font = float(current.attributes.get("font_size") or body_font)
    if (
        max(previous_font, current_font) / max(0.1, min(previous_font, current_font))
        > 1.18
    ):
        return False
    height = max(
        1.0,
        float(previous_box[3]) - float(previous_box[1]),
        float(current_box[3]) - float(current_box[1]),
    )
    vertical_gap = float(current_box[1]) - float(previous_box[3])
    if not -height * 0.10 <= vertical_gap <= max(height * 0.65, body_font * 1.5):
        return False
    previous_center = (float(previous_box[0]) + float(previous_box[2])) / 2.0
    current_center = (float(current_box[0]) + float(current_box[2])) / 2.0
    return abs(previous_center - current_center) <= max(height * 1.75, body_font * 3.0)


def _is_title_followed_by_byline(
    block: SourceBlock, page_blocks: list[SourceBlock], body_font: float
) -> bool:
    """Recognize a front-matter title/byline pair without relying on its words.

    This prevents a large article or book title from becoming an audiobook
    chapter simply because it precedes substantial prose.
    """
    text = block.text.strip()
    if not text or len(text) > 180 or len(text.split()) > 16:
        return False
    bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
    page_size = block.attributes.get("page_size") or [1, 1]
    y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
    font_size = float(block.attributes.get("font_size") or body_font)
    if y0 > 0.35 or font_size < body_font * 1.30:
        return False
    try:
        index = next(i for i, candidate in enumerate(page_blocks) if candidate.block_id == block.block_id)
    except StopIteration:
        return False
    following = page_blocks[index + 1 : index + 3]
    for candidate in following:
        candidate_text = candidate.text.strip()
        candidate_box = candidate.attributes.get("bbox") or [0, 0, 0, 0]
        candidate_font = float(candidate.attributes.get("font_size") or body_font)
        vertical_distance = float(candidate_box[1]) - float(bbox[3])
        if vertical_distance < -2.0 or vertical_distance > float(page_size[1]) * 0.14:
            continue
        if (
            _looks_like_byline(candidate_text)
            and len(candidate_text) <= 90
            and candidate_font <= font_size * 0.88
            and not _CHAPTER_RE.match(candidate_text)
            and not _NUMBERED_HEADING_RE.match(candidate_text)
            and not _MAJOR_SECTION_RE.match(candidate_text)
        ):
            return True
    return False


def _looks_like_byline(text: str) -> bool:
    normalized = _normalize_space(text)
    if normalized.endswith((".", "!", "?", ";", ":")):
        return False
    words = re.findall(r"[^\W\d_]+", normalized)
    if not 2 <= len(words) <= 8:
        return False
    capitalized = sum(word[0].isupper() for word in words if word)
    # Permit a connector such as "and", "de", or "van" in an otherwise
    # name-like byline, but avoid treating a short sentence as an author line.
    return capitalized >= len(words) - 1


def _is_probable_running_header(
    text: str, short: bool, y1: float, font_size: float, body_font: float
) -> bool:
    """Catch section/page headers whose wording varies too much to repeat exactly."""
    return bool(
        short
        and y1 <= 0.14
        and font_size <= body_font * 1.10
        and text.isupper()
        and re.search(r"[^\W\d_]", text)
        and not _is_major_section_heading_text(text)
    )


def _load_cached_document(
    cache_path: str, source_fingerprint: dict[str, Any], config: PDFIngestionConfig
) -> SourceDocument | None:
    try:
        with open(cache_path, "r", encoding="utf-8") as file_handle:
            payload = json.load(file_handle)
        document = SourceDocument.from_dict(payload)
        ingestion = document.attributes.get("pdf_ingestion", {})
        if ingestion.get("version") != PDF_INGESTION_VERSION:
            return None
        if ingestion.get("source_fingerprint") != source_fingerprint:
            return None
        if ingestion.get("config") != asdict(config):
            return None
        return document
    except (OSError, ValueError, TypeError):
        return None


def _source_fingerprint(path: str) -> dict[str, Any]:
    stat = os.stat(path)
    digest = hashlib.sha256()
    with open(path, "rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()}


def _normalized_marginal_key(text: str) -> str:
    normalized = _normalize_space(text).casefold()
    normalized = re.sub(r"\d+", "#", normalized)
    return re.sub(r"[^\w#]+", "", normalized)


def _combined_bbox(lines: list[dict[str, Any]]) -> list[float]:
    return _round_bbox(
        [
            min(line["bbox"][0] for line in lines),
            min(line["bbox"][1] for line in lines),
            max(line["bbox"][2] for line in lines),
            max(line["bbox"][3] for line in lines),
        ]
    )


def _weighted_median(samples: list[tuple[float, int]]) -> float:
    """Return the median value when each sample represents several source lines."""
    ordered = sorted((value, max(1, int(weight))) for value, weight in samples)
    threshold = sum(weight for _, weight in ordered) / 2
    cumulative = 0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]


def _round_bbox(value: Iterable[float]) -> list[float]:
    return [round(float(item), 3) for item in value]


def _bbox_area(value: Iterable[float]) -> float:
    x0, y0, x1, y1 = value
    return max(0.0, float(x1) - float(x0)) * max(0.0, float(y1) - float(y0))


def _normalize_space(text: str) -> str:
    without_controls = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or ""))
    return re.sub(r"\s+", " ", without_controls).strip()


def _write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as file_handle:
        json.dump(payload, file_handle, indent=2, ensure_ascii=False)
        file_handle.write("\n")


def _emit(callback: ProgressCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
