"""Geometry-safe PDF edit plans, left/right stacks, and provenance output."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz


PROVENANCE_SCHEMA = "pandrator.pdf-edit"
PROVENANCE_VERSION = 1


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PdfRect:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "PdfRect":
        try:
            rect = cls(*(float(value[key]) for key in ("x0", "y0", "x1", "y1")))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("PDF rectangles require numeric x0, y0, x1, and y1 values.") from error
        if not all(math.isfinite(item) for item in asdict(rect).values()):
            raise ValueError("PDF rectangle values must be finite.")
        if rect.x1 <= rect.x0 or rect.y1 <= rect.y0:
            raise ValueError("PDF rectangle must have positive width and height.")
        return rect

    def fitz(self) -> fitz.Rect:
        return fitz.Rect(self.x0, self.y0, self.x1, self.y1)


@dataclass(frozen=True, slots=True)
class CropOperation:
    original_page: int
    rect: PdfRect


@dataclass(frozen=True, slots=True)
class WhiteoutOperation:
    original_page: int
    rect: PdfRect
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)


@dataclass(frozen=True, slots=True)
class PdfEditPlan:
    first_page_side: str
    crops: tuple[CropOperation, ...]
    whiteouts: tuple[WhiteoutOperation, ...]
    deleted_pages: tuple[int, ...]

    @classmethod
    def from_value(cls, payload: dict[str, Any]) -> "PdfEditPlan":
        first_page_side = str(payload.get("first_page_side") or "right").lower()
        if first_page_side not in {"left", "right"}:
            raise ValueError("first_page_side must be 'left' or 'right'.")
        crops = tuple(
            CropOperation(int(item["original_page"]), PdfRect.from_value(item["rect"]))
            for item in payload.get("crops", [])
        )
        whiteouts = []
        for item in payload.get("whiteouts", []):
            raw_color = item.get("color", [1, 1, 1])
            if not isinstance(raw_color, list) or len(raw_color) != 3:
                raise ValueError("Whiteout color must be an RGB array.")
            color = tuple(max(0.0, min(1.0, float(component))) for component in raw_color)
            whiteouts.append(
                WhiteoutOperation(int(item["original_page"]), PdfRect.from_value(item["rect"]), color)
            )
        deleted = tuple(sorted({int(page) for page in payload.get("deleted_pages", [])}))
        return cls(first_page_side, crops, tuple(whiteouts), deleted)


def page_side(original_page: int, first_page_side: str) -> str:
    first = str(first_page_side).lower()
    if first not in {"left", "right"}:
        raise ValueError("first_page_side must be 'left' or 'right'.")
    return first if original_page % 2 == 0 else ("right" if first == "left" else "left")


def inspect_pdf(path: Path, *, first_page_side: str = "right") -> dict[str, Any]:
    document = fitz.open(path)
    try:
        pages = []
        for index, page in enumerate(document):
            media = page.mediabox
            crop = page.cropbox
            pages.append(
                {
                    "original_page": index,
                    "page_number": index + 1,
                    "side": page_side(index, first_page_side),
                    "rotation": int(page.rotation),
                    "media_box": {"x0": media.x0, "y0": media.y0, "x1": media.x1, "y1": media.y1},
                    "crop_box": {"x0": crop.x0, "y0": crop.y0, "x1": crop.x1, "y1": crop.y1},
                    "width": float(media.width),
                    "height": float(media.height),
                }
            )
        return {"page_count": len(pages), "first_page_side": first_page_side, "pages": pages}
    finally:
        document.close()


def _validate_page_rect(page: fitz.Page, rect: PdfRect, page_index: int) -> fitz.Rect:
    candidate = rect.fitz()
    media = page.mediabox
    if not media.contains(candidate):
        raise ValueError(f"Operation rectangle escapes the MediaBox of page {page_index + 1}.")
    return candidate


def apply_pdf_edit_plan(
    source: Path,
    destination: Path,
    plan: PdfEditPlan,
    *,
    parent_artifact_id: str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination:
        raise ValueError("PDF edits must create a derived file; source overwrite is not allowed.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open(source)
    original_count = document.page_count
    page_indices = set(range(original_count))
    requested_indices = {
        *(operation.original_page for operation in plan.crops),
        *(operation.original_page for operation in plan.whiteouts),
        *plan.deleted_pages,
    }
    invalid = sorted(requested_indices - page_indices)
    if invalid:
        document.close()
        raise ValueError(f"PDF edit plan references nonexistent page indices: {invalid}")

    try:
        crops_by_page = {operation.original_page: operation for operation in plan.crops}
        whiteouts_by_page: dict[int, list[WhiteoutOperation]] = {}
        for operation in plan.whiteouts:
            whiteouts_by_page.setdefault(operation.original_page, []).append(operation)

        operation_log: list[dict[str, Any]] = []
        for original_page in range(original_count):
            page = document[original_page]
            for operation in whiteouts_by_page.get(original_page, []):
                rect = _validate_page_rect(page, operation.rect, original_page)
                page.add_redact_annot(rect, fill=operation.color)
                operation_log.append(
                    {
                        "type": "whiteout",
                        "original_page": original_page,
                        "side": page_side(original_page, plan.first_page_side),
                        "rect": asdict(operation.rect),
                        "color": list(operation.color),
                    }
                )
            if whiteouts_by_page.get(original_page):
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            crop_operation = crops_by_page.get(original_page)
            if crop_operation:
                crop_rect = _validate_page_rect(page, crop_operation.rect, original_page)
                page.set_cropbox(crop_rect)
                operation_log.append(
                    {
                        "type": "crop",
                        "original_page": original_page,
                        "side": page_side(original_page, plan.first_page_side),
                        "rect": asdict(crop_operation.rect),
                    }
                )

        if plan.deleted_pages:
            document.delete_pages(list(plan.deleted_pages))

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.", suffix=".pdf", dir=destination.parent
        )
        os.close(file_descriptor)
        temporary = Path(temporary_name)
        try:
            document.save(temporary, garbage=4, deflate=True)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    finally:
        document.close()

    deleted = set(plan.deleted_pages)
    page_map = [
        {
            "output_page": output_page,
            "original_page": original_page,
            "side": page_side(original_page, plan.first_page_side),
        }
        for output_page, original_page in enumerate(page for page in range(original_count) if page not in deleted)
    ]
    provenance = {
        "schema": PROVENANCE_SCHEMA,
        "version": PROVENANCE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"path": str(source), "sha256": sha256(source), "page_count": original_count},
        "output": {"path": str(destination), "sha256": sha256(destination), "page_count": len(page_map)},
        "parent_artifact_id": parent_artifact_id,
        "coordinate_space": "unrotated_pdf_points",
        "first_page_side": plan.first_page_side,
        "deleted_pages": list(plan.deleted_pages),
        "page_map": page_map,
        "operations": operation_log,
        "operation_order": ["whiteout", "crop", "delete"],
        "pycroppdf_compatibility": {
            "deleted_pages": list(plan.deleted_pages),
            "page_map": page_map,
            "crops": [item for item in operation_log if item["type"] == "crop"],
            "whiteouts": [item for item in operation_log if item["type"] == "whiteout"],
        },
    }
    manifest = destination.with_suffix(destination.suffix + ".pandrator.json")
    temporary_manifest = manifest.with_suffix(manifest.suffix + ".tmp")
    temporary_manifest.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    os.replace(temporary_manifest, manifest)
    return destination, manifest, provenance

