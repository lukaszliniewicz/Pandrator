"""Model-safe session reads and revision-safe writes."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..schemas import (
    AttachExistingSourceInput,
    CreateSessionInput,
    GetSessionInput,
    GetSessionSettingsInput,
    GetWorkflowInput,
    ImportSubtitlesInput,
    ListSessionsInput,
    ListSourcesInput,
    PatchSubtitleCuesInput,
    PreviewSubtitlesInput,
    ReplaceSubtitleTextInput,
    UpdateSessionInput,
    UpdateSessionSettingsInput,
)

_PRIVATE_SETTING_KEYS = frozenset(
    {
        "api_base",
        "api_key",
        "authorization",
        "base_url",
        "ca_bundle",
        "command",
        "connection",
        "credential",
        "credential_reference",
        "endpoint",
        "external_path",
        "host",
        "origin",
        "password",
        "path",
        "port",
        "private_key",
        "proxy",
        "proxy_origin",
        "secret",
        "token",
        "url",
        "workspace",
    }
)


def _session_projection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "workflow_kind": item.get("workflow_kind"),
        "source_language": item.get("source_language"),
        "target_language": item.get("target_language"),
        "workflow_preset": item.get("workflow_preset"),
        "included_stages": list(
            item.get("included_stages") or item.get("included_stages_json") or ()
        )[:20],
        "status": item.get("status"),
        "revision": item.get("revision"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _safe_setting_value(
    value: Any,
    *,
    depth: int = 0,
) -> Any:
    if depth > 10:
        return None
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:500]:
            key = str(raw_key)[:120]
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _PRIVATE_SETTING_KEYS or normalized.endswith(
                (
                    "_api_key",
                    "_command",
                    "_credential",
                    "_endpoint",
                    "_origin",
                    "_password",
                    "_path",
                    "_private_key",
                    "_proxy",
                    "_secret",
                    "_token",
                    "_url",
                )
            ):
                continue
            result[key] = _safe_setting_value(
                item,
                depth=depth + 1,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_setting_value(item, depth=depth + 1) for item in list(value)[:500]]
    if isinstance(value, str):
        return value[:20_000]
    return value


def _artifact_projection(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    return {
        key: item.get(key)
        for key in (
            "id",
            "version",
            "kind",
            "role",
            "mime_type",
            "size_bytes",
            "state",
            "settings_hash",
            "parent_ids",
            "created_at",
            "is_selected",
        )
        if key in item
    }


def _workflow_projection(payload: dict[str, Any]) -> dict[str, Any]:
    stages: list[dict[str, Any]] = []
    source_stages = payload.get("stages") if isinstance(payload.get("stages"), list) else []
    for source in source_stages[:50]:
        if not isinstance(source, dict):
            continue
        stage = {
            key: source.get(key)
            for key in (
                "number",
                "key",
                "title",
                "explanation",
                "status",
                "stale_reason",
                "executable",
                "toggle",
                "toggle_only",
                "enabled",
                "optimization_timing",
                "included",
                "required",
                "selected_artifact_id",
                "selection_revision",
                "artifact_history_total",
                "artifact_history_has_more",
                "artifact_history_next_before_version",
                "job_id",
                "progress",
                "usage",
            )
            if key in source
        }
        stage["artifact"] = _artifact_projection(source.get("artifact"))
        history = source.get("artifacts") if isinstance(source.get("artifacts"), list) else []
        stage["artifacts"] = [
            projected
            for item in history[:20]
            if (projected := _artifact_projection(item)) is not None
        ]
        stages.append(stage)
    sources: list[dict[str, Any]] = []
    source_items = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    for item in source_items[:50]:
        if not isinstance(item, dict):
            continue
        sources.append(
            {
                "id": item.get("id"),
                "filename": str(item.get("filename") or "")[:255],
                "kind": item.get("kind"),
                "role": item.get("role"),
            }
        )
    return {
        "schema_version": "1",
        "session_id": payload.get("session_id"),
        "workflow_kind": payload.get("workflow_kind"),
        "workflow_preset": payload.get("workflow_preset"),
        "revision": payload.get("revision"),
        "stages": stages,
        "sources": sources,
    }


def list_sessions(
    runtime: McpRuntime,
    arguments: ListSessionsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_sessions(
        limit=arguments.limit,
        query=arguments.query,
    )
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    query_term = arguments.query.strip().casefold() if arguments.query else None
    filtered = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if arguments.workflow_kind and item.get("workflow_kind") != arguments.workflow_kind:
            continue
        if arguments.state and item.get("status") != arguments.state:
            continue
        if query_term:
            match_fields = (
                str(item.get("name") or ""),
                str(item.get("id") or ""),
                str(item.get("source_language") or ""),
                str(item.get("target_language") or ""),
            )
            if not any(query_term in field.casefold() for field in match_fields):
                continue
        filtered.append(_session_projection(item))
    return {
        "schema_version": "1",
        "items": filtered[: arguments.limit],
    }


def get_session(
    runtime: McpRuntime,
    arguments: GetSessionInput,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        **_session_projection(runtime.require_application().get_session(arguments.session_id)),
    }


def get_workflow(
    runtime: McpRuntime,
    arguments: GetWorkflowInput,
) -> dict[str, Any]:
    return _workflow_projection(runtime.require_application().get_workflow(arguments.session_id))


def get_session_settings(
    runtime: McpRuntime,
    arguments: GetSessionSettingsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().get_session_settings(
        arguments.session_id,
        arguments.section,
    )
    return {
        "schema_version": "1",
        "session_id": arguments.session_id,
        "section": payload.get("section") or arguments.section,
        "override": (
            _safe_setting_value(payload.get("override"))
            if isinstance(payload.get("override"), dict)
            else {}
        ),
        "effective": (
            _safe_setting_value(payload.get("effective"))
            if isinstance(payload.get("effective"), dict)
            else {}
        ),
        "session_context": (
            _safe_setting_value(payload.get("session_context"))
            if isinstance(payload.get("session_context"), dict)
            else {}
        ),
        "revision": payload.get("revision"),
        "global_revision": payload.get("global_revision"),
    }


def list_sources(
    runtime: McpRuntime,
    arguments: ListSourcesInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_sources(
        include_trashed=arguments.state == "trashed"
    )
    source = payload.get("items") if isinstance(payload.get("items"), list) else []
    items: list[dict[str, Any]] = []
    query = str(arguments.query or "").casefold().strip()
    requested_kind = str(arguments.kind or "").casefold().strip()
    requested_mime = str(arguments.mime_type or "").casefold().strip()
    for item in source:
        if not isinstance(item, dict):
            continue
        if item.get("state") != arguments.state:
            continue
        if query and query not in str(item.get("display_name") or "").casefold():
            continue
        if requested_kind and str(item.get("kind") or "").casefold() != requested_kind:
            continue
        if requested_mime and str(item.get("mime_type") or "").casefold() != requested_mime:
            continue
        items.append(
            {
                "source_asset_id": item.get("id"),
                "artifact_id": item.get("artifact_id"),
                "display_name": item.get("display_name"),
                "kind": item.get("kind"),
                "mime_type": item.get("mime_type"),
                "size_bytes": item.get("size_bytes"),
                "content_hash": item.get("content_hash"),
                "state": item.get("state"),
                "revision": item.get("revision"),
                "reference_count": item.get("reference_count"),
                "current_reference_count": item.get("current_reference_count"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
        )
        if len(items) >= arguments.limit:
            break
    return {"schema_version": "1", "items": items}


def create_session(
    runtime: McpRuntime,
    arguments: CreateSessionInput,
) -> ToolOutcome:
    result = runtime.require_application().create_session(
        name=arguments.name,
        workflow_kind=arguments.workflow_kind,
        source_language=arguments.source_language,
        target_language=arguments.target_language,
        workflow_preset=arguments.workflow_preset,
        included_stages=arguments.included_stages,
        idempotency_key=arguments.idempotency_key,
    )
    session = {
        "schema_version": "1",
        **_session_projection(result),
    }
    return ToolOutcome(
        result=session,
        next_actions=[
            NextAction(
                tool="pandrator_list_sources",
                arguments={"state": "current", "limit": 50},
                reason=("Inspect reusable sources before attaching one to the new session."),
            )
        ],
    )


def update_session(
    runtime: McpRuntime,
    arguments: UpdateSessionInput,
) -> ToolOutcome:
    result = runtime.require_application().update_session(
        arguments.session_id,
        expected_revision=arguments.expected_revision,
        changes=arguments.changes(),
        idempotency_key=arguments.idempotency_key,
    )
    session = {
        "schema_version": "1",
        **_session_projection(result),
    }
    return ToolOutcome(
        result=session,
        next_actions=[
            NextAction(
                tool="pandrator_get_workflow",
                arguments={"session_id": arguments.session_id},
                reason=("Inspect how the revised session changes workflow prerequisites."),
            )
        ],
    )


def attach_existing_source(
    runtime: McpRuntime,
    arguments: AttachExistingSourceInput,
) -> ToolOutcome:
    result = runtime.require_application().attach_existing_source(
        arguments.session_id,
        source_asset_id=arguments.source_asset_id,
        role=arguments.role,
        expected_session_revision=(arguments.expected_session_revision),
        idempotency_key=arguments.idempotency_key,
    )
    safe = {
        "schema_version": "1",
        "attachment_id": result.get("id"),
        "session_id": result.get("session_id"),
        "source_asset_id": result.get("source_asset_id"),
        "role": result.get("role"),
        "is_current": bool(result.get("is_current")),
        "attachment_revision": result.get("revision"),
        "session_revision": result.get("session_revision"),
    }
    return ToolOutcome(
        result=safe,
        next_actions=[
            NextAction(
                tool="pandrator_get_workflow",
                arguments={"session_id": arguments.session_id},
                reason=("Inspect the workflow and prerequisites after the source attachment."),
            )
        ],
    )


def update_session_settings(
    runtime: McpRuntime,
    arguments: UpdateSessionSettingsInput,
) -> ToolOutcome:
    result = runtime.require_application().update_session_settings(
        arguments.session_id,
        section=arguments.section,
        value=arguments.value,
        expected_revision=arguments.expected_revision,
        idempotency_key=arguments.idempotency_key,
    )
    safe = {
        "schema_version": "1",
        "session_id": arguments.session_id,
        "section": result.get("section") or arguments.section,
        "override": (
            _safe_setting_value(result.get("override"))
            if isinstance(result.get("override"), dict)
            else {}
        ),
        "revision": result.get("revision"),
    }
    return ToolOutcome(
        result=safe,
        next_actions=[
            NextAction(
                tool="pandrator_get_session_settings",
                arguments={
                    "session_id": arguments.session_id,
                    "section": arguments.section,
                },
                reason=(
                    "Review the effective settings and new revision before planning execution."
                ),
            )
        ],
    )


STAGE_ALIASES: dict[str, str] = {
    "transcribe": "transcription",
    "transcription": "transcription",
    "correct": "correction",
    "correction": "correction",
    "translate": "translation",
    "translation": "translation",
    "tts_optimized": "tts_optimization",
    "tts_optimization": "tts_optimization",
}
STAGE_ORDER: tuple[str, ...] = (
    "tts_optimization",
    "translation",
    "correction",
    "transcription",
)


def preview_subtitles(
    runtime: McpRuntime,
    arguments: PreviewSubtitlesInput,
) -> dict[str, Any]:
    application = runtime.require_application()
    session_id = arguments.session_id
    cues: list[dict[str, Any]] = []
    selected_stage: str | None = arguments.stage
    language = None
    revision = None
    artifact_id = arguments.artifact_id

    if arguments.artifact_id:
        review_data = application.review_subtitles(
            session_id,
            artifact_ids=[arguments.artifact_id],
        )
        columns = review_data.get("columns") or []
        if columns and isinstance(columns[0], dict):
            col = columns[0]
            selected_stage = col.get("stage") or selected_stage
            language = col.get("language")
            revision = col.get("revision")
            cues = list(col.get("segments") or [])
    else:
        doc_data = application.get_subtitles(session_id)
        stages = doc_data.get("stages") or {}
        normalized_stage = STAGE_ALIASES.get(selected_stage) if selected_stage else None
        if normalized_stage and normalized_stage in stages:
            target_info = stages[normalized_stage]
        elif selected_stage and selected_stage in stages:
            target_info = stages[selected_stage]
        else:
            order = (
                "tts_optimization",
                "tts_optimized",
                "translation",
                "translate",
                "correction",
                "correct",
                "transcription",
                "transcribe",
            )
            target_stage = next((s for s in order if s in stages), None)
            target_info = stages.get(target_stage) if target_stage else None
            selected_stage = target_stage

        if target_info and isinstance(target_info, dict):
            language = target_info.get("language")
            revision = target_info.get("revision")
            cues = list(target_info.get("segments") or [])

    total_cues = len(cues)
    matched_cues = cues
    if arguments.query and arguments.query.strip():
        term = arguments.query.strip().casefold()
        matched_cues = [
            cue
            for cue in cues
            if term in str(cue.get("text") or "").casefold()
            or term in str(cue.get("speaker") or "").casefold()
        ]

    offset = arguments.offset
    limit = arguments.limit

    if arguments.around_ordinal is not None:
        target_ord = arguments.around_ordinal
        target_idx = next(
            (i for i, c in enumerate(matched_cues) if int(c.get("ordinal", i + 1)) == target_ord),
            max(0, min(len(matched_cues) - 1, target_ord - 1)) if matched_cues else 0,
        )
        ctx = arguments.context
        start_idx = max(0, target_idx - ctx)
        end_idx = min(len(matched_cues), target_idx + ctx + 1)
        sliced = matched_cues[start_idx:end_idx]
        offset = start_idx
        limit = len(sliced)
    elif arguments.start_ordinal is not None or arguments.end_ordinal is not None:
        start_ord = arguments.start_ordinal or 1
        end_ord = arguments.end_ordinal or (total_cues + 1)
        sliced = [
            c
            for i, c in enumerate(matched_cues)
            if start_ord <= int(c.get("ordinal", i + 1)) <= end_ord
        ]
        offset = max(0, start_ord - 1)
        limit = len(sliced)
    else:
        sliced = matched_cues[offset : offset + limit]

    projected_cues = [
        {
            "ordinal": int(cue.get("ordinal", idx + 1)),
            "start_ms": int(cue.get("start_ms", 0)),
            "end_ms": int(cue.get("end_ms", 0)),
            "speaker": cue.get("speaker"),
            "text": str(cue.get("text") or ""),
        }
        for idx, cue in enumerate(sliced)
        if isinstance(cue, dict)
    ]

    return {
        "schema_version": "1",
        "session_id": session_id,
        "stage": selected_stage,
        "language": language,
        "artifact_id": artifact_id,
        "revision": revision,
        "total_cues": total_cues,
        "matched_cues": len(matched_cues),
        "offset": offset,
        "limit": limit,
        "cues": projected_cues,
    }


def replace_subtitle_text(
    runtime: McpRuntime,
    arguments: ReplaceSubtitleTextInput,
) -> ToolOutcome:
    application = runtime.require_application()
    session_id = arguments.session_id
    stage_key = STAGE_ALIASES.get(arguments.stage, arguments.stage)
    doc_data = application.get_subtitles(session_id)
    stages = doc_data.get("stages") or {}
    if stage_key not in stages and arguments.stage in stages:
        stage_key = arguments.stage
    if stage_key not in stages:
        raise PandratorMcpError(
            "not_found",
            f"Stage '{arguments.stage}' subtitles not found in session '{session_id}'.",
        )
    target_info = stages[stage_key]
    current_revision = int(target_info.get("revision") or 1)
    if current_revision != arguments.expected_revision:
        raise PandratorMcpError(
            "revision_conflict",
            f"Subtitle revision conflict: expected {arguments.expected_revision} but active is {current_revision}.",
        )
    raw_segments = list(target_info.get("segments") or [])
    if not raw_segments:
        raise PandratorMcpError(
            "validation_error",
            f"Stage '{arguments.stage}' contains no subtitle cues to modify.",
        )

    search_text = arguments.search_text
    replacement = arguments.replacement_text

    if arguments.is_regex:
        flags = 0 if arguments.match_case else re.IGNORECASE
        try:
            pattern = re.compile(search_text, flags)
        except re.error as err:
            raise PandratorMcpError(
                "validation_error",
                f"Invalid regular expression: {err}",
            ) from err
    elif arguments.whole_word:
        flags = 0 if arguments.match_case else re.IGNORECASE
        pattern = re.compile(r"\b" + re.escape(search_text) + r"\b", flags)
    elif not arguments.match_case:
        pattern = re.compile(re.escape(search_text), re.IGNORECASE)
    else:
        pattern = None

    changes: list[dict[str, Any]] = []
    modified_segments: list[dict[str, Any]] = []

    for idx, seg in enumerate(raw_segments):
        orig_text = str(seg.get("text") or "")
        start_ms = int(seg.get("start_ms") or 0)
        end_ms = int(seg.get("end_ms") or 0)
        speaker = seg.get("speaker")
        ordinal = int(seg.get("ordinal", idx + 1))

        if pattern:
            new_text, count = pattern.subn(replacement, orig_text)
        else:
            count = orig_text.count(search_text)
            new_text = orig_text.replace(search_text, replacement)

        if count > 0 and new_text != orig_text:
            changes.append(
                {
                    "ordinal": ordinal,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "speaker": speaker,
                    "before": orig_text,
                    "after": new_text,
                }
            )

        modified_segments.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": new_text,
                "speaker": speaker,
            }
        )

    if not changes:
        return ToolOutcome(
            result={
                "schema_version": "1",
                "session_id": session_id,
                "stage": stage_key,
                "revision": current_revision,
                "dry_run": arguments.dry_run,
                "modified_count": 0,
                "changes": [],
                "message": f"No occurrences of '{search_text}' found in stage '{stage_key}'.",
            }
        )

    if arguments.dry_run:
        return ToolOutcome(
            result={
                "schema_version": "1",
                "session_id": session_id,
                "stage": stage_key,
                "revision": current_revision,
                "dry_run": True,
                "modified_count": len(changes),
                "changes": changes,
                "message": f"Dry-run found {len(changes)} cue(s) matching '{search_text}'.",
            },
            next_actions=[
                NextAction(
                    tool="pandrator_replace_subtitle_text",
                    arguments={
                        "session_id": session_id,
                        "stage": stage_key,
                        "expected_revision": current_revision,
                        "search_text": search_text,
                        "replacement_text": replacement,
                        "match_case": arguments.match_case,
                        "whole_word": arguments.whole_word,
                        "is_regex": arguments.is_regex,
                        "dry_run": False,
                        "idempotency_key": arguments.idempotency_key,
                    },
                    reason="Commit the reviewed subtitle text replacement.",
                )
            ],
        )

    saved = application.save_subtitle_review(
        session_id,
        stage_key,
        expected_revision=arguments.expected_revision,
        segments=modified_segments,
        idempotency_key=arguments.idempotency_key,
    )

    next_actions = [
        NextAction(
            tool="pandrator_preview_subtitles",
            arguments={
                "session_id": session_id,
                "stage": stage_key,
                "around_ordinal": changes[0]["ordinal"],
            },
            reason="Verify the updated subtitles in context.",
        )
    ]
    if stage_key in ("translation", "tts_optimization"):
        next_actions.append(
            NextAction(
                tool="pandrator_list_generation_segments",
                arguments={"session_id": session_id},
                reason="Inspect or regenerate generation segments for modified cues.",
            )
        )

    return ToolOutcome(
        result={
            "schema_version": "1",
            "session_id": session_id,
            "stage": stage_key,
            "revision": saved.get("revision"),
            "artifact_id": saved.get("artifact_id"),
            "document_id": saved.get("document_id"),
            "dry_run": False,
            "modified_count": len(changes),
            "changed_ordinals": [c["ordinal"] for c in changes],
            "changes": changes,
        },
        next_actions=next_actions,
    )


def patch_subtitle_cues(
    runtime: McpRuntime,
    arguments: PatchSubtitleCuesInput,
) -> ToolOutcome:
    application = runtime.require_application()
    session_id = arguments.session_id
    stage_key = STAGE_ALIASES.get(arguments.stage, arguments.stage)
    doc_data = application.get_subtitles(session_id)
    stages = doc_data.get("stages") or {}
    if stage_key not in stages and arguments.stage in stages:
        stage_key = arguments.stage
    if stage_key not in stages:
        raise PandratorMcpError(
            "not_found",
            f"Stage '{arguments.stage}' subtitles not found in session '{session_id}'.",
        )
    target_info = stages[stage_key]
    current_revision = int(target_info.get("revision") or 1)
    if current_revision != arguments.expected_revision:
        raise PandratorMcpError(
            "revision_conflict",
            f"Subtitle revision conflict: expected {arguments.expected_revision} but active is {current_revision}.",
        )
    raw_segments = list(target_info.get("segments") or [])
    if not raw_segments:
        raise PandratorMcpError(
            "validation_error",
            f"Stage '{arguments.stage}' contains no subtitle cues to patch.",
        )

    patches_by_ordinal = {patch.ordinal: patch for patch in arguments.cues}
    max_ordinal = len(raw_segments)
    for ord_num in patches_by_ordinal:
        if ord_num < 1 or ord_num > max_ordinal:
            raise PandratorMcpError(
                "validation_error",
                f"Cue ordinal {ord_num} is out of bounds (document has {max_ordinal} cues).",
            )

    applied_changes: list[dict[str, Any]] = []
    modified_segments: list[dict[str, Any]] = []

    for idx, seg in enumerate(raw_segments, start=1):
        orig_text = str(seg.get("text") or "")
        orig_speaker = seg.get("speaker")
        orig_start = int(seg.get("start_ms") or 0)
        orig_end = int(seg.get("end_ms") or 0)

        patch = patches_by_ordinal.get(idx)
        if patch:
            new_text = patch.text if patch.text is not None else orig_text
            new_speaker = patch.speaker if patch.speaker is not None else orig_speaker
            new_start = patch.start_ms if patch.start_ms is not None else orig_start
            new_end = patch.end_ms if patch.end_ms is not None else orig_end

            applied_changes.append(
                {
                    "ordinal": idx,
                    "before": {
                        "text": orig_text,
                        "speaker": orig_speaker,
                        "start_ms": orig_start,
                        "end_ms": orig_end,
                    },
                    "after": {
                        "text": new_text,
                        "speaker": new_speaker,
                        "start_ms": new_start,
                        "end_ms": new_end,
                    },
                }
            )

            modified_segments.append(
                {
                    "start_ms": new_start,
                    "end_ms": new_end,
                    "text": new_text,
                    "speaker": new_speaker,
                }
            )
        else:
            modified_segments.append(
                {
                    "start_ms": orig_start,
                    "end_ms": orig_end,
                    "text": orig_text,
                    "speaker": orig_speaker,
                }
            )

    saved = application.save_subtitle_review(
        session_id,
        stage_key,
        expected_revision=arguments.expected_revision,
        segments=modified_segments,
        idempotency_key=arguments.idempotency_key,
    )

    next_actions = [
        NextAction(
            tool="pandrator_preview_subtitles",
            arguments={
                "session_id": session_id,
                "stage": stage_key,
                "around_ordinal": applied_changes[0]["ordinal"] if applied_changes else 1,
            },
            reason="Verify patched cues in context.",
        )
    ]
    if stage_key in ("translation", "tts_optimization"):
        next_actions.append(
            NextAction(
                tool="pandrator_list_generation_segments",
                arguments={"session_id": session_id},
                reason="Inspect or regenerate generation segments for modified cues.",
            )
        )

    return ToolOutcome(
        result={
            "schema_version": "1",
            "session_id": session_id,
            "stage": stage_key,
            "revision": saved.get("revision"),
            "artifact_id": saved.get("artifact_id"),
            "document_id": saved.get("document_id"),
            "patched_count": len(applied_changes),
            "patched_ordinals": [c["ordinal"] for c in applied_changes],
            "changes": applied_changes,
        },
        next_actions=next_actions,
    )


def _parse_srt_text(content: str) -> list[dict[str, Any]]:
    normalized = (
        str(content or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    )
    if not normalized:
        return []
    blocks = re.split(r"\n\s*\n+", normalized)
    segments = []
    time_re = re.compile(
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
    )
    speaker_re = re.compile(r"^\[(?P<speaker>[^\]]+)\]:\s*(?P<text>.*)$", re.DOTALL)

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        time_idx = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if time_idx < 0:
            continue
        match = time_re.search(lines[time_idx])
        if not match:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = [int(v) for v in match.groups()]
        start_ms = h1 * 3600_000 + m1 * 60_000 + s1 * 1000 + ms1
        end_ms = h2 * 3600_000 + m2 * 60_000 + s2 * 1000 + ms2
        if end_ms <= start_ms:
            end_ms = start_ms + 100

        text_lines = lines[time_idx + 1 :]
        raw_text = "\n".join(text_lines).strip()
        if not raw_text:
            continue

        speaker = None
        sp_match = speaker_re.match(raw_text)
        if sp_match:
            speaker = sp_match.group("speaker").strip()
            raw_text = sp_match.group("text").strip()

        segments.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": raw_text,
                "speaker": speaker,
            }
        )
    return segments


def import_subtitles(
    runtime: McpRuntime,
    arguments: ImportSubtitlesInput,
) -> ToolOutcome:
    application = runtime.require_application()
    session_id = arguments.session_id
    stage_key = STAGE_ALIASES.get(arguments.stage, arguments.stage)

    content: str | None = arguments.srt_content
    if not content:
        if not arguments.filename:
            raise PandratorMcpError(
                "validation_error",
                "Either 'srt_content' or 'filename' must be provided.",
            )
        target = runtime.profile
        allowed_roots: list[Path] = []
        if target:
            if target.workspace:
                ws_path = Path(target.workspace).resolve()
                allowed_roots.append(ws_path)
                allowed_roots.append(ws_path / "exports")
            if target.local_output_root:
                allowed_roots.append(Path(target.local_output_root).resolve())
            for root in target.local_source_roots:
                allowed_roots.append(Path(root.path).resolve())

        resolved_file: Path | None = None
        candidate = Path(arguments.filename)
        if candidate.is_absolute():
            for allowed_root in allowed_roots:
                try:
                    candidate.relative_to(allowed_root)
                    if candidate.is_file():
                        resolved_file = candidate
                        break
                except ValueError:
                    continue
        else:
            for allowed_root in allowed_roots:
                check = (allowed_root / candidate).resolve()
                try:
                    check.relative_to(allowed_root)
                    if check.is_file():
                        resolved_file = check
                        break
                except ValueError:
                    continue

        if resolved_file is None:
            raise PandratorMcpError(
                "not_found",
                f"File '{arguments.filename}' was not found in approved workspace or source roots.",
            )
        content = resolved_file.read_text(encoding="utf-8")

    parsed_segments = _parse_srt_text(content)
    if not parsed_segments:
        raise PandratorMcpError(
            "validation_error",
            "Could not parse any valid subtitle cues from the provided SRT content.",
        )

    saved = application.save_subtitle_review(
        session_id,
        stage_key,
        expected_revision=arguments.expected_revision,
        segments=parsed_segments,
        idempotency_key=arguments.idempotency_key,
    )

    return ToolOutcome(
        result={
            "schema_version": "1",
            "session_id": session_id,
            "stage": stage_key,
            "revision": saved.get("revision"),
            "artifact_id": saved.get("artifact_id"),
            "document_id": saved.get("document_id"),
            "imported_cues": len(parsed_segments),
        },
        next_actions=[
            NextAction(
                tool="pandrator_preview_subtitles",
                arguments={"session_id": session_id, "stage": stage_key},
                reason="Review imported subtitles in the session.",
            )
        ],
    )
