"""Compare PP-OCRv6 medium ONNX with the existing small-model benchmark."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np


DEFAULT_BASELINE = Path(r"E:\Pandrator\.tmp\paddle-v6-layout-m\benchmark-full\report.json")
DEFAULT_IMAGES = Path(r"E:\Pandrator\.tmp\paddle-v6-layout-m\benchmark-full\images")
DEFAULT_OUTPUT = Path(r"E:\Pandrator\.tmp\paddle-v6-medium\benchmark")
DEFAULT_CACHE = Path(r"E:\Pandrator\.tmp\paddle-model-cache")


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


def markdown_summary(report: dict[str, Any]) -> str:
    lines = [
        "# PP-OCRv6 medium benchmark",
        "",
        f"Medium model initialization: {report['model_initialization_seconds']:.2f}s.",
        "",
        "| Sample | Medium sec | Small sec | Medium conf | Small conf | Medium/native | "
        "Small/native | Medium/small |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for sample in report["samples"]:
        medium = sample["medium"]
        small = sample["small"]
        lines.append(
            "| {key} | {medium_seconds:.2f} | {small_seconds:.2f} | {medium_conf:.3f} | "
            "{small_conf:.3f} | {medium_native} | {small_native} | {medium_small} |".format(
                key=sample["key"],
                medium_seconds=medium["seconds"],
                small_seconds=small["seconds"],
                medium_conf=medium["mean_confidence"],
                small_conf=small["mean_confidence"],
                medium_native=medium["similarity_to_native"],
                small_native=small["similarity_to_native"],
                medium_small=medium["similarity_to_small"],
            )
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--images", type=Path, default=DEFAULT_IMAGES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args()

    os.environ["PADDLE_PDX_CACHE_HOME"] = str(args.cache)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

    from paddleocr import PaddleOCR

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    init_started = time.perf_counter()
    model = PaddleOCR(
        text_detection_model_name="PP-OCRv6_medium_det",
        text_recognition_model_name="PP-OCRv6_medium_rec",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        engine="onnxruntime",
        device="cpu",
    )
    init_seconds = time.perf_counter() - init_started
    report: dict[str, Any] = {
        "model": "PP-OCRv6_medium_det_onnx + PP-OCRv6_medium_rec_onnx",
        "baseline": str(args.baseline),
        "model_initialization_seconds": round(init_seconds, 4),
        "samples": [],
    }

    for baseline_sample in baseline["samples"]:
        key = baseline_sample["key"]
        image_path = args.images / f"{key}.png"
        sample_started = time.perf_counter()
        result = list(model.predict(str(image_path)))[0]
        seconds = time.perf_counter() - sample_started
        texts = [normalize_text(str(text)) for text in result["rec_texts"]]
        texts = [text for text in texts if text]
        scores = [float(score) for score in result["rec_scores"]]
        text = "\n".join(texts)
        small = baseline_sample["ocr_v6"]
        medium = {
            "seconds": round(seconds, 4),
            "line_count": len(texts),
            "chars": sum(len(item) for item in texts),
            "mean_confidence": round(statistics.fmean(scores), 4) if scores else 0.0,
            "similarity_to_native": similarity(text, baseline_sample["native_text"]),
            "similarity_to_small": similarity(text, small["text"]),
            "text": text,
        }
        report["samples"].append(
            {
                "key": key,
                "language": baseline_sample["language"],
                "medium": medium,
                "small": {
                    "seconds": small["seconds"],
                    "chars": small["chars"],
                    "mean_confidence": small["mean_confidence"],
                    "similarity_to_native": small["similarity_to_native"],
                    "text": small["text"],
                },
            }
        )
        sample_dir = args.output / key
        sample_dir.mkdir(exist_ok=True)
        (sample_dir / "medium.txt").write_text(text, encoding="utf-8")
        result.save_to_img(str(sample_dir / "ocr-medium"))
        print(f"{key}: {seconds:.2f}s", flush=True)

    report["seconds"] = round(time.perf_counter() - started, 4)
    public = json_ready(report)
    (args.output / "report.json").write_text(
        json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (args.output / "summary.md").write_text(markdown_summary(public), encoding="utf-8")
    print(f"Report: {args.output / 'summary.md'}")


if __name__ == "__main__":
    main()
