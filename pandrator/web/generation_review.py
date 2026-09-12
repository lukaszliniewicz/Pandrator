"""Bounded inspection and atomic edits of immutable generation-plan revisions."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select

from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    SessionRecord,
)

COMPACT_SEGMENT_FIELDS = (
    "id", "ordinal", "revision", "text", "source_segment_ids", "status", "removed",
    "optimization_status", "take_count", "active_take_id", "has_usable_take",
    "audio_reuse_reason", "has_reusable_take",
)
EXTRA_SEGMENT_FIELDS = {"take_count", "active_take_id", "has_usable_take"}
PROJECTABLE_SEGMENT_FIELDS = set(COMPACT_SEGMENT_FIELDS) | {
    "node_kind", "paragraph_break_after", "speaker", "speech_block_provenance",
    "alignment_group", "optimized_text", "speech_plan", "optimization_reviewed",
    "optimization_model", "voice_id", "voice", "language", "silence_after_ms", "marked", "takes",
}


def project_segments(payload: dict[str, Any], *, view: str = "full", fields: list[str] | None = None) -> dict[str, Any]:
    if view not in {"full", "compact", "provenance"}:
        raise ValueError("Segment view must be full, compact, or provenance.")
    if fields is not None and (not fields or set(fields) - PROJECTABLE_SEGMENT_FIELDS):
        raise ValueError("Segment projection contains an unsupported field.")
    selected = fields or (list(COMPACT_SEGMENT_FIELDS) if view == "compact" else ["id", "ordinal", "revision", "source_segment_ids", "speech_block_provenance"] if view == "provenance" else None)
    if selected is None:
        return payload
    selected = list(dict.fromkeys(["id", "ordinal", "revision", *selected]))
    rows = []
    for original in payload["items"]:
        item = dict(original)
        takes = item.get("takes") or []
        active = next((take for take in takes if take.get("is_active") and take.get("status") == "completed" and take.get("artifact_id")), None)
        item.update(take_count=len(takes), active_take_id=active.get("id") if active else None, has_usable_take=bool(active and item.get("status") == "completed"))
        rows.append({key: item.get(key) for key in selected})
    return {**payload, "items": rows, "view": view, "fields": selected}


def revision_history(database, session_id: str, *, limit: int = 50, before_revision_number: int | None = None) -> dict[str, Any]:
    from .generation_audio_identity import AudioIdentityContext, take_reuse_reason
    from .workspace import WorkspaceSettingsService

    snapshot, _ = WorkspaceSettingsService(database).resolve(session_id)
    limit = max(1, min(int(limit), 100))
    with database.session() as session:
        if session.get(SessionRecord, session_id) is None:
            raise KeyError(session_id)
        plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
        if plan is None:
            return {"items": [], "active_revision_id": None, "total": 0, "next_before_revision_number": None}
        query = select(GenerationPlanRevision).where(GenerationPlanRevision.plan_id == plan.id)
        if before_revision_number is not None:
            query = query.where(GenerationPlanRevision.revision_number < before_revision_number)
        revisions = list(session.scalars(query.order_by(GenerationPlanRevision.revision_number.desc()).limit(limit + 1)))
        has_more = len(revisions) > limit
        revisions = revisions[:limit]
        ids = [item.id for item in revisions]
        counts: dict[str, dict[str, int]] = {}
        if ids:
            for revision_id, removed, count in session.execute(select(
                GenerationSegment.plan_revision_id, GenerationSegment.removed, func.count(GenerationSegment.id),
            ).where(GenerationSegment.plan_revision_id.in_(ids)).group_by(GenerationSegment.plan_revision_id, GenerationSegment.removed)):
                values = counts.setdefault(revision_id, {"total": 0, "active": 0, "reusable": 0})
                values["total"] += count
                if not removed:
                    values["active"] += count
            audio_identity = AudioIdentityContext(session, snapshot)
            for segment, take, artifact in session.execute(select(
                GenerationSegment, AudioTake, Artifact,
            ).join(AudioTake, AudioTake.generation_segment_id == GenerationSegment.id).join(Artifact, Artifact.id == AudioTake.artifact_id).where(
                GenerationSegment.plan_revision_id.in_(ids),
                GenerationSegment.removed.is_(False), GenerationSegment.status == "completed",
                AudioTake.is_active.is_(True), AudioTake.status == "completed", Artifact.state != "deleted",
            )):
                values = counts[segment.plan_revision_id]
                reason = take_reuse_reason(segment, take, artifact, audio_identity.for_segment(segment))
                key = "reusable" if reason == "reusable" else "unknown" if reason == "audio_identity_unknown" else "settings_stale"
                values[key] = values.get(key, 0) + 1
        items = []
        for revision in revisions:
            operation = dict(revision.operation_json or {})
            action = str(operation.get("action") or "automatic")
            batch = operation.get("batch") or {}
            summary = {"automatic": "Automatic speech plan", "split": "Split speech block", "merge": "Merge adjacent speech blocks", "restore": "Restore earlier speech plan"}.get(action, action.replace("_", " ").capitalize())
            if operation.get("reason") == "edit_copy":
                summary = "Editable copy of reviewed or historical speech plan"
            if batch:
                summary += f" (batch {batch.get('index')}/{batch.get('count')})"
            values = counts.get(revision.id, {"total": 0, "active": 0, "reusable": 0})
            items.append({
                "id": revision.id, "revision_number": revision.revision_number,
                "parent_revision_id": revision.parent_revision_id,
                "source_revision_id": revision.source_revision_id,
                "source_artifact_id": (revision.settings_json or {}).get("_source_artifact_id"),
                "is_active": revision.id == plan.active_revision_id,
                "origin": "automatic" if action == "automatic" else "manual",
                "action": action, "summary": summary,
                "restored_from_revision_id": operation.get("target_revision_id"),
                "segment_count": values["total"], "active_segment_count": values["active"],
                "reusable_segment_count": values["reusable"],
                "stale_segment_count": values["active"] - values["reusable"],
                "audio_settings_stale_segment_count": values.get("settings_stale", 0),
                "audio_identity_unknown_segment_count": values.get("unknown", 0),
                "created_at": revision.created_at.isoformat(),
                "speech_block_settings": {key: value for key, value in (revision.settings_json or {}).items() if key.startswith("speech_block_")},
            })
        return {
            "items": items, "active_revision_id": plan.active_revision_id,
            "total": int(session.scalar(select(func.count()).select_from(GenerationPlanRevision).where(GenerationPlanRevision.plan_id == plan.id)) or 0),
            "next_before_revision_number": revisions[-1].revision_number if revisions and has_more else None,
        }


def source_references(segment: GenerationSegment) -> set[str]:
    cues = (segment.speech_block_provenance_json or {}).get("source_cues") or []
    refs = {str(cue["reference"]) for cue in cues if isinstance(cue, dict) and cue.get("reference") is not None}
    return refs or {str(value) for value in segment.source_segment_ids_json or []}


def resolve_segment_selector(rows: list[GenerationSegment], selector: dict[str, Any], lineage: dict[str, list[str]], aliases: dict[str, list[str]]) -> GenerationSegment:
    values = {key: value for key, value in selector.items() if value is not None and value != []}
    if len(values) != 1:
        raise ValueError("A segment selector must name exactly one ID, cue set, ordinal, or batch-result reference.")
    if "segment_id" in values:
        requested = str(values["segment_id"])
        possible = lineage.get(requested, [requested])
        matches = [row for row in rows if row.id in possible]
    elif "source_cue_ids" in values:
        expected = {str(value) for value in values["source_cue_ids"]}
        matches = [row for row in rows if expected.issubset(source_references(row))]
    elif "ordinal" in values:
        matches = [row for row in rows if row.ordinal == values["ordinal"]]
    elif "result_ref" in values:
        possible = aliases.get(str(values["result_ref"]), [])
        matches = [row for row in rows if row.id in possible]
    else:
        raise ValueError("Unsupported segment selector.")
    if len(matches) != 1:
        raise ValueError(f"Segment selector matched {len(matches)} active blocks; use a narrower cue set or an explicit batch-result reference.")
    return matches[0]


def resolve_split_boundary(segment: GenerationSegment, boundary: dict[str, Any], *, text_layer: str) -> int:
    values = {key: value for key, value in boundary.items() if value is not None}
    if len(values) != 1:
        raise ValueError("A split boundary must specify exactly one cursor, text anchor, cue boundary, or sentence boundary.")
    text = segment.text if text_layer == "display" else segment.optimized_text or segment.text
    key, value = next(iter(values.items()))
    if key == "cursor":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("A split cursor must be an integer Unicode code-point offset.")
        cursor = value
    elif key in {"before_text", "after_text"}:
        anchor = str(value)
        matches = list(re.finditer(f"(?={re.escape(anchor)})", text)) if anchor else []
        if len(matches) != 1:
            raise ValueError(f"The split text anchor matched {len(matches)} places; an unambiguous anchor is required.")
        cursor = matches[0].start() + (len(anchor) if key == "after_text" else 0)
    elif key in {"before_source_cue_id", "after_source_cue_id"}:
        cues = (segment.speech_block_provenance_json or {}).get("source_cues") or []
        matches = [cue for cue in cues if isinstance(cue, dict) and str(cue.get("reference")) == str(value)]
        spans = [span for cue in matches for span in cue.get(f"{text_layer}_spans") or [] if isinstance(span, (list, tuple)) and len(span) == 2]
        if not spans:
            raise ValueError("This source cue has no exact character-span evidence in the selected text layer. Use a unique text anchor instead.")
        cursor = min(int(span[0]) for span in spans) if key == "before_source_cue_id" else max(int(span[1]) for span in spans)
    elif key == "after_sentence":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("The sentence number must be a positive integer.")
        endings = list(re.finditer(r'[.!?。！？؟։…]+["”’»」』）)]*(?=\s|$)', text))
        if value > len(endings):
            raise ValueError("The selected sentence boundary does not exist.")
        cursor = endings[value - 1].end()
    else:
        raise ValueError("Unsupported split-boundary selector.")
    if cursor <= 0 or cursor >= len(text) or not text[:cursor].strip() or not text[cursor:].strip():
        raise ValueError("The selected boundary would create an empty speech block.")
    return cursor


def revise_topology_batch_in_session(service, session, session_id: str, expected_revision_id: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Run an ordered batch in ONE caller transaction; any failure rolls it all back.

    Each operation remains an immutable, restorable revision. Original IDs are
    followed through lineage; labels such as 'opening.left' resolve prior batch
    results without another round trip. Ambiguous descendants are never guessed.
    """
    from .workspace import RevisionConflict

    if not operations or len(operations) > 50:
        raise ValueError("Topology batches must contain between 1 and 50 operations.")
    plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
    if plan is None or plan.active_revision_id != expected_revision_id:
        raise RevisionConflict("The speech plan changed before the batch could be applied.")
    initial = list(session.scalars(select(GenerationSegment).where(GenerationSegment.plan_revision_id == expected_revision_id).order_by(GenerationSegment.ordinal)))
    lineage = {row.id: [row.id] for row in initial}
    aliases: dict[str, list[str]] = {}
    used_labels: set[str] = set()
    affected: set[str] = set()
    revision_ids: list[str] = []
    current_id = expected_revision_id
    result: dict[str, Any] = {}
    for index, operation in enumerate(operations, start=1):
        rows = list(session.scalars(select(GenerationSegment).where(GenerationSegment.plan_revision_id == current_id, GenerationSegment.removed.is_(False)).order_by(GenerationSegment.ordinal)))
        action = operation.get("action")
        label = str(operation.get("label") or "").strip()
        if label:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", label) or label in used_labels:
                raise ValueError("Batch labels must be unique identifiers of at most 40 characters.")
            used_labels.add(label)
        concrete: dict[str, Any] = {"action": action, "batch": {"base_revision_id": expected_revision_id, "index": index, "count": len(operations), "label": label or None}}
        if action == "split":
            selector = operation.get("segment") or {"segment_id": operation.get("segment_id")}
            segment = resolve_segment_selector(rows, selector, lineage, aliases)
            text_layer = str(operation.get("text_layer") or "display")
            if text_layer not in {"display", "speech"}:
                raise ValueError("Text layer must be display or speech.")
            boundary = operation.get("boundary") or {"cursor": operation.get("cursor")}
            cursor = resolve_split_boundary(segment, boundary, text_layer=text_layer)
            concrete.update(segment_id=segment.id, cursor=cursor, text_layer=text_layer)
        elif action == "merge":
            left = resolve_segment_selector(rows, operation.get("left") or {"segment_id": operation.get("left_segment_id")}, lineage, aliases)
            right = resolve_segment_selector(rows, operation.get("right") or {"segment_id": operation.get("right_segment_id")}, lineage, aliases)
            concrete.update(left_segment_id=left.id, right_segment_id=right.id)
        else:
            raise ValueError("A topology batch supports split and merge operations only.")
        result = service.revise_topology_in_session(session, session_id, current_id, concrete)
        step_lineage = result["lineage"]
        lineage = {original: list(dict.fromkeys(child for parent in current for child in step_lineage.get(parent, []))) for original, current in lineage.items()}
        aliases = {name: list(dict.fromkeys(child for parent in current for child in step_lineage.get(parent, []))) for name, current in aliases.items()}
        affected = {child for parent in affected for child in step_lineage.get(parent, [])} | set(result["affected_segment_ids"])
        if label:
            if action == "split":
                children = step_lineage[segment.id]
                aliases[f"{label}.left"] = children[:1]
                aliases[f"{label}.right"] = children[1:]
            else:
                aliases[label] = step_lineage[left.id]
        current_id = result["plan_revision_id"]
        revision_ids.append(current_id)
    return {
        **result,
        "base_revision_id": expected_revision_id,
        "operation_count": len(operations),
        "revision_ids": revision_ids,
        "lineage": lineage,
        "result_refs": aliases,
        "affected_segment_ids": [value for value in result["segment_ids"] if value in affected],
    }
