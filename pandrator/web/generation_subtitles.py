"""Freeze reviewed display subtitles and project them onto their source timing."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.srt_utils import compose_srt, parse_srt

from .logical_passages import passage_srt, stored_passages
from .models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    new_id,
)
from .workflow_output_context import OutputWorkflowContext


def capture_display_subtitle_snapshot(
    session: Session, session_id: str, settings: dict[str, Any]
) -> dict[str, Any] | None:
    """Capture the chosen revision's display layer while the export is queued."""
    run_id = str(settings.get("generation_run_id") or "").strip()
    if run_id:
        run = session.get(GenerationRun, run_id)
        if run is None or run.session_id != session_id:
            raise ValueError("The selected generation run is unavailable in this session.")
        revision = session.get(GenerationPlanRevision, run.plan_revision_id)
    else:
        plan = session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == session_id)
        )
        revision = (
            session.get(GenerationPlanRevision, plan.active_revision_id)
            if plan is not None and plan.active_revision_id
            else None
        )
    if revision is None:
        if run_id:
            raise ValueError("The selected generation run's speech revision is unavailable.")
        return None
    plan = session.get(GenerationPlan, revision.plan_id)
    if plan is None or plan.session_id != session_id:
        raise ValueError("The selected speech revision belongs to another session.")
    source_id = str((revision.settings_json or {}).get("_source_artifact_id") or "")
    if not source_id:
        return None
    source = session.get(Artifact, source_id)
    if source is None or source.session_id != session_id:
        raise ValueError("The speech revision's subtitle source is unavailable in this session.")
    display = source
    if source.role == "tts_optimized":
        display_id = str((source.metadata_json or {}).get("source_artifact_id") or "")
        display = session.get(Artifact, display_id) if display_id else None
        if display is None or display.session_id != session_id:
            raise ValueError("The optimized speech source has no matching display subtitles.")
    if Path(display.relative_path).suffix.lower() != ".srt":
        return None
    rows = list(
        session.scalars(
            select(GenerationSegment)
            .where(GenerationSegment.plan_revision_id == revision.id)
            .order_by(GenerationSegment.ordinal)
        ).all()
    )
    if not rows:
        return None
    namespaces = {
        str((row.speech_block_provenance_json or {}).get("source_reference_namespace")
            or "subtitle_ordinal")
        for row in rows
    }
    if len(namespaces) != 1 or next(iter(namespaces)) not in {
        "subtitle_ordinal", "logical_passage_ordinal"
    }:
        raise ValueError(
            "The speech revision mixes subtitle reference namespaces. "
            "Prepare a new speech plan before exporting."
        )
    namespace = next(iter(namespaces))
    logical_srt = None
    if namespace == "logical_passage_ordinal":
        passages = stored_passages(display)
        if passages is None:
            raise ValueError(
                "The speech revision's logical passages are no longer verified "
                "for these display subtitles. Restore them or prepare a new speech plan."
            )
        logical_srt = passage_srt(passages)
    return {
        "version": 1,
        "revision_id": revision.id,
        "generation_run_id": run_id or None,
        "display_artifact_id": display.id,
        "display_content_hash": display.content_hash,
        "display_source_role": (display.metadata_json or {}).get("source_role") or display.role,
        "display_language": (display.metadata_json or {}).get("language"),
        "source_reference_namespace": namespace,
        "logical_passage_srt": logical_srt,
        "segments": [
            {
                "ordinal": row.ordinal,
                "text": row.text,
                "removed": row.removed,
                "source_refs": deepcopy(row.source_segment_ids_json or []),
                "provenance": deepcopy(row.speech_block_provenance_json or {}),
            }
            for row in rows
        ],
    }


