"""Bounded passage inspection and sparse edits through canonical review."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..schemas.subtitle_passages import (
    PatchSubtitlePassagesInput,
    PreviewSubtitlePassagesInput,
)

_EDIT_FIELDS = (
    "id", "text", "speaker", "start_ms", "end_ms", "review_state", "review_note",
)


def _column(application: Any, session_id: str, artifact_id: str) -> dict[str, Any]:
    data = application.review_subtitles(session_id, artifact_ids=[artifact_id])
    column = next(
        (item for item in data.get("columns", []) if item.get("artifact_id") == artifact_id),
        None,
    )
    if column is None:
        raise PandratorMcpError("not_found", "Selected subtitle artifact is unavailable.")
    return column


def preview_subtitle_passages(
    runtime: McpRuntime, arguments: PreviewSubtitlePassagesInput,
) -> dict[str, Any]:
    column = _column(runtime.require_application(), arguments.session_id, arguments.artifact_id)
    source = column.get("logical_passages") or []
    query = (arguments.query or "").strip().casefold()
    matched = [
        row for row in source
        if not query or any(query in str(row.get(key) or "").casefold()
                            for key in ("id", "text", "speaker"))
    ]
    rows = matched[arguments.offset:arguments.offset + arguments.limit]
    return {
        "schema_version": "1",
        "session_id": arguments.session_id,
        "artifact_id": arguments.artifact_id,
        "source_content_hash": column.get("source_content_hash"),
        "stage": column.get("stage"),
        "revision": column.get("revision"),
        "revision_id": column.get("revision_id"),
        "composition_hash": column.get("composition_hash"),
        "language": column.get("language"),
        "total_passages": len(source),
        "matched_passages": len(matched),
        "offset": arguments.offset,
        "next_offset": arguments.offset + len(rows)
        if arguments.offset + len(rows) < len(matched) else None,
        "passages": [
            {key: row.get(key) for key in (*_EDIT_FIELDS, "turn_id") if key in row}
            for row in rows
        ],
    }


def patch_subtitle_passages(
    runtime: McpRuntime, arguments: PatchSubtitlePassagesInput,
) -> ToolOutcome:
    application = runtime.require_application()
    column = _column(application, arguments.session_id, arguments.source_artifact_id)
    for key, expected in (
        ("source_content_hash", arguments.expected_source_hash),
        ("revision", arguments.expected_revision),
        ("composition_hash", arguments.expected_composition_hash),
    ):
        if column.get(key) != expected:
            raise PandratorMcpError("revision_conflict", f"Subtitle {key} changed; refresh the preview.")
    source = column.get("logical_passages") or []
    patches = {row.id: row for row in arguments.passages}
    unknown = set(patches) - {str(row["id"]) for row in source}
    if unknown:
        raise PandratorMcpError("validation_error", "Unknown source passage IDs: " + ", ".join(sorted(unknown)))
    values = []
    changes = []
    for row in source:
        value = {key: row[key] for key in _EDIT_FIELDS if key in row}
        # The backend owns all ancestry, evidence and word anchors. Absence of
        # starts_new_turn preserves inherited turns; it does not remove one.
        patch = patches.get(str(row["id"]))
        if patch is not None:
            delta = patch.model_dump(exclude_none=True, exclude_unset=True)
            delta.pop("id")
            changes.append({"id": row["id"], "before": dict(value), "patch": delta})
            value.update(delta)
        values.append(value)
    result = application.save_subtitle_passage_review(
        arguments.session_id, str(column["stage"]),
        source_artifact_id=arguments.source_artifact_id,
        expected_source_hash=arguments.expected_source_hash,
        expected_revision=arguments.expected_revision,
        expected_composition_hash=arguments.expected_composition_hash,
        passages=values, dry_run=arguments.dry_run,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(
        result={**result, "schema_version": "1", "dry_run": arguments.dry_run,
                "patched_count": len(changes), "changes": changes},
        next_actions=[NextAction(
            tool="pandrator_preview_subtitle_passages",
            arguments={"session_id": arguments.session_id,
                       "artifact_id": result.get("artifact_id") or arguments.source_artifact_id},
            reason="Inspect the reviewed passage text and turn metadata.",
        )],
    )
