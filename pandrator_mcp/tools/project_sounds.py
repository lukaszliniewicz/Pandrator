"""Project sound-plan projections and typed handlers."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.project_sounds import ProjectSoundsInput
from ..work_mapping import application_work_reference
from .sound_results import _application, _mapping


def _safe_project_cue(value: Any) -> dict[str, Any]:
    raw = _mapping(value)
    cue = {
        key: raw[key]
        for key in (
            "logical_id",
            "variant_id",
            "included",
            "placement_status",
        )
        if key in raw
    }
    anchor = _mapping(raw.get("anchor"))
    if anchor:
        cue["anchor"] = {
            key: anchor[key]
            for key in ("kind", "segment_id", "time_seconds", "quote", "occurrence", "start", "end")
            if key in anchor
        }
        measured = _mapping(anchor.get("resolution"))
        placement = {
            key: measured[key][:120]
            for key in ("status", "reason")
            if isinstance(measured.get(key), str)
        }
        if placement:
            cue["placement"] = placement
    playback = _mapping(raw.get("playback"))
    if playback:
        cue["playback"] = {
            key: playback[key]
            for key in (
                "mode",
                "offset_ms",
                "gain_db",
                "trim_start_seconds",
                "trim_end_seconds",
                "fade_in_ms",
                "fade_out_ms",
            )
            if key in playback
        }
    sound = _mapping(raw.get("sound"))
    if sound:
        cue["sound"] = {
            key: sound[key]
            for key in ("name", "artifact_id", "duration_seconds", "content_hash", "status")
            if key in sound
        }
    return cue


def _safe_project_reply(value: Any, *, mutation: bool = False) -> dict[str, Any]:
    raw = _mapping(value)
    result = {
        key: raw[key]
        for key in ("session_id", "revision", "plan_id", "speech_plan_revision_id")
        if key in raw
    }
    if mutation:
        if isinstance(raw.get("cue"), dict):
            result["cue"] = _safe_project_cue(raw["cue"])
        for key in ("removed_logical_id", "restored_from_plan_id"):
            if key in raw:
                result[key] = raw[key]
        return result
    page = _mapping(raw.get("cues"))
    result["cues"] = {
        "items": [
            _safe_project_cue(item) for item in page.get("items") or [] if isinstance(item, dict)
        ],
        "next_cursor": page.get("next_cursor"),
    }
    return result


_RECEIPT_ID_FIELDS = (
    "session_id",
    "plan_id",
    "sound_plan_id",
    "speech_plan_revision_id",
    "mix_id",
    "render_id",
    "job_id",
    "work_id",
    "artifact_id",
    "base_artifact_id",
    "timeline_artifact_id",
)


def _safe_project_job_receipt(value: Any) -> dict[str, Any]:
    raw = _mapping(value)
    nested_job = _mapping(raw.get("job"))
    nested_mix = _mapping(raw.get("mix"))
    result: dict[str, Any] = {}
    for key in _RECEIPT_ID_FIELDS:
        item = raw.get(key)
        if not isinstance(item, str):
            item = nested_job.get(key)
        if not isinstance(item, str) and key == "job_id":
            item = nested_job.get("id") or raw.get("work_id")
        if isinstance(item, str):
            result[key] = item[:200]
    for key in ("revision", "sound_plan_revision", "remaining_count"):
        item = raw.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            result[key] = item
    for key in ("status", "job_status"):
        item = raw.get(key)
        if isinstance(item, str):
            result[key] = item[:80]
    job_status = nested_job.get("status")
    if isinstance(job_status, str):
        result["job_status"] = job_status[:80]
    mix_status = nested_mix.get("status")
    if "status" not in result and isinstance(mix_status, str):
        result["status"] = mix_status[:80]
    fingerprint = raw.get("request_fingerprint")
    if isinstance(fingerprint, str):
        result["request_fingerprint"] = fingerprint[:128]
    cache_hit = raw.get("cache_hit")
    if isinstance(cache_hit, bool):
        result["cache_hit"] = cache_hit
    elif isinstance(nested_job.get("cache_hit"), bool):
        result["cache_hit"] = nested_job["cache_hit"]
    return result


def _project_job_outcome(value: Any, *, always_tool_outcome: bool) -> ToolOutcome | dict[str, Any]:
    raw = _mapping(value)
    nested_job = _mapping(raw.get("job"))
    job_id = (
        raw.get("job_id") or raw.get("work_id") or nested_job.get("job_id") or nested_job.get("id")
    )
    job_status = nested_job.get("status") or raw.get("job_status") or raw.get("status")
    cached = raw.get("cache_hit") is True or nested_job.get("cache_hit") is True
    result = _safe_project_job_receipt(raw)
    active = (
        isinstance(job_id, str)
        and job_status in {"active", "queued", "running", "cancel_requested"}
        and not cached
    )
    if not active:
        return ToolOutcome(result=result) if always_tool_outcome else result
    payload = dict(raw)
    payload["job_id"] = job_id
    if isinstance(job_status, str):
        payload["status"] = job_status
    return ToolOutcome(
        result=result,
        work=application_work_reference(payload),
        next_actions=[
            NextAction(
                tool="pandrator_get_work",
                arguments={"work_id": job_id, "work_type": "job"},
                reason="Inspect the project sound-plan job until it finishes.",
            )
        ],
    )


def project_sounds(
    runtime: McpRuntime, arguments: ProjectSoundsInput
) -> ToolOutcome | dict[str, Any]:
    application = _application(runtime)
    action = arguments.action
    if action == "get":
        return _safe_project_reply(
            application.get_project_sound_plan(
                arguments.session_id,
                segment_ids=arguments.segment_ids,
                cursor=arguments.cursor,
                limit=arguments.limit,
            )
        )
    if action == "status":
        return _project_job_outcome(
            application.get_project_sound_mix_status(
                arguments.session_id, base_artifact_id=arguments.base_artifact_id
            ),
            always_tool_outcome=False,
        )
    if action == "add":
        assert arguments.anchor is not None
        return ToolOutcome(
            result=_safe_project_reply(
                application.add_project_sound_cue(
                    arguments.session_id,
                    expected_revision=arguments.expected_revision or 0,
                    variant_id=arguments.variant_id or "",
                    anchor=arguments.anchor.model_dump(mode="json", exclude_none=True),
                    playback=(
                        arguments.playback.model_dump(mode="json", exclude_unset=True)
                        if arguments.playback is not None
                        else None
                    ),
                    included=arguments.included,
                    idempotency_key=arguments.idempotency_key or "",
                ),
                mutation=True,
            )
        )
    if action == "update":
        changes: dict[str, Any] = {}
        for field in ("variant_id", "anchor", "playback", "included"):
            if field not in arguments.model_fields_set:
                continue
            value = getattr(arguments, field)
            if field == "anchor" and value is not None:
                value = value.model_dump(mode="json", exclude_none=True)
            elif field == "playback" and value is not None:
                value = value.model_dump(mode="json", exclude_unset=True)
            changes[field] = value
        return ToolOutcome(
            result=_safe_project_reply(
                application.update_project_sound_cue(
                    arguments.session_id,
                    arguments.logical_cue_id or "",
                    expected_revision=arguments.expected_revision or 1,
                    changes=changes,
                    idempotency_key=arguments.idempotency_key or "",
                ),
                mutation=True,
            )
        )
    if action == "remove":
        return ToolOutcome(
            result=_safe_project_reply(
                application.remove_project_sound_cue(
                    arguments.session_id,
                    arguments.logical_cue_id or "",
                    expected_revision=arguments.expected_revision or 1,
                    idempotency_key=arguments.idempotency_key or "",
                ),
                mutation=True,
            )
        )
    if action == "restore":
        return ToolOutcome(
            result=_safe_project_reply(
                application.restore_project_sound_plan(
                    arguments.session_id,
                    expected_revision=arguments.expected_revision or 0,
                    source_plan_id=arguments.source_plan_id or "",
                    idempotency_key=arguments.idempotency_key or "",
                ),
                mutation=True,
            )
        )
    if action == "render":
        return _project_job_outcome(
            application.render_project_sound_plan(
                arguments.session_id,
                expected_revision=arguments.expected_revision or 0,
                base_artifact_id=arguments.base_artifact_id or "",
                timeline_artifact_id=arguments.timeline_artifact_id,
                idempotency_key=arguments.idempotency_key or "",
            ),
            always_tool_outcome=True,
        )
    if action == "resolve":
        return _project_job_outcome(
            application.resolve_project_sound_plan(
                arguments.session_id,
                expected_revision=arguments.expected_revision or 1,
                cue_ids=arguments.cue_ids,
                idempotency_key=arguments.idempotency_key or "",
            ),
            always_tool_outcome=True,
        )
    raise ValueError("Unknown project-sounds action.")


__all__ = [
    "_safe_project_cue",
    "_safe_project_job_receipt",
    "_safe_project_reply",
    "project_sounds",
]
