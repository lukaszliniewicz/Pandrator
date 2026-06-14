"""Geometry-aware PDF ingestion with selective PP-OCRv6 medium OCR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

from .models import SourceBlock, SourceDocument
from .pdf_text_adapter import _front_matter_metadata, _metadata_from_filename


ProgressCallback = Callable[[str], None]
PDF_INGESTION_VERSION = 1
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
    r"^(?:[ivxlcdm]+|\d{1,4})(?:(?:[.)]\s+)|(?:\s+-\s+)|\s+)\S+",
    re.IGNORECASE,
)
_NOTE_PREFIX_RE = re.compile(r"^(?:\[\d+\]|\d{1,3}[.)]|[*†‡])\s+\S+")
_TOC_HEADING_RE = re.compile(
    r"^(?:table of contents|contents|spis tre[sś]ci|sommaire|inhaltsverzeichnis|indice|índice)$",
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
    repeated_marginal_count = sum(
        block.role_score("repeated_marginal") >= 0.95 for block in document.blocks
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
            if block.role_score("repeated_marginal") >= 0.95 or block.role_score("page_number") >= 0.98
        ]
        if repeated:
            deletion_groups.append(("high-confidence repeated margins and page numbers", repeated, 0.98))
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

    toc_candidates: list[SourceBlock] = []
    front_limit = max(5, min(30, int(page_count * 0.2) + 1))
    for block in document.blocks:
        evidence: dict[str, dict[str, Any]] = {}
        text = block.text.strip()
        lowered = text.casefold()
        bbox = block.attributes.get("bbox") or [0, 0, 0, 0]
        page_size = block.attributes.get("page_size") or [1, 1]
        y0 = float(bbox[1]) / max(1.0, float(page_size[1]))
        y1 = float(bbox[3]) / max(1.0, float(page_size[1]))
        font_size = float(block.attributes.get("font_size") or body_font)
        short = len(text) <= 160 and len(text.split()) <= 18

        page_number_shape = bool(re.fullmatch(r"(?:\d{1,4}|[ivxlcdm]{1,8})", text, re.IGNORECASE))
        page_number_size = font_size <= body_font * 1.15
        if page_number_shape and page_number_size and (y1 <= 0.18 or y0 >= 0.80):
            evidence["page_number"] = {"score": 0.99, "reasons": ["numeric_or_roman", "marginal_position"]}
        if block.block_id in repeated_ids:
            evidence["repeated_marginal"] = {
                "score": 0.98,
                "reasons": ["normalized_text_repeats_across_pages", "consistent_marginal_position"],
            }
        heading_score = 0.0
        heading_reasons: list[str] = []
        if short and font_size >= body_font * 1.18:
            heading_score += 0.45
            heading_reasons.append("larger_than_body_font")
        if short and (_CHAPTER_RE.match(text) or _NUMBERED_HEADING_RE.match(text)):
            heading_score += 0.45
            heading_reasons.append("numbered_or_named_heading")
        if short and text.isupper() and len(text) >= 4:
            heading_score += 0.20
            heading_reasons.append("all_caps")
        if short and y0 < 0.35:
            heading_score += 0.10
            heading_reasons.append("upper_page_position")
        if heading_score >= 0.45:
            evidence["heading"] = {"score": min(0.99, heading_score), "reasons": heading_reasons}
        if heading_score >= 0.85 and not evidence.get("repeated_marginal"):
            evidence["deterministic_chapter"] = {
                "score": min(0.96, heading_score),
                "reasons": heading_reasons + ["global_pdf_heading_policy"],
            }

        footnote_reasons: list[str] = []
        footnote_score = 0.0
        if y0 >= 0.72:
            footnote_score += 0.4
            footnote_reasons.append("bottom_page_region")
        if font_size <= body_font * 0.86:
            footnote_score += 0.3
            footnote_reasons.append("smaller_than_body_font")
        if _NOTE_PREFIX_RE.match(text):
            footnote_score += 0.3
            footnote_reasons.append("note_marker_prefix")
        if footnote_score >= 0.6 and "page_number" not in evidence:
            evidence["footnote"] = {"score": min(0.98, footnote_score), "reasons": footnote_reasons}

        toc_like = bool(re.search(r"\.{3,}\s*\d{1,4}$", text)) or (
            short and bool(re.search(r"\s+\d{1,4}$", text))
        )
        if (block.page or 1) <= front_limit and (toc_like or _TOC_HEADING_RE.match(text)):
            toc_candidates.append(block)
            evidence["toc_candidate"] = {
                "score": 0.7 if toc_like else 0.85,
                "reasons": ["front_matter", "toc_entry_shape" if toc_like else "toc_heading"],
            }
        block.attributes["role_evidence"] = evidence
        block.role_candidates = sorted(set(block.role_candidates + list(evidence)))

    by_page = Counter(block.page for block in toc_candidates)
    toc_pages = {page for page, count in by_page.items() if page and count >= 4}
    for block in toc_candidates:
        if block.page not in toc_pages:
            continue
        evidence = block.attributes["role_evidence"]
        evidence["toc"] = {
            "score": 0.94,
            "reasons": ["front_matter", "dense_toc_candidate_page"],
        }
        block.role_candidates = sorted(set(block.role_candidates + ["toc"]))


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
