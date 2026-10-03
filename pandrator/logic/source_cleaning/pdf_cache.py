"""PDF ingestion configuration, cache validation and source input identity."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from .models import SourceDocument

PDF_INGESTION_VERSION = 12


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


def _load_cached_document(
    cache_path: str, source_fingerprint: dict[str, Any], config: PDFIngestionConfig
) -> SourceDocument | None:
    try:
        with open(cache_path, "r", encoding="utf-8") as file_handle:
            payload = json.load(file_handle)
        if not isinstance(payload, dict):
            return None
        document = SourceDocument.from_dict(payload)
        ingestion = document.attributes.get("pdf_ingestion", {})
        if not isinstance(ingestion, dict):
            return None
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
    return {
        "path": os.path.abspath(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
        "provenance_fingerprint": _provenance_fingerprint(path),
    }


def _provenance_fingerprint(path: str) -> dict[str, str] | None:
    provenance_path = f"{path}.pycroppdf.json"
    if not os.path.isfile(provenance_path):
        return None
    try:
        digest = hashlib.sha256()
        with open(provenance_path, "rb") as file_handle:
            for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"sha256": digest.hexdigest()}
    except OSError as error:
        # Let the existing manifest loader retain its warning fallback.
        return {"error": str(error)}


def _write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as file_handle:
        json.dump(payload, file_handle, indent=2, ensure_ascii=False)
        file_handle.write("\n")
