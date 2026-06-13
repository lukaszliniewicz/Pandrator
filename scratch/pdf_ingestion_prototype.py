"""Scratch evaluator for Pandrator's proposed lightweight PDF ingestion path.

This intentionally remains separate from production ingestion. It emits structured
reports and readable target-page text for real-book comparisons.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

# Import ONNX Runtime before PyMuPDF Layout. This avoids a Windows DLL loading
# conflict in the current Pandrator environment.
import onnxruntime  # noqa: F401
import numpy as np
import pymupdf


DEFAULT_SAMPLE_REPORT = Path(r"E:\Pandrator\.tmp\pdf-realbooks-probe\report.json")
DEFAULT_OUTPUT = Path(r"E:\Pandrator\.tmp\pdf-lightweight-prototype")
LAYOUT_CONTENT_ROLES = {
    "text",
    "section-header",
    "title",
    "list-item",
    "caption",
    "table",
    "formula",
}
MARGINAL_ROLES = {"page-header", "page-footer"}
LANGUAGE_BY_SAMPLE = {
    "cyrillic": "cyrillic",
    "historical_existing_ocr": "latin",
    "historical_no_text": "latin",
    "polish_scan": "latin",
}


def install_layout_compatibility_patch() -> None:
    import pymupdf.layout  # noqa: F401
    from pymupdf.layout.onnx import BoxRFDGNN

    current = BoxRFDGNN.get_nn_input_from_datadict
    if getattr(current, "_pandrator_int64_patch", False):
        return

    def patched(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        values = list(current(*args, **kwargs))
        for index in (1, 3):
            if values[index] is not None:
                values[index] = np.asarray(values[index], dtype=np.int64)
        return tuple(values)

    patched._pandrator_int64_patch = True  # type: ignore[attr-defined]
    BoxRFDGNN.get_nn_input_from_datadict = patched


def bbox_tuple(value: Iterable[float]) -> list[float]:
    return [round(float(item), 3) for item in value]


def bbox_area(bbox: Iterable[float]) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def intersection_area(first: Iterable[float], second: Iterable[float]) -> float:
    ax0, ay0, ax1, ay1 = first
    bx0, by0, bx1, by1 = second
    return max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(
        0.0, min(ay1, by1) - max(ay0, by0)
    )


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalized_marginal_key(text: str) -> str:
    text = normalize_space(text).casefold()
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"[^\w#]+", "", text)


def extract_native_lines(page: pymupdf.Page) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    data = page.get_text("dict")
    for block_index, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            spans = line.get("spans", [])
            text = normalize_space("".join(str(span.get("text", "")) for span in spans))
            if not text:
                continue
            char_count = max(1, sum(len(str(span.get("text", ""))) for span in spans))
            result.append(
                {
                    "text": text,
                    "bbox": bbox_tuple(line["bbox"]),
                    "block_index": block_index,
                    "line_index": line_index,
                    "mean_font_size": round(
                        sum(float(span.get("size", 0.0)) * len(str(span.get("text", ""))) for span in spans)
                        / char_count,
                        3,
                    ),
                    "fonts": sorted({str(span.get("font", "")) for span in spans}),
                    "invisible_ratio": round(
                        sum(
                            len(str(span.get("text", "")))
                            for span in spans
                            if int(span.get("alpha", 255)) == 0
                        )
                        / char_count,
                        4,
                    ),
                }
            )
    return result


def script_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for char in text:
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if "CYRILLIC" in name:
            counts["cyrillic"] += 1
        elif "GREEK" in name:
            counts["greek"] += 1
        elif "LATIN" in name:
            counts["latin"] += 1
        else:
            counts["other"] += 1
    return counts


def image_coverage(page: pymupdf.Page) -> float:
    page_area = bbox_area(page.rect)
    if not page_area:
        return 0.0
    covered = sum(bbox_area(info["bbox"]) for info in page.get_image_info())
    return round(min(1.0, covered / page_area), 4)


def native_diagnostics(page: pymupdf.Page, lines: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(line["text"] for line in lines)
    compact = "".join(text.split())
    alpha_numeric = sum(char.isalnum() for char in compact)
    bad_chars = sum(
        char == "\ufffd" or (unicodedata.category(char) == "Cc" and char not in "\n\t")
        for char in text
    )
    one_char_lines = sum(len(line["text"].split()) <= 1 for line in lines)
    invisible_chars = sum(
        len(line["text"]) * float(line["invisible_ratio"]) for line in lines
    )
    scripts = script_counts(text)
    dominant_script = scripts.most_common(1)[0][0] if scripts else "unknown"
    quality = min(1.0, len(compact) / 800.0) * 0.45
    quality += min(1.0, alpha_numeric / max(1, len(compact)) / 0.65) * 0.35
    quality += (1.0 - min(1.0, bad_chars / max(1, len(compact)) * 20.0)) * 0.20
    if len(lines) >= 20 and one_char_lines / len(lines) > 0.6:
        quality *= 0.5

    reasons: list[str] = []
    use_ocr = False
    if len(compact) < 40 or alpha_numeric < 20:
        use_ocr = True
        reasons.append("too_little_native_text")
    if bad_chars / max(1, len(compact)) > 0.02:
        use_ocr = True
        reasons.append("high_invalid_character_ratio")
    if len(lines) >= 20 and one_char_lines / len(lines) > 0.6:
        use_ocr = True
        reasons.append("fragmented_native_text")
    if not reasons:
        reasons.append("native_text_is_plausible")

    return {
        "chars": len(text),
        "compact_chars": len(compact),
        "lines": len(lines),
        "alpha_numeric_ratio": round(alpha_numeric / max(1, len(compact)), 4),
        "bad_character_ratio": round(bad_chars / max(1, len(compact)), 4),
        "one_char_line_ratio": round(one_char_lines / max(1, len(lines)), 4),
        "invisible_text_ratio": round(invisible_chars / max(1, len(text)), 4),
        "dominant_script": dominant_script,
        "script_counts": dict(scripts),
        "image_coverage": image_coverage(page),
        "vector_drawings": len(page.get_drawings()),
        "quality_score": round(quality, 4),
        "auto_ocr": use_ocr,
        "decision_reasons": reasons,
    }


def layout_boxes(page: pymupdf.Page) -> tuple[list[dict[str, Any]], str | None, float]:
    started = time.perf_counter()
    try:
        page.get_layout()
        boxes = [
            {"bbox": bbox_tuple(item[:4]), "role": str(item[4]), "model_index": index}
            for index, item in enumerate(page.layout_information or [])
        ]
        return boxes, None, time.perf_counter() - started
    except Exception as exc:  # prototype must report failures rather than abort the run
        return [], f"{type(exc).__name__}: {exc}", time.perf_counter() - started


def best_box_index(line: dict[str, Any], boxes: list[dict[str, Any]]) -> int | None:
    line_area = max(1.0, bbox_area(line["bbox"]))
    best_index: int | None = None
    best_score = 0.0
    for index, box in enumerate(boxes):
        overlap = intersection_area(line["bbox"], box["bbox"]) / line_area
        if overlap > best_score:
            best_score = overlap
            best_index = index
    return best_index if best_score >= 0.25 else None


def geometry_order(lines: list[dict[str, Any]], page_width: float) -> tuple[list[dict[str, Any]], str]:
    if len(lines) < 8:
        return sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0])), "top_to_bottom"

    middle = page_width / 2.0
    gutter = page_width * 0.025
    left = [line for line in lines if line["bbox"][2] < middle - gutter]
    right = [line for line in lines if line["bbox"][0] > middle + gutter]
    spanning = [line for line in lines if line not in left and line not in right]
    if len(left) >= 4 and len(right) >= 4:
        top = min(min(line["bbox"][1] for line in left), min(line["bbox"][1] for line in right))
        bottom = max(max(line["bbox"][3] for line in left), max(line["bbox"][3] for line in right))
        top_spanning = [line for line in spanning if line["bbox"][3] <= top + 12]
        bottom_spanning = [line for line in spanning if line["bbox"][1] >= bottom - 12]
        middle_spanning = [
            line for line in spanning if line not in top_spanning and line not in bottom_spanning
        ]
        ordered = sorted(top_spanning, key=lambda line: (line["bbox"][1], line["bbox"][0]))
        ordered += sorted(left, key=lambda line: (line["bbox"][1], line["bbox"][0]))
        ordered += sorted(right, key=lambda line: (line["bbox"][1], line["bbox"][0]))
        ordered += sorted(middle_spanning, key=lambda line: (line["bbox"][1], line["bbox"][0]))
        ordered += sorted(bottom_spanning, key=lambda line: (line["bbox"][1], line["bbox"][0]))
        return ordered, "two_columns"
    return sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0])), "top_to_bottom"


def join_lines(lines: list[dict[str, Any]]) -> str:
    text = ""
    for line in lines:
        current = normalize_space(line["text"])
        if not current:
            continue
        if not text:
            text = current
        elif re.search(r"[\w\u00c0-\u024f]-$", text) and re.match(
            r"^[a-z\u00df-\u024f]", current
        ):
            text = text[:-1] + current
        else:
            text += " " + current
    return normalize_space(text)


def infer_role(role: str, lines: list[dict[str, Any]], page_height: float) -> list[str]:
    roles = [role]
    text = join_lines(lines)
    mean_size = statistics.fmean(line["mean_font_size"] for line in lines) if lines else 0.0
    bbox = combined_bbox(lines)
    if re.fullmatch(r"\d{1,4}", text):
        roles.append("page_number")
    if bbox and bbox[1] > page_height * 0.78 and mean_size < 9.0:
        roles.append("footnote_candidate")
    if role == "text" and len(text) <= 140 and len(text.split()) <= 16:
        roles.append("heading_candidate")
    return roles


def combined_bbox(lines: list[dict[str, Any]]) -> list[float] | None:
    if not lines:
        return None
    return [
        round(min(line["bbox"][0] for line in lines), 3),
        round(min(line["bbox"][1] for line in lines), 3),
        round(max(line["bbox"][2] for line in lines), 3),
        round(max(line["bbox"][3] for line in lines), 3),
    ]


def blocks_from_layout(
    lines: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    page_rect: pymupdf.Rect,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    assigned: dict[int, list[dict[str, Any]]] = defaultdict(list)
    unassigned: list[dict[str, Any]] = []
    for line in lines:
        index = best_box_index(line, boxes)
        if index is None:
            unassigned.append(line)
        else:
            assigned[index].append(line)

    blocks: list[dict[str, Any]] = []
    reading_orders: Counter[str] = Counter()
    for index, box in enumerate(boxes):
        box_lines = assigned.get(index, [])
        if not box_lines:
            continue
        ordered, reading_order = geometry_order(box_lines, page_rect.width)
        reading_orders[reading_order] += 1
        blocks.append(
            {
                "role": box["role"],
                "roles": infer_role(box["role"], ordered, page_rect.height),
                "bbox": box["bbox"],
                "text": join_lines(ordered),
                "source_lines": len(ordered),
                "reading_order": reading_order,
                "model_index": box["model_index"],
            }
        )

    if unassigned:
        ordered, reading_order = geometry_order(unassigned, page_rect.width)
        blocks.append(
            {
                "role": "unclassified",
                "roles": infer_role("unclassified", ordered, page_rect.height),
                "bbox": combined_bbox(ordered),
                "text": join_lines(ordered),
                "source_lines": len(ordered),
                "reading_order": reading_order,
                "model_index": len(boxes),
            }
        )
        reading_orders[reading_order] += 1

    assigned_chars = sum(len(line["text"]) for group in assigned.values() for line in group)
    total_chars = sum(len(line["text"]) for line in lines)
    return blocks, {
        "coverage": round(assigned_chars / max(1, total_chars), 4),
        "unassigned_lines": len(unassigned),
        "reading_orders": dict(reading_orders),
    }


def geometry_fallback_blocks(
    lines: list[dict[str, Any]], page_rect: pymupdf.Rect, role: str
) -> list[dict[str, Any]]:
    by_pdf_block: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        by_pdf_block[int(line["block_index"])].append(line)
    groups = list(by_pdf_block.values())
    representatives = [
        {
            "text": join_lines(group),
            "bbox": combined_bbox(group),
            "lines": group,
            "mean_font_size": statistics.fmean(line["mean_font_size"] for line in group),
        }
        for group in groups
    ]
    ordered, reading_order = geometry_order(representatives, page_rect.width)
    result: list[dict[str, Any]] = []
    for index, group in enumerate(ordered):
        roles = infer_role(role, group["lines"], page_rect.height)
        result.append(
            {
                "role": role,
                "roles": roles,
                "bbox": group["bbox"],
                "text": group["text"],
                "source_lines": len(group["lines"]),
                "reading_order": reading_order,
                "model_index": index,
            }
        )
    return result


def merge_hyphenated_block_continuations(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    text_roles = {"text", "native-block", "ocr-block"}
    for block in blocks:
        if (
            merged
            and merged[-1]["role"] in text_roles
            and block["role"] in text_roles
            and re.search(r"[\w\u00c0-\u024f]-$", merged[-1]["text"])
            and re.match(r"^[a-z\u00df-\u024f]", block["text"])
        ):
            previous = merged[-1]
            previous["text"] = previous["text"][:-1] + block["text"]
            previous["bbox"] = [
                min(previous["bbox"][0], block["bbox"][0]),
                min(previous["bbox"][1], block["bbox"][1]),
                max(previous["bbox"][2], block["bbox"][2]),
                max(previous["bbox"][3], block["bbox"][3]),
            ]
            previous["source_lines"] += block["source_lines"]
            previous["roles"].append("merged_hyphenated_continuation")
            continue
        merged.append(block)
    return merged


def get_ocr_engine(language: str, cache: dict[str, Any]) -> Any:
    if language in cache:
        return cache[language]
    from rapidocr import LangRec, OCRVersion, RapidOCR

    language_value = getattr(LangRec, language.upper())
    cache[language] = RapidOCR(
        params={
            "Global.log_level": "warning",
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.lang_type": language_value,
            "Cls.ocr_version": OCRVersion.PPOCRV5,
        }
    )
    return cache[language]


def render_page(page: pymupdf.Page, dpi: int) -> tuple[bytes, int, int]:
    pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    return pixmap.tobytes("png"), pixmap.width, pixmap.height


def clahe_variant(image_bytes: bytes) -> bytes:
    import cv2

    raw = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    ok, encoded = cv2.imencode(".png", enhanced)
    return encoded.tobytes() if ok else image_bytes


def ocr_candidate(
    engine: Any,
    image_bytes: bytes,
    image_width: int,
    image_height: int,
    page_rect: pymupdf.Rect,
    name: str,
    dpi: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    result = engine(image_bytes)
    elapsed = time.perf_counter() - started
    if result.txts is None or result.boxes is None or result.scores is None:
        return {
            "name": name,
            "dpi": dpi,
            "seconds": round(elapsed, 3),
            "lines": [],
            "chars": 0,
            "mean_confidence": 0.0,
            "text_area_ratio": 0.0,
            "score": 0.0,
        }

    scale_x = page_rect.width / image_width
    scale_y = page_rect.height / image_height
    lines: list[dict[str, Any]] = []
    for box, text, confidence in zip(result.boxes, result.txts, result.scores):
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        pdf_bbox = [
            min(xs) * scale_x,
            min(ys) * scale_y,
            max(xs) * scale_x,
            max(ys) * scale_y,
        ]
        cleaned = normalize_space(str(text))
        if cleaned:
            lines.append(
                {
                    "text": cleaned,
                    "bbox": bbox_tuple(pdf_bbox),
                    "mean_font_size": round(max(5.0, pdf_bbox[3] - pdf_bbox[1]) * 0.75, 3),
                    "confidence": round(float(confidence), 4),
                    "fonts": ["RapidOCR"],
                    "invisible_ratio": 0.0,
                    "block_index": len(lines),
                    "line_index": 0,
                }
            )

    chars = sum(len(line["text"]) for line in lines)
    confidence = statistics.fmean(line["confidence"] for line in lines) if lines else 0.0
    text_area = sum(bbox_area(line["bbox"]) for line in lines)
    area_ratio = min(1.0, text_area / max(1.0, bbox_area(page_rect)))
    yield_score = min(1.0, chars / 800.0)
    area_score = min(1.0, area_ratio / 0.08)
    score = confidence * 0.65 + yield_score * 0.25 + area_score * 0.10
    return {
        "name": name,
        "dpi": dpi,
        "seconds": round(elapsed, 3),
        "lines": lines,
        "chars": chars,
        "mean_confidence": round(confidence, 4),
        "text_area_ratio": round(area_ratio, 4),
        "score": round(score, 4),
    }


def run_selective_ocr(
    page: pymupdf.Page,
    language: str,
    engine_cache: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    engine_started = time.perf_counter()
    engine = get_ocr_engine(language, engine_cache)
    engine_seconds = time.perf_counter() - engine_started

    baseline_bytes, width, height = render_page(page, 200)
    candidates = [
        ocr_candidate(engine, baseline_bytes, width, height, page.rect, "baseline", 200)
    ]
    baseline = candidates[0]
    if baseline["score"] < 0.82 or baseline["chars"] < 200:
        candidates.append(
            ocr_candidate(
                engine,
                clahe_variant(baseline_bytes),
                width,
                height,
                page.rect,
                "clahe",
                200,
            )
        )
        high_bytes, high_width, high_height = render_page(page, 300)
        candidates.append(
            ocr_candidate(
                engine,
                high_bytes,
                high_width,
                high_height,
                page.rect,
                "baseline",
                300,
            )
        )

    selected = max(candidates, key=lambda candidate: candidate["score"])
    if selected is not baseline and selected["score"] < baseline["score"] + 0.06:
        selected = baseline
    selected_lines = selected.pop("lines")
    public_candidates = [{key: value for key, value in item.items() if key != "lines"} for item in candidates]
    sparse = selected["chars"] < 80 or selected["text_area_ratio"] < 0.006
    return {
        "language": language,
        "engine_initialization_seconds": round(engine_seconds, 3),
        "candidates": public_candidates,
        "selected": selected,
        "sparse_result": sparse,
    }, selected_lines


def synthetic_ocr_layout(
    source_rect: pymupdf.Rect, lines: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str | None, float]:
    if not lines:
        return [], None, 0.0
    document = pymupdf.open()
    page = document.new_page(width=source_rect.width, height=source_rect.height)
    font_file = r"C:\Windows\Fonts\arial.ttf"
    for line in lines:
        bbox = line["bbox"]
        font_size = max(5.0, min(18.0, (bbox[3] - bbox[1]) * 0.72))
        page.insert_text(
            (bbox[0], bbox[3] - 1),
            line["text"],
            fontname="ocrfont",
            fontfile=font_file,
            fontsize=font_size,
            render_mode=0,
        )
    boxes, error, seconds = layout_boxes(page)
    document.close()
    return boxes, error, seconds


def clean_text_from_blocks(blocks: list[dict[str, Any]], sparse_ocr: bool = False) -> str:
    if sparse_ocr:
        return ""
    texts = [
        block["text"]
        for block in blocks
        if block["text"]
        and block["role"] not in MARGINAL_ROLES
        and "page_number" not in block["roles"]
    ]
    return "\n\n".join(texts)


def process_page(
    document: pymupdf.Document,
    page_index: int,
    language: str,
    engine_cache: dict[str, Any],
    ocr_mode: str,
) -> dict[str, Any]:
    page = document[page_index]
    native_lines = extract_native_lines(page)
    diagnostics = native_diagnostics(page, native_lines)
    native_boxes, native_layout_error, native_layout_seconds = layout_boxes(page)

    use_ocr = ocr_mode == "force" or (
        ocr_mode == "auto" and bool(diagnostics["auto_ocr"])
    )
    ocr_report: dict[str, Any] | None = None
    lines = native_lines
    boxes = native_boxes
    layout_error = native_layout_error
    layout_seconds = native_layout_seconds
    source = "native"

    if use_ocr:
        source = "ocr"
        ocr_report, lines = run_selective_ocr(page, language, engine_cache)
        boxes, layout_error, layout_seconds = synthetic_ocr_layout(page.rect, lines)

    blocks, layout_metrics = blocks_from_layout(lines, boxes, page.rect)
    fallback_used = False
    has_content_layout = any(box["role"] in LAYOUT_CONTENT_ROLES for box in boxes)
    if lines and (
        not boxes
        or layout_metrics["coverage"] < 0.80
        or (source == "ocr" and not has_content_layout)
    ):
        fallback_role = "ocr-block" if source == "ocr" else "native-block"
        blocks = geometry_fallback_blocks(lines, page.rect, fallback_role)
        fallback_used = True
    blocks = merge_hyphenated_block_continuations(blocks)

    sparse_ocr = bool(ocr_report and ocr_report["sparse_result"])
    return {
        "page_index": page_index,
        "page_number": page_index + 1,
        "page_size": bbox_tuple(page.rect),
        "source": source,
        "ocr_mode": ocr_mode,
        "native_diagnostics": diagnostics,
        "layout": {
            "seconds": round(layout_seconds, 3),
            "error": layout_error,
            "boxes": boxes,
            "box_roles": dict(Counter(box["role"] for box in boxes)),
            "metrics": layout_metrics,
            "fallback_used": fallback_used,
        },
        "ocr": ocr_report,
        "blocks": blocks,
        "clean_text": clean_text_from_blocks(blocks, sparse_ocr=sparse_ocr),
    }


def mark_repeated_marginals(pages: list[dict[str, Any]]) -> None:
    occurrences: Counter[str] = Counter()
    for page in pages:
        for block in page["blocks"]:
            if block["role"] in MARGINAL_ROLES:
                key = normalized_marginal_key(block["text"])
                if key:
                    occurrences[key] += 1
    repeated = {key for key, count in occurrences.items() if count >= 2}
    for page in pages:
        for block in page["blocks"]:
            if block["role"] not in MARGINAL_ROLES:
                continue
            key = normalized_marginal_key(block["text"])
            if key in repeated and "repeated_marginal" not in block["roles"]:
                block["roles"].append("repeated_marginal")


def write_sample_outputs(output_dir: Path, sample: dict[str, Any]) -> None:
    sample_dir = output_dir / sample["key"]
    sample_dir.mkdir(parents=True, exist_ok=True)
    (sample_dir / "report.json").write_text(
        json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    target_page = next(page for page in sample["pages"] if page["is_target"])
    (sample_dir / "target.txt").write_text(target_page["clean_text"], encoding="utf-8")
    block_lines: list[str] = []
    for block in target_page["blocks"]:
        block_lines.append(
            f"[{block['role']} | {', '.join(block['roles'])} | {block['bbox']}]\n{block['text']}\n"
        )
    (sample_dir / "target-blocks.txt").write_text("\n".join(block_lines), encoding="utf-8")


def markdown_summary(report: dict[str, Any]) -> str:
    all_pages = [page for sample in report["samples"] for page in sample["pages"]]
    native_pages = sum(page["source"] == "native" for page in all_pages)
    ocr_pages = len(all_pages) - native_pages
    sparse_pages = sum(bool(page["ocr"] and page["ocr"]["sparse_result"]) for page in all_pages)
    lines = [
        "# Lightweight PDF ingestion prototype",
        "",
        f"Processed {len(report['samples'])} real-book samples in {report['seconds']:.2f} seconds.",
        f"Across {len(all_pages)} pages: {native_pages} native, {ocr_pages} OCR, "
        f"{sparse_pages} sparse OCR results excluded.",
        "",
        "| Sample | Page | Source | Native chars | Layout roles | Coverage | Fallback | OCR result |",
        "|---|---:|---|---:|---|---:|---|---|",
    ]
    for sample in report["samples"]:
        page = next(item for item in sample["pages"] if item["is_target"])
        roles = ", ".join(
            f"{key}:{value}" for key, value in page["layout"]["box_roles"].items()
        ) or "none"
        ocr = "not used"
        if page["ocr"]:
            selected = page["ocr"]["selected"]
            ocr = (
                f"{selected['chars']} chars, {selected['mean_confidence']:.3f} confidence, "
                f"{selected['name']} {selected['dpi']} DPI"
            )
            if page["ocr"]["sparse_result"]:
                ocr += ", sparse/excluded"
        lines.append(
            "| {key} | {page_number} | {source} | {chars} | {roles} | {coverage:.3f} | "
            "{fallback} | {ocr} |".format(
                key=sample["key"],
                page_number=page["page_number"],
                source=page["source"],
                chars=page["native_diagnostics"]["chars"],
                roles=roles,
                coverage=page["layout"]["metrics"]["coverage"],
                fallback="yes" if page["layout"]["fallback_used"] else "no",
                ocr=ocr,
            )
        )
    lines += [
        "",
        "Target-page text and block-level evidence are stored in each sample directory.",
        "",
    ]
    return "\n".join(lines)


def load_samples(report_path: Path, selected_keys: set[str]) -> list[dict[str, Any]]:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    samples = data["samples"]
    if selected_keys:
        samples = [sample for sample in samples if sample["key"] in selected_keys]
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-report", type=Path, default=DEFAULT_SAMPLE_REPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample", action="append", default=[])
    parser.add_argument("--window", type=int, default=1)
    parser.add_argument("--ocr-mode", choices=("auto", "off", "force"), default="auto")
    args = parser.parse_args()

    install_layout_compatibility_patch()
    samples = load_samples(args.sample_report, set(args.sample))
    args.output.mkdir(parents=True, exist_ok=True)
    engine_cache: dict[str, Any] = {}
    report: dict[str, Any] = {
        "prototype": "PyMuPDF native + PyMuPDF Layout + selective RapidOCR PP-OCRv5",
        "sample_report": str(args.sample_report),
        "window": args.window,
        "ocr_mode": args.ocr_mode,
        "samples": [],
    }
    started = time.perf_counter()

    for sample in samples:
        sample_started = time.perf_counter()
        document = pymupdf.open(sample["path"])
        target = int(sample["page_index"])
        page_indices = range(
            max(0, target - args.window),
            min(document.page_count, target + args.window + 1),
        )
        language = LANGUAGE_BY_SAMPLE.get(sample["key"], "en")
        pages = [
            process_page(document, page_index, language, engine_cache, args.ocr_mode)
            for page_index in page_indices
        ]
        for page in pages:
            page["is_target"] = page["page_index"] == target
        mark_repeated_marginals(pages)
        document.close()
        sample_result = {
            "key": sample["key"],
            "path": sample["path"],
            "target_page_index": target,
            "language": language,
            "seconds": round(time.perf_counter() - sample_started, 3),
            "pages": pages,
        }
        report["samples"].append(sample_result)
        write_sample_outputs(args.output, sample_result)
        print(f"{sample['key']}: {sample_result['seconds']:.2f}s")

    report["seconds"] = round(time.perf_counter() - started, 3)
    (args.output / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (args.output / "summary.md").write_text(markdown_summary(report), encoding="utf-8")
    print(f"Report: {args.output / 'summary.md'}")


if __name__ == "__main__":
    main()
