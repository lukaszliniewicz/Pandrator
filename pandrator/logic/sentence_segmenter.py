"""Lazy wtpsplit-lite sentence segmentation for preprocessing."""

import logging
import re
import threading


WTPSPLIT_MODEL = "sat-12l-sm"
WTPSPLIT_THRESHOLD = 0.05
_PARAGRAPH_BOUNDARY_RE = re.compile(r"\r?\n+")
_SEGMENTER = None
_SEGMENTER_FAILED = False
_SEGMENTER_LOCK = threading.RLock()

# Load ONNX Runtime before SciPy-family native libraries to avoid a Windows DLL collision.
try:
    import onnxruntime as _onnxruntime
except Exception as exc:
    _ONNX_RUNTIME_IMPORT_ERROR = exc
else:
    _ONNX_RUNTIME_IMPORT_ERROR = None


def _create_segmenter():
    if _ONNX_RUNTIME_IMPORT_ERROR is not None:
        raise RuntimeError(f"ONNX Runtime could not be loaded: {_ONNX_RUNTIME_IMPORT_ERROR}")

    from wtpsplit_lite import SaT

    return SaT(WTPSPLIT_MODEL, ort_providers=["CPUExecutionProvider"])


def _get_segmenter():
    global _SEGMENTER, _SEGMENTER_FAILED

    with _SEGMENTER_LOCK:
        if _SEGMENTER_FAILED:
            return None
        if _SEGMENTER is not None:
            return _SEGMENTER

        try:
            _SEGMENTER = _create_segmenter()
        except Exception as exc:
            _SEGMENTER_FAILED = True
            logging.warning("wtpsplit-lite sentence segmentation is unavailable: %s", exc)
            return None

        return _SEGMENTER


def is_available() -> bool:
    """Return whether the shared wtpsplit-lite model can be loaded."""
    return _get_segmenter() is not None


def split_text(text: str) -> list[str] | None:
    """Return robust sentence segments, or None when wtpsplit-lite is unavailable."""
    if not text:
        return []

    segmenter = _get_segmenter()
    if segmenter is None:
        return None

    segments = []
    try:
        with _SEGMENTER_LOCK:
            for paragraph in _PARAGRAPH_BOUNDARY_RE.split(text):
                if not paragraph.strip():
                    continue
                paragraph_segments = segmenter.split(
                    paragraph,
                    threshold=WTPSPLIT_THRESHOLD,
                    stride=128,
                    block_size=256,
                    weighting="hat",
                    treat_newline_as_space=True,
                )
                segments.extend(segment.strip() for segment in paragraph_segments if segment.strip())
    except Exception as exc:
        logging.warning("wtpsplit-lite skipped sentence segmentation: %s", exc)
        return None

    return segments
