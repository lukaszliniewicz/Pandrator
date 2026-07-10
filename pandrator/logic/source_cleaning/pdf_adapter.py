"""Geometry-aware PDF ingestion with selective PP-OCRv6 medium OCR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

from .models import SourceBlock, SourceDocument
from .pdf_text_adapter import _front_matter_metadata, _metadata_from_filename


ProgressCallback = Callable[[str], None]
PDF_INGESTION_VERSION = 4
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
    r"conclusion|appendi(?:x|ces)|postscript|chapter|book|part|volume|section|"
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
    r"(?:wykaz|spis)\s+skr[oó]t[oó]w|ключи|.*(?:мини[-*])?словар\w*)\b",
    re.IGNORECASE,
)


@dataclass
class PDFIngestionConfig:
    ocr_mode: str = "auto"
    ocr_language: str = "auto"
    ocr_dpi: int = 200
    use_cache: bool = True

    def normalized(self) -> "PDFIngestionConfig":
        mode = str(self.ocr_mode or "auto").lower()
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
        result = list(engine.predict(image))[0]
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
    cache_path = os.path.join(artifact_dir, "source_document.json") if artifact_dir else ""
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
        metadata_candidates=_metadata_from_filename(os.path.splitext(os.path.basename(normalized_path))[0]),
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
            document.warnings.append(f"Could not read PyCropPDF provenance manifest: {error}")
    engine = ocr_engine or PaddleOCRMediumEngine()
    pdf = fitz.open(normalized_path)
    line_number = 1
    source_index = 0
    try:
        for page_index, page in enumerate(pdf):
            _emit(progress_callback, f"Ingesting PDF page {page_index + 1}/{pdf.page_count}...")
            native_lines = _extract_native_lines(page)
            diagnostics = _native_diagnostics(page, native_lines)
            use_ocr = resolved.ocr_mode == "force" or (
                resolved.ocr_mode == "auto" and diagnostics["auto_ocr"]
            )
            source_method = "native"
            ocr_report: dict[str, Any] | None = None
            lines = native_lines
            if use_ocr:
                source_method = "ocr"
                try:
                    _emit(progress_callback, f"Running OCR on PDF page {page_index + 1}/{pdf.page_count}...")
                    lines, ocr_report = engine.recognize(page, resolved.ocr_language, resolved.ocr_dpi)
                except Exception as error:
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
                        "page_size": [round(float(page.rect.width), 3), round(float(page.rect.height), 3)],
                        "source_method": source_method,
                        "font_size": payload.get("font_size", 0.0),
                        "fonts": payload.get("fonts", []),
                        "confidence": payload.get("confidence"),
                        "source_lines": payload.get("source_lines", 1),
                        "reading_order": payload.get("reading_order", "top_to_bottom"),
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
                    "ocr": ocr_report,
                    "block_count": len(page_blocks),
                }
            )
    finally:
        pdf.close()

    _emit(progress_callback, "Analyzing PDF structure and layout...")
    _annotate_structural_roles(document)
    _annotate_page_continuations(document)
    repeated_marginal_count = sum(
        block.role_score("repeated_marginal") >= 0.95 for block in document.blocks
    )
    page_continuation_count = sum(
        block.role_score("page_continuation") >= 0.92 for block in document.blocks
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
        "native_page_count": len(document.attributes["pdf_ingestion"]["pages"]) - ocr_page_count,
        "block_count": len(document.blocks),
        "repeated_marginal_block_count": repeated_marginal_count,
        "page_continuation_count": page_continuation_count,
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
            {"op": "mark_chapter", "block_id": block_id, "reason": "high-confidence PDF heading", "confidence": 0.88}
        )
    return operations


def _extract_native_lines(page: Any) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for block_index, block in enumerate(page.get_text("dict").get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = _normalize_space("".join(str(span.get("text", "")) for span in spans))
            if not text:
                continue
            char_count = max(1, sum(len(str(span.get("text", ""))) for span in spans))
            lines.append(
                {
                    "text": text,
                    "bbox": _round_bbox(line["bbox"]),
                    "block_index": block_index,
                    "font_size": round(
                        sum(float(span.get("size", 0.0)) * len(str(span.get("text", ""))) for span in spans)
                        / char_count,
                        3,
                    ),
                    "font": ",".join(sorted({str(span.get("font", "")) for span in spans if span.get("font")})),
                    "confidence": None,
                }
            )
    return lines


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


def _lines_to_blocks(lines: list[dict[str, Any]], page_rect: Any, source_method: str) -> list[dict[str, Any]]:
    if not lines:
        return []
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
        confidences = [float(line["confidence"]) for line in group if line.get("confidence") is not None]
        blocks.append(
            {
                "text": text,
                "bbox": _combined_bbox(group),
                "font_size": round(statistics.fmean(float(line.get("font_size") or 0.0) for line in group), 3),
                "fonts": sorted({str(line.get("font") or "") for line in group if line.get("font")}),
                "confidence": round(statistics.fmean(confidences), 4) if confidences else None,
                "source_lines": len(group),
                "reading_order": reading_order,
                "source_method": source_method,
            }
        )
    return blocks


def _geometry_order(lines: list[dict[str, Any]], page_width: float) -> tuple[list[dict[str, Any]], str]:
    if len(lines) < 8:
        return sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0])), "top_to_bottom"
    middle = page_width / 2.0
    gutter = page_width * 0.01
    left = [line for line in lines if line["bbox"][2] < middle - gutter]
    right = [line for line in lines if line["bbox"][0] > middle + gutter]
    if len(left) >= 4 and len(right) >= 4:
        spanning = [line for line in lines if line not in left and line not in right]
        top = min(min(line["bbox"][1] for line in left), min(line["bbox"][1] for line in right))
        top_spanning = [line for line in spanning if line["bbox"][3] <= top + 14]
        rest_spanning = [line for line in spanning if line not in top_spanning]
        ordered = sorted(top_spanning, key=lambda line: (line["bbox"][1], line["bbox"][0]))
        ordered += sorted(left, key=lambda line: (line["bbox"][1], line["bbox"][0]))
        ordered += sorted(right, key=lambda line: (line["bbox"][1], line["bbox"][0]))
        ordered += sorted(rest_spanning, key=lambda line: (line["bbox"][1], line["bbox"][0]))
        return ordered, "two_columns"
    return sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0])), "top_to_bottom"


def _same_paragraph(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_block = previous.get("block_index")
    current_block = current.get("block_index")
    if previous_block is not None and current_block is not None and previous_block == current_block:
        return True
    prev_box = previous["bbox"]
    box = current["bbox"]
    height = max(5.0, prev_box[3] - prev_box[1], box[3] - box[1])
    vertical_gap = box[1] - prev_box[3]
    left_gap = abs(box[0] - prev_box[0])
    if box[1] < prev_box[1] - height:
        return False
    return vertical_gap <= height * 1.15 and left_gap <= height * 2.5


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
    font_sizes = [
        float(block.attributes.get("font_size") or 0.0)
        for block in document.blocks
        if float(block.attributes.get("font_size") or 0.0) > 0
    ]
    body_font = statistics.median(font_sizes) if font_sizes else 10.0
    page_count = max((block.page or 0 for block in document.blocks), default=1)
    blocks_by_page: dict[int, list[SourceBlock]] = defaultdict(list)
    for block in document.blocks:
        if block.page:
            blocks_by_page[block.page].append(block)

    marginal_occurrences: dict[str, list[SourceBlock]] = defaultdict(list)
    for block in document.blocks:
        bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
        page_size = block.attributes.get("page_size") or [1, 1]
        y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
        y1 = float(bbox[3]) / max(1.0, float(page_size[1]))
        if y1 <= 0.16 or y0 >= 0.84:
            key = _normalized_marginal_key(block.text)
            if key:
                marginal_occurrences[key].append(block)
    repeated_threshold = max(3, min(8, int(page_count * 0.15) or 3))
    repeated_ids = {
        block.block_id
        for blocks in marginal_occurrences.values()
        if len({block.page for block in blocks}) >= repeated_threshold
        for block in blocks
    }

    front_limit = max(5, min(30, int(page_count * 0.2) + 1))
    toc_pages = _find_toc_pages(blocks_by_page, front_limit)
    boilerplate_pages = _find_front_boilerplate_pages(blocks_by_page, page_count)
    for block in document.blocks:
        evidence: dict[str, dict[str, Any]] = {}
        text = block.text.strip()
        bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
        page_size = block.attributes.get("page_size") or [1, 1]
        y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
        y1 = float(bbox[3]) / max(1.0, float(page_size[1]))
        font_size = float(block.attributes.get("font_size") or body_font)
        font_ratio = font_size / max(1.0, body_font)
        short = len(text) <= 160 and len(text.split()) <= 18
        page_blocks = blocks_by_page.get(block.page or 0, [])
        is_toc_page = (block.page or 0) in toc_pages
        is_content_opener = _is_content_opener(block, page_blocks, body_font)
        is_running_header = _is_probable_running_header(text, short, y1, font_size, body_font)

        page_number_shape = bool(re.fullmatch(r"(?:\d{1,4}|[ivxlcdm]{1,8})", text, re.IGNORECASE))
        page_number_size = font_size <= body_font * 1.15
        if page_number_shape and page_number_size and (y1 <= 0.18 or y0 >= 0.80):
            evidence["page_number"] = {"score": 0.99, "reasons": ["numeric_or_roman", "marginal_position"]}
        if block.block_id in repeated_ids:
            evidence["repeated_marginal"] = {
                "score": 0.98,
                "reasons": ["normalized_text_repeats_across_pages", "consistent_marginal_position"],
            }
        if is_running_header:
            evidence["running_header"] = {
                "score": 0.98,
                "reasons": ["top_marginal_position", "smaller_than_body_font", "variable_header_shape"],
            }
        if (block.page or 0) in boilerplate_pages:
            reasons = ["front_matter_page_with_multiple_publishing_signals"]
            if _COPYRIGHT_RE.search(text):
                reasons.insert(0, "explicit_publishing_or_copyright_signal")
            evidence["boilerplate"] = {"score": 0.98, "reasons": reasons}

        heading_score = 0.0
        heading_reasons: list[str] = []
        explicit_chapter = short and bool(_CHAPTER_RE.match(text))
        numbered_heading = short and bool(_NUMBERED_HEADING_RE.match(text))
        major_section = short and bool(_MAJOR_SECTION_RE.match(text))
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
        if heading_score >= 0.45:
            evidence["heading"] = {"score": min(0.99, heading_score), "reasons": heading_reasons}
        if non_narrative_section:
            evidence["non_narrative_section"] = {
                "score": 0.90,
                "reasons": ["bibliographic_or_note_section_heading"],
            }
        if (
            heading_score >= 0.85
            and not evidence.get("repeated_marginal")
            and not evidence.get("running_header")
            and not is_toc_page
            and not non_narrative_section
            and (is_content_opener or explicit_chapter or major_section)
        ):
            evidence["deterministic_chapter"] = {
                "score": min(0.96, heading_score),
                "reasons": heading_reasons + ["global_pdf_heading_policy"],
            }

        footnote_reasons: list[str] = []
        footnote_score = 0.0
        note_marker = bool(_NOTE_PREFIX_RE.match(text) or _SINGLE_NOTE_MARKER_RE.fullmatch(text))
        if y0 >= 0.80:
            footnote_score += 0.45
            footnote_reasons.append("bottom_page_region")
        elif y0 >= 0.72:
            footnote_score += 0.30
            footnote_reasons.append("lower_page_region")
        if font_size <= body_font * 0.90:
            footnote_score += 0.3
            footnote_reasons.append("smaller_than_body_font")
        if note_marker:
            footnote_score += 0.35
            footnote_reasons.append("note_marker_prefix")
        if footnote_score >= 0.6 and "page_number" not in evidence:
            evidence["footnote"] = {"score": min(0.98, footnote_score), "reasons": footnote_reasons}

        toc_like = bool(re.search(r"\.{3,}\s*\d{1,4}$", text)) or (
            short and bool(re.search(r"\s+\d{1,4}$", text))
        )
        if is_toc_page:
            evidence["toc"] = {
                "score": 0.94,
                "reasons": ["front_matter", "toc_heading_or_continuation_page"],
            }
        elif (block.page or 1) <= front_limit and (toc_like or _TOC_HEADING_RE.search(text)):
            evidence["toc_candidate"] = {
                "score": 0.7 if toc_like else 0.85,
                "reasons": ["front_matter", "toc_entry_shape" if toc_like else "toc_heading"],
            }
        block.attributes["role_evidence"] = evidence
        block.role_candidates = sorted(set(block.role_candidates + list(evidence)))


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
    for previous_page, current_page in zip(pages, pages[1:]):
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
    for block in reversed(blocks) if reverse else blocks:
        if _is_narrative_boundary_block(block):
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
    return not any(block.role_score(role) >= score for role, score in excluded_roles)


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
    previous_box = previous.attributes.get("bbox") or [0, 0, 0, 0]
    current_box = current.attributes.get("bbox") or [0, 0, 0, 0]
    previous_size = previous.attributes.get("page_size") or [1, 1]
    current_size = current.attributes.get("page_size") or [1, 1]
    previous_bottom = float(previous_box[3]) / max(1.0, float(previous_size[1]))
    current_top = float(current_box[1]) / max(1.0, float(current_size[1]))
    if previous_bottom < 0.64 or current_top > 0.32:
        return None
    if previous.attributes.get("source_method") != current.attributes.get("source_method"):
        return None
    if previous.attributes.get("reading_order") != current.attributes.get("reading_order"):
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
    reasons = [
        "adjacent_pages",
        "body_blocks_touch_page_boundary",
        "matching_source_method_and_layout",
        "matching_typography_and_left_margin",
    ]
    if _ends_with_split_hyphen(previous_text) and _starts_with_letter(current_text):
        return "remove_hyphen", reasons + ["hyphenated_word_continues"]
    if _ends_with_sentence_terminal(previous_text) or not _starts_with_lowercase_continuation(current_text):
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


def _find_toc_pages(
    blocks_by_page: dict[int, list[SourceBlock]], front_limit: int
) -> set[int]:
    """Find an anchored TOC and its short, numbered continuation pages."""
    anchors = {
        page
        for page, blocks in blocks_by_page.items()
        if page <= front_limit and any(_TOC_HEADING_RE.search(block.text) for block in blocks)
    }
    toc_pages = set(anchors)
    for anchor in anchors:
        for page in range(anchor + 1, min(front_limit, anchor + 5) + 1):
            if not _looks_like_toc_continuation(blocks_by_page.get(page, [])):
                break
            toc_pages.add(page)
    return toc_pages


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


def _is_probable_running_header(
    text: str, short: bool, y1: float, font_size: float, body_font: float
) -> bool:
    """Catch section/page headers whose wording varies too much to repeat exactly."""
    return bool(
        short
        and y1 <= 0.10
        and font_size <= body_font * 1.10
        and (text.isupper() or re.match(r"^\d{1,4}\s+", text))
        and re.search(r"[^\W\d_]", text)
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


def _round_bbox(value: Iterable[float]) -> list[float]:
    return [round(float(item), 3) for item in value]


def _bbox_area(value: Iterable[float]) -> float:
    x0, y0, x1, y1 = value
    return max(0.0, float(x1) - float(x0)) * max(0.0, float(y1) - float(y0))


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as file_handle:
        json.dump(payload, file_handle, indent=2, ensure_ascii=False)
        file_handle.write("\n")


def _emit(callback: ProgressCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
