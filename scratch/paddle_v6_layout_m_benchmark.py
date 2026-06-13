"""Compare PP-OCRv6 small and PP-DocLayout-M with the current PDF prototype.

This is deliberately a scratch benchmark, not production ingestion code.
PP-OCRv6 small runs through ONNX Runtime. PP-DocLayout-M currently requires
Paddle's CPU runtime because Paddle does not publish an ONNX package for it.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
import pymupdf


DEFAULT_SAMPLE_REPORT = Path(r"E:\Pandrator\.tmp\pdf-realbooks-probe\report.json")
DEFAULT_OUTPUT = Path(r"E:\Pandrator\.tmp\paddle-v6-layout-m\benchmark")
DEFAULT_CACHE = Path(r"E:\Pandrator\.tmp\paddle-model-cache")
LANGUAGE_BY_SAMPLE = {
    "cyrillic": "cyrillic",
    "historical_existing_ocr": "latin",
    "historical_no_text": "latin",
    "polish_scan": "latin",
}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def similarity(first: str, second: str) -> float | None:
    first = normalize_text(first).casefold()
    second = normalize_text(second).casefold()
    if len(first) < 40 or len(second) < 40:
        return None
    return round(difflib.SequenceMatcher(None, first, second, autojunk=False).ratio(), 4)


def json_ready(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


def bbox_area(bbox: Iterable[float]) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def intersection_area(first: Iterable[float], second: Iterable[float]) -> float:
    ax0, ay0, ax1, ay1 = first
    bx0, by0, bx1, by1 = second
    return max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(
        0.0, min(ay1, by1) - max(ay0, by0)
    )


def line_coverage(lines: list[list[float]], boxes: list[list[float]]) -> float:
    if not lines:
        return 0.0
    covered = 0
    for line in lines:
        area = max(1.0, bbox_area(line))
        if any(intersection_area(line, box) / area >= 0.5 for box in boxes):
            covered += 1
    return round(covered / len(lines), 4)


def install_pymupdf_layout_patch() -> None:
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


def native_page_data(page: pymupdf.Page) -> tuple[str, list[list[float]]]:
    text = page.get_text("text")
    boxes: list[list[float]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            if normalize_text("".join(span.get("text", "") for span in line.get("spans", []))):
                boxes.append([float(value) for value in line["bbox"]])
    return text, boxes


def render_page(page: pymupdf.Page, target: Path, dpi: int) -> tuple[np.ndarray, float, float]:
    pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    pixmap.save(target)
    image = cv2.imread(str(target), cv2.IMREAD_COLOR)
    return image, page.rect.width / pixmap.width, page.rect.height / pixmap.height


def run_pymupdf_layout(page: pymupdf.Page, native_lines: list[list[float]]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        page.get_layout()
        raw_boxes = page.layout_information or []
        boxes = [[float(value) for value in item[:4]] for item in raw_boxes]
        roles = Counter(str(item[4]) for item in raw_boxes)
        return {
            "seconds": round(time.perf_counter() - started, 4),
            "box_count": len(boxes),
            "roles": dict(roles),
            "native_line_coverage": line_coverage(native_lines, boxes),
            "boxes_pdf": boxes,
            "error": None,
        }
    except Exception as exc:
        return {
            "seconds": round(time.perf_counter() - started, 4),
            "box_count": 0,
            "roles": {},
            "native_line_coverage": 0.0,
            "boxes_pdf": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_paddle_layout(
    model: Any,
    image_path: Path,
    native_lines: list[list[float]],
    scale_x: float,
    scale_y: float,
) -> tuple[dict[str, Any], Any]:
    started = time.perf_counter()
    result = list(model.predict(str(image_path), batch_size=1, layout_nms=True))[0]
    seconds = time.perf_counter() - started
    boxes = result["boxes"]
    image_boxes = [[float(value) for value in item["coordinate"]] for item in boxes]
    pdf_boxes = [
        [box[0] * scale_x, box[1] * scale_y, box[2] * scale_x, box[3] * scale_y]
        for box in image_boxes
    ]
    scores = [float(item["score"]) for item in boxes]
    report = {
        "seconds": round(seconds, 4),
        "box_count": len(boxes),
        "roles": dict(Counter(str(item["label"]) for item in boxes)),
        "mean_confidence": round(statistics.fmean(scores), 4) if scores else 0.0,
        "native_line_coverage": line_coverage(native_lines, pdf_boxes),
        "boxes_image": image_boxes,
        "boxes_pdf": pdf_boxes,
    }
    return report, result


def run_v6_ocr(model: Any, image_path: Path) -> tuple[dict[str, Any], Any]:
    started = time.perf_counter()
    result = list(model.predict(str(image_path)))[0]
    seconds = time.perf_counter() - started
    texts = [normalize_text(str(text)) for text in result["rec_texts"]]
    texts = [text for text in texts if text]
    scores = [float(score) for score in result["rec_scores"]]
    report = {
        "seconds": round(seconds, 4),
        "line_count": len(texts),
        "chars": sum(len(text) for text in texts),
        "mean_confidence": round(statistics.fmean(scores), 4) if scores else 0.0,
        "text": "\n".join(texts),
    }
    return report, result


def rapid_engine(language: str, cache: dict[str, Any]) -> Any:
    if language in cache:
        return cache[language]
    from rapidocr import LangRec, OCRVersion, RapidOCR

    cache[language] = RapidOCR(
        params={
            "Global.log_level": "warning",
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.lang_type": getattr(LangRec, language.upper()),
            "Cls.ocr_version": OCRVersion.PPOCRV5,
        }
    )
    return cache[language]


def run_rapid_ocr(engine: Any, image_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    result = engine(str(image_path))
    seconds = time.perf_counter() - started
    texts = [normalize_text(str(text)) for text in (result.txts or [])]
    texts = [text for text in texts if text]
    scores = [float(score) for score in (result.scores or [])]
    return {
        "seconds": round(seconds, 4),
        "line_count": len(texts),
        "chars": sum(len(text) for text in texts),
        "mean_confidence": round(statistics.fmean(scores), 4) if scores else 0.0,
        "text": "\n".join(texts),
    }


def write_sample_outputs(
    sample_dir: Path,
    image: np.ndarray,
    sample: dict[str, Any],
    layout_result: Any,
    ocr_result: Any,
) -> None:
    sample_dir.mkdir(parents=True, exist_ok=True)
    public = json_ready(sample)
    (sample_dir / "report.json").write_text(
        json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    comparison = [
        "# NATIVE",
        sample["native_text"],
        "",
        "# PP-OCRV6 SMALL",
        sample["ocr_v6"]["text"],
        "",
        "# RAPIDOCR PP-OCRV5",
        sample["rapidocr_v5"]["text"],
        "",
    ]
    (sample_dir / "text-comparison.txt").write_text("\n".join(comparison), encoding="utf-8")
    cv2.imwrite(str(sample_dir / "page.png"), image)
    layout_result.save_to_img(str(sample_dir / "layout-m"))
    ocr_result.save_to_img(str(sample_dir / "ocr-v6"))


def markdown_summary(report: dict[str, Any]) -> str:
    lines = [
        "# PP-OCRv6 small + PP-DocLayout-M benchmark",
        "",
        (
            f"Model initialization: OCR {report['model_initialization']['ocr_seconds']:.2f}s; "
            f"layout {report['model_initialization']['layout_seconds']:.2f}s."
        ),
        "",
        "| Sample | V6 sec | V5 sec | V6 conf | V5 conf | V6/native | V5/native | "
        "Layout M sec | M roles | M/native coverage | PyMuPDF roles |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---|",
    ]
    for sample in report["samples"]:
        v6 = sample["ocr_v6"]
        v5 = sample["rapidocr_v5"]
        paddle = sample["paddle_layout_m"]
        pymu = sample["pymupdf_layout"]
        lines.append(
            "| {key} | {v6s:.2f} | {v5s:.2f} | {v6c:.3f} | {v5c:.3f} | {v6n} | "
            "{v5n} | {ls:.2f} | {lr} | {lc:.3f} | {pr} |".format(
                key=sample["key"],
                v6s=v6["seconds"],
                v5s=v5["seconds"],
                v6c=v6["mean_confidence"],
                v5c=v5["mean_confidence"],
                v6n=v6["similarity_to_native"],
                v5n=v5["similarity_to_native"],
                ls=paddle["seconds"],
                lr=", ".join(f"{key}:{value}" for key, value in paddle["roles"].items()) or "none",
                lc=paddle["native_line_coverage"],
                pr=", ".join(f"{key}:{value}" for key, value in pymu["roles"].items()) or "none",
            )
        )
    lines += [
        "",
        "Similarity to native text is evidence only; it is not a valid accuracy score when "
        "the PDF has no text layer or its existing OCR is poor.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-report", type=Path, default=DEFAULT_SAMPLE_REPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--sample", action="append", default=[])
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args()

    os.environ["PADDLE_PDX_CACHE_HOME"] = str(args.cache)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    install_pymupdf_layout_patch()

    from paddleocr import LayoutDetection, PaddleOCR

    args.output.mkdir(parents=True, exist_ok=True)
    raw_samples = json.loads(args.sample_report.read_text(encoding="utf-8"))["samples"]
    if args.sample:
        raw_samples = [sample for sample in raw_samples if sample["key"] in set(args.sample)]

    started = time.perf_counter()
    init_started = time.perf_counter()
    layout_model = LayoutDetection(
        model_name="PP-DocLayout-M",
        engine="paddle_static",
        device="cpu",
        enable_mkldnn=False,
    )
    layout_init = time.perf_counter() - init_started
    init_started = time.perf_counter()
    ocr_model = PaddleOCR(
        text_detection_model_name="PP-OCRv6_small_det",
        text_recognition_model_name="PP-OCRv6_small_rec",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        engine="onnxruntime",
        device="cpu",
    )
    ocr_init = time.perf_counter() - init_started

    report: dict[str, Any] = {
        "models": {
            "ocr": "PP-OCRv6_small_det_onnx + PP-OCRv6_small_rec_onnx",
            "layout": "PP-DocLayout-M paddle_static CPU, MKL-DNN disabled",
            "baseline_ocr": "RapidOCR PP-OCRv5",
            "baseline_layout": "PyMuPDF Layout",
        },
        "model_initialization": {
            "ocr_seconds": round(ocr_init, 4),
            "layout_seconds": round(layout_init, 4),
        },
        "dpi": args.dpi,
        "samples": [],
    }
    rapid_cache: dict[str, Any] = {}
    image_dir = args.output / "images"
    image_dir.mkdir(exist_ok=True)

    for raw_sample in raw_samples:
        sample_started = time.perf_counter()
        document = pymupdf.open(raw_sample["path"])
        page = document[int(raw_sample["page_index"])]
        native_text, native_lines = native_page_data(page)
        image_path = image_dir / f"{raw_sample['key']}.png"
        image, scale_x, scale_y = render_page(page, image_path, args.dpi)
        pymupdf_report = run_pymupdf_layout(page, native_lines)
        paddle_report, layout_result = run_paddle_layout(
            layout_model, image_path, native_lines, scale_x, scale_y
        )
        v6_report, ocr_result = run_v6_ocr(ocr_model, image_path)
        language = LANGUAGE_BY_SAMPLE.get(raw_sample["key"], "en")
        rapid_report = run_rapid_ocr(rapid_engine(language, rapid_cache), image_path)
        document.close()

        v6_report["similarity_to_native"] = similarity(v6_report["text"], native_text)
        rapid_report["similarity_to_native"] = similarity(rapid_report["text"], native_text)
        v6_report["similarity_to_rapidocr_v5"] = similarity(
            v6_report["text"], rapid_report["text"]
        )
        sample = {
            "key": raw_sample["key"],
            "path": raw_sample["path"],
            "page_index": raw_sample["page_index"],
            "page_number": int(raw_sample["page_index"]) + 1,
            "language": language,
            "seconds": round(time.perf_counter() - sample_started, 4),
            "native_chars": len(native_text),
            "native_lines": len(native_lines),
            "native_text": native_text,
            "pymupdf_layout": pymupdf_report,
            "paddle_layout_m": paddle_report,
            "ocr_v6": v6_report,
            "rapidocr_v5": rapid_report,
        }
        report["samples"].append(sample)
        write_sample_outputs(args.output / raw_sample["key"], image, sample, layout_result, ocr_result)
        print(f"{raw_sample['key']}: {sample['seconds']:.2f}s", flush=True)

    report["seconds"] = round(time.perf_counter() - started, 4)
    public_report = json_ready(report)
    (args.output / "report.json").write_text(
        json.dumps(public_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (args.output / "summary.md").write_text(markdown_summary(public_report), encoding="utf-8")
    print(f"Report: {args.output / 'summary.md'}")


if __name__ == "__main__":
    main()