def _reference(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("A reviewed subtitle row has a malformed source cue reference. Prepare a new speech plan before exporting.")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        result = int(value)
    else:
        raise ValueError("A reviewed subtitle row has a malformed source cue reference. Prepare a new speech plan before exporting.")
    if result <= 0:
        raise ValueError("A reviewed subtitle row has a nonpositive source cue reference. Prepare a new speech plan before exporting.")
    return result


def _validated_provenance_spans(
    display_text: str, references: set[int], provenance: dict[str, Any]
) -> dict[int, str] | None:
    """Use only nonoverlapping spans covering all non-whitespace display text."""
    source_cues = provenance.get("source_cues") or []
    if not isinstance(source_cues, list) or not source_cues:
        return None
    seen: set[int] = set()
    covered = [False] * len(display_text)
    texts: dict[int, str] = {}
    for cue in source_cues:
        if not isinstance(cue, dict):
            return None
        reference = _reference(cue.get("reference"))
        spans = cue.get("display_spans") or []
        if reference not in references or not isinstance(spans, list) or not spans or reference in seen:
            return None
        pieces: list[str] = []
        for span in spans:
            if not isinstance(span, (list, tuple)) or len(span) != 2:
                return None
            try:
                start, end = int(span[0]), int(span[1])
            except (TypeError, ValueError):
                return None
            if start < 0 or end <= start or end > len(display_text):
                return None
            if any(covered[start:end]):
                return None
            covered[start:end] = [True] * (end - start)
            pieces.append(display_text[start:end])
        texts[reference] = " ".join(piece.strip() for piece in pieces).strip()
        if not texts[reference] or texts[reference] != str(
            cue.get("display_text") or ""
        ).strip():
            return None
        seen.add(reference)
    if seen != references or any(
        not is_covered and not character.isspace()
        for character, is_covered in zip(display_text, covered, strict=True)
    ):
        return None
    return texts


def project_display_srt(source_srt: str, snapshot: dict[str, Any]) -> str:
    """Keep source cue timing, changing only display text owned by the plan."""
    cues = parse_srt(source_srt)
    cue_by_index = {cue.index: cue for cue in cues}
    if len(cue_by_index) != len(cues):
        raise ValueError("Subtitle cue numbers are ambiguous; renumber the source subtitles and export again.")
    rows = snapshot.get("segments")
    if not isinstance(rows, list):
        raise ValueError("The queued display subtitle snapshot is malformed; submit the export again.")
    prepared: list[tuple[int, str, bool, tuple[int, ...], dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("The queued display subtitle snapshot is malformed; submit the export again.")
        provenance = dict(row.get("provenance") or {})
        raw_refs = row.get("source_refs") or []
        if not isinstance(raw_refs, list):
            raise ValueError("The queued subtitle source references are malformed; submit the export again.")
        refs = tuple(dict.fromkeys(_reference(value) for value in raw_refs))
        source_cues = provenance.get("source_cues") or []
        if not isinstance(source_cues, list):
            raise ValueError("The queued subtitle provenance is malformed; submit the export again.")
        provenance_refs = []
        for cue in source_cues:
            if not isinstance(cue, dict):
                raise ValueError("The queued subtitle provenance is malformed; submit the export again.")
            provenance_refs.append(_reference(cue.get("reference")))
        if not refs:
            refs = tuple(dict.fromkeys(provenance_refs))
        if not refs or any(ref not in cue_by_index for ref in refs):
            raise ValueError(
                "A reviewed subtitle row has no usable source cue references. "
                "Restore its source subtitles or prepare a new speech plan before exporting."
            )
        prepared.append((
            int(row.get("ordinal") or 0), str(row.get("text") or "").strip(),
            bool(row.get("removed")), refs,
            provenance,
        ))
    prepared.sort(key=lambda item: item[0])
    # Connected rows sharing any source cue own one timing window. This also
    # handles splits without emitting the same original cue twice.
    groups: list[list[tuple[int, str, bool, tuple[int, ...], dict[str, Any]]]] = []
    for row in prepared:
        overlapping = [group for group in groups if any(
            set(row[3]).intersection(member[3]) for member in group
        )]
        if overlapping:
            primary = overlapping[0]
            primary.append(row)
            for other in overlapping[1:]:
                primary.extend(other)
                groups.remove(other)
        else:
            groups.append([row])
    owned: set[int] = set()
    replacements = []
    for group in groups:
        group.sort(key=lambda item: item[0])
        refs = set(ref for row in group for ref in row[3])
        owned.update(refs)
        active = [row for row in group if not row[2]]
        if not active:
            continue
        # A still-identical multi-cue block retains its original individual
        # cue text and boundaries. Edited merged blocks use the union window.
        if len(group) == 1 and len(refs) > 1:
            row = group[0]
            source_text = " ".join(cue_by_index[ref].text.strip() for ref in sorted(refs))
            span_texts = _validated_provenance_spans(row[1], refs, row[4])
            if span_texts is not None:
                replacements.extend(
                    replace(cue_by_index[ref], text=span_texts[ref])
                    for ref in sorted(refs)
                )
                continue
            if row[1] == source_text:
                replacements.extend(cue_by_index[ref] for ref in sorted(refs))
                continue
        text = " ".join(row[1] for row in active if row[1]).strip()
        if not text:
            continue
        windows = [cue_by_index[ref] for ref in refs]
        first = min(windows, key=lambda cue: cue.start_ms)
        replacements.append(replace(
            first, start_ms=min(cue.start_ms for cue in windows),
            end_ms=max(cue.end_ms for cue in windows), text=text,
        ))
    replacements.extend(cue for cue in cues if cue.index not in owned)
    replacements.sort(key=lambda cue: (cue.start_ms, cue.end_ms, cue.index))
    return compose_srt(replacements)


def derive_display_subtitle_artifact(
    context: OutputWorkflowContext,
    selected: list[Artifact],
    snapshot: dict[str, Any] | None,
    *,
    session_id: str,
) -> list[Artifact]:
    """Replace only the selected display track with a managed derived SRT."""
    if snapshot is None:
        return selected
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise ValueError("The queued display subtitle snapshot is malformed; submit the export again.")
    display_id = str(snapshot.get("display_artifact_id") or "")
    matched = [item for item in selected if item.id == display_id]
    if not matched:
        return selected
    original, source_path = context._resolve_input(display_id)
    if original.session_id != session_id or source_path.suffix.lower() != ".srt":
        raise ValueError("The reviewed display subtitle source is unavailable in this session.")
    expected_hash = str(snapshot.get("display_content_hash") or "")
    if expected_hash and original.content_hash != expected_hash:
        raise ValueError("The reviewed display subtitle source changed; submit the export again.")
    namespace = str(snapshot.get("source_reference_namespace") or "subtitle_ordinal")
    if namespace == "logical_passage_ordinal":
        source_srt = snapshot.get("logical_passage_srt")
        if not isinstance(source_srt, str) or not source_srt.strip():
            raise ValueError("The queued logical subtitle timing is missing; submit the export again.")
    elif namespace == "subtitle_ordinal":
        source_srt = source_path.read_text(encoding="utf-8-sig")
    else:
        raise ValueError("The queued subtitle reference namespace is unsupported; submit the export again.")
    projected = project_display_srt(source_srt, snapshot)
    directory = context._session_dir(session_id) / "intermediates" / "subtitles"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f".generation-display-{new_id()}.srt"
    destination.write_text(projected, encoding="utf-8")
    metadata = {
        "source_role": snapshot.get("display_source_role") or original.role,
        "language": snapshot.get("display_language"),
        "original_parent_id": original.id,
        "generation_plan_revision_id": snapshot.get("revision_id"),
        "source_reference_namespace": namespace,
    }
    try:
        derived = context.artifacts.register(
            destination, kind="srt", role="generation_display_subtitles",
            session_id=session_id, parent_ids=[original.id], metadata=metadata,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return [derived if item.id == display_id else item for item in selected]
