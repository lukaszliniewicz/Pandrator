"""Read-only previews of accepted native dispatch batch outputs."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from math import isfinite
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from .models import DispatchBatch, DispatchRun

if TYPE_CHECKING:
    from .application_services import ApplicationServices


DEFAULT_PREVIEW_LIMIT = 20
MAX_PREVIEW_LIMIT = 100
MAX_SOURCE_CUES = 500

_OUTPUT_ROW_FIELDS = (
    "text",
    "start_ms",
    "end_ms",
    "speaker",
    "turn_id",
    "timing_basis",
    "timing",
    "gap_from_previous_ms",
    "overlap_with_previous_ms",
    "starts_new_turn",
    "source_cue_ids",
    "split_boundary_ids",
    "split_part_index",
    "split_part_count",
    "review_state",
    "review_note",
    "evidence_ids",
    "uncertain_source_cue_ids",
)


def _bounded_int(value: object, *, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer.")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ValueError(f"{name} must be at least {minimum}.")
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _output_row(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value[key]) for key in _OUTPUT_ROW_FIELDS if key in value}


def _milliseconds(value: object, *, seconds: bool = False) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not isfinite(numeric):
        return None
    return int(round(numeric * 1000)) if seconds else int(numeric)


def _source_cue(value: Mapping[str, Any], *, cue_id: int) -> dict[str, Any]:
    passage = value.get("_passage")
    passage = passage if isinstance(passage, Mapping) else {}
    timing: dict[str, int] = {}
    for key in ("start_ms", "end_ms"):
        milliseconds = _milliseconds(passage.get(key))
        if milliseconds is None:
            milliseconds = _milliseconds(
                value.get("start" if key == "start_ms" else "end"), seconds=True
            )
        if milliseconds is not None:
            timing[key] = milliseconds
    for key in ("gap_from_previous_ms", "overlap_with_previous_ms"):
        milliseconds = _milliseconds(passage.get(key))
        if milliseconds is not None:
            timing[key] = milliseconds

    speaker = str(value.get("speaker") or "").strip() or None
    turn_id = str(passage.get("turn_id") or "").strip() or None
    timing_basis = str(passage.get("timing_basis") or "").strip() or None
    return {
        "cue_id": cue_id,
        "text": str(value.get("text") or ""),
        "speaker": speaker,
        "turn_id": turn_id,
        "timing_basis": timing_basis,
        "timing": timing,
    }


def _source_cue_map(batch: DispatchBatch) -> dict[int, dict[str, Any]] | None:
    inputs = batch.input_json
    if not isinstance(inputs, list):
        return None
    result: dict[int, dict[str, Any]] = {}
    for item in inputs:
        if not isinstance(item, dict):
            return None
        raw_id = item.get("index")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id in result:
            return None
        result[raw_id] = _source_cue(item, cue_id=raw_id)
    return result


def _paired_source_cues(
    output_rows: list[dict[str, Any]],
    batch: DispatchBatch,
) -> tuple[str, list[dict[str, Any]]]:
    if not output_rows:
        return "unavailable", []
    cue_map = _source_cue_map(batch)
    if cue_map is None:
        return "unavailable", []

    referenced_ids: list[int] = []
    seen: set[int] = set()
    for row in output_rows:
        raw_ids = row.get("source_cue_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return "unavailable", []
        for raw_id in raw_ids:
            if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id not in cue_map:
                return "unavailable", []
            if raw_id not in seen:
                seen.add(raw_id)
                referenced_ids.append(raw_id)

    if len(referenced_ids) > MAX_SOURCE_CUES:
        return "unavailable", []
    return "source_cue_ids", [cue_map[cue_id] for cue_id in referenced_ids]


def get_dispatch_preview(
    services: ApplicationServices,
    run_id: str,
    *,
    batch_ordinal: int | None = None,
    offset: int = 0,
    limit: int = DEFAULT_PREVIEW_LIMIT,
) -> dict[str, Any]:
    """Return a bounded preview of a persisted accepted batch without publishing it.

    ``batch_ordinal`` is the one-based ordinal exposed by the native dispatch API.
    When omitted, the earliest accepted batch is selected.
    """
    offset = _bounded_int(offset, name="offset", minimum=0)
    limit = _bounded_int(
        limit,
        name="limit",
        minimum=1,
        maximum=MAX_PREVIEW_LIMIT,
    )
    if batch_ordinal is not None:
        batch_ordinal = _bounded_int(
            batch_ordinal,
            name="batch_ordinal",
            minimum=1,
        )

    with services.database.snapshot_session() as session:
        run = session.get(DispatchRun, run_id)
        if run is None:
            raise KeyError(run_id)

        if batch_ordinal is None:
            batch = session.scalar(
                select(DispatchBatch)
                .where(
                    DispatchBatch.dispatch_run_id == run.id,
                    DispatchBatch.status == "completed",
                )
                .order_by(DispatchBatch.ordinal)
                .limit(1)
            )
        else:
            batch = session.scalar(
                select(DispatchBatch).where(
                    DispatchBatch.dispatch_run_id == run.id,
                    DispatchBatch.ordinal == batch_ordinal - 1,
                )
            )
            if batch is not None and batch.status != "completed":
                raise ValueError("The selected dispatch batch has not been accepted.")
        if batch is None:
            raise ValueError("The dispatch run has no accepted batch for that ordinal.")

        if run.kind not in {"correction", "translation"}:
            raise ValueError("The dispatch run has an unsupported kind.")
        normalized = batch.normalized_output_json
        if normalized is None:
            normalized = []
        if not isinstance(normalized, list) or any(
            not isinstance(item, dict) for item in normalized
        ):
            raise ValueError("The accepted dispatch output has an invalid stored shape.")

        total_count = len(normalized)
        page = [_output_row(item) for item in normalized[offset : offset + limit]]
        source_pairing, source_cues = _paired_source_cues(page, batch)
        next_offset = offset + len(page) if offset + len(page) < total_count else None

        return {
            "schema_version": "1",
            "run_id": run.id,
            "status": run.status,
            "kind": run.kind,
            "source_revision_id": run.source_revision_id,
            "source_content_hash": run.source_content_hash,
            "batch_id": batch.id,
            "batch_ordinal": batch.ordinal + 1,
            "partial": True,
            "published": bool(run.result_artifact_id),
            "offset": offset,
            "limit": limit,
            "output_count": len(page),
            "total_count": total_count,
            "next_offset": next_offset,
            "output_rows": page,
            "source_pairing": source_pairing,
            "source_cues": source_cues,
        }
