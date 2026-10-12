"""Bounded subtitle audio-evidence request and resolution handlers."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.subtitle_evidence import (
    GetSubtitleEvidenceInput,
    GetSubtitleEvidenceRoutesInput,
    RequestSubtitleEvidenceInput,
    ResolveSubtitleEvidenceInput,
)

_EVIDENCE_KEYS = (
    "id",
    "evidence_id",
    "session_id",
    "source_artifact_id",
    "source_media_artifact_id",
    "source_revision_id",
    "source_segment_id",
    "cue_id",
    "start_ms",
    "end_ms",
    "clip_start_ms",
    "clip_end_ms",
    "reason",
    "routes",
    "routes_json",
    "audio_model_ids",
    "audio_model_ids_json",
    "status",
    "job_id",
    "job_status",
    "clip_artifact_id",
    "candidates",
    "candidates_json",
    "resolution",
    "resolution_json",
    "error_message",
    "created_at",
    "updated_at",
)
_CANDIDATE_SUMMARY_KEYS = (
    "id",
    "route",
    "status",
    "text",
    "context_text",
    "selection_method",
    "language",
    "timing_kind",
    "timing_method",
    "provider",
    "model",
    "engine",
    "compute_backend",
    "transcript_artifact_id",
    "error",
    "error_message",
)
_COST_SUMMARY_KEYS = (
    "kind",
    "amount",
    "currency",
    "unit",
    "cost_source",
    "price_effective_until",
    "usage_reported_by_provider",
    "submitted_audio_seconds",
    "billable_audio_seconds",
    "billing_increment_seconds",
)
_CACHE_HASH_KEYS = (
    "key_sha256",
    "media_sha256",
    "configuration_sha256",
    "prompt_sha256",
    "source_language_sha256",
)
_CACHE_WINDOW_KEYS = ("clip_window_ms", "cue_window_ms")
_REUSED_FROM_KEYS = (
    "evidence_id",
    "candidate_id",
    "transcript_artifact_id",
    "source_artifact_id",
    "source_revision_id",
    "source_media_artifact_id",
)


def _cost_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: item
        for key in _COST_SUMMARY_KEYS
        if key in value
        and isinstance(
            (item := value[key]),
            (str, int, float, bool),
        )
        and not (isinstance(item, float) and not math.isfinite(item))
        and not (isinstance(item, str) and len(item) > 200)
    }


def _cache_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    version = value.get("version")
    if isinstance(version, int) and not isinstance(version, bool):
        result["version"] = version
    for key in ("route", "model_id"):
        item = value.get(key)
        if isinstance(item, str) and len(item) <= 200:
            result[key] = item
    for key in _CACHE_HASH_KEYS:
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item):
            result[key] = item
    for key in _CACHE_WINDOW_KEYS:
        item = value.get(key)
        if (
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(part, int) and not isinstance(part, bool) for part in item)
        ):
            result[key] = list(item)
    return result


def _reused_from_summary(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: item
        for key in _REUSED_FROM_KEYS
        if isinstance((item := value.get(key)), str) and len(item) <= 200
    }


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: candidate[key]
        for key in _CANDIDATE_SUMMARY_KEYS
        if key in candidate
    }
    segments = candidate.get("segments")
    words = candidate.get("words")
    segment_items = segments if isinstance(segments, list) else []
    result["segments_count"] = len(segment_items)
    if isinstance(words, list):
        words_count = len(words)
    else:
        words_count = sum(
            len(segment["words"])
            for segment in segment_items
            if isinstance(segment, dict) and isinstance(segment.get("words"), list)
        )
    result["words_count"] = words_count
    result["has_timing_detail"] = bool(segment_items or words_count)
    for key, projector in (
        ("cost", _cost_summary),
        ("cache", _cache_summary),
        ("reused_from", _reused_from_summary),
    ):
        if key in candidate:
            projected = projector(candidate[key])
            if projected is not None:
                result[key] = projected
    return result


def _candidate_collection(value: Any) -> Any:
    if isinstance(value, list):
        return [
            _candidate_summary(candidate)
            for candidate in value
            if isinstance(candidate, dict)
        ]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        if isinstance(decoded, list):
            return json.dumps(
                [
                    _candidate_summary(candidate)
                    for candidate in decoded
                    if isinstance(candidate, dict)
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
    return []


def _record(
    payload: dict[str, Any],
    *,
    detail: Literal["summary", "full"] = "summary",
) -> dict[str, Any]:
    source = payload.get("record")
    if not isinstance(source, dict):
        source = payload.get("evidence")
    if not isinstance(source, dict):
        source = payload.get("request")
    if not isinstance(source, dict):
        source = payload
    projected = {key: source[key] for key in _EVIDENCE_KEYS if key in source}
    if detail == "summary":
        for key in ("candidates", "candidates_json"):
            if key in source:
                projected[key] = _candidate_collection(source[key])
    evidence_id = str(
        projected.get("evidence_id") or projected.get("id") or ""
    ).strip()
    if evidence_id:
        projected["evidence_id"] = evidence_id
    job = payload.get("job")
    if isinstance(job, dict):
        projected.setdefault("job_id", job.get("id"))
        projected.setdefault("job_status", job.get("status"))
        if projected.get("status") in {"queued", "running"} and job.get("status") in {
            "failed", "cancelled", "canceled",
        }:
            projected["status"] = "failed"
            projected["error_message"] = (
                projected.get("error_message") or job.get("error_message")
                or "The subtitle evidence job did not complete."
            )
    projected["schema_version"] = "1"
    return projected


def _status_action(evidence_id: str) -> NextAction:
    return NextAction(
        tool="pandrator_get_subtitle_evidence",
        arguments={"evidence_id": evidence_id},
        reason=(
            "Check the bounded re-transcription after its durable job has had time to "
            "run. Renew any active dispatch lease before it expires."
        ),
    )


def request_subtitle_evidence(
    runtime: McpRuntime,
    arguments: RequestSubtitleEvidenceInput,
) -> ToolOutcome:
    payload = runtime.require_application().request_subtitle_evidence(
        arguments.session_id,
        source_artifact_id=arguments.source_artifact_id,
        cue_id=arguments.cue_id,
        reason=arguments.reason,
        routes=list(arguments.routes),
        audio_model_ids=list(arguments.audio_model_ids),
        padding_before_ms=arguments.padding_before_ms,
        padding_after_ms=arguments.padding_after_ms,
        idempotency_key=arguments.idempotency_key,
        **({"force_refresh": True} if arguments.force_refresh else {}),
    )
    result = _record(payload)
    evidence_id = str(result.get("evidence_id") or "")
    return ToolOutcome(
        result=result,
        next_actions=[_status_action(evidence_id)] if evidence_id else [],
    )


def get_subtitle_evidence_routes(runtime: McpRuntime, arguments: GetSubtitleEvidenceRoutesInput) -> dict[str, Any]:
    return runtime.require_application().get_subtitle_evidence_routes(
        language=arguments.language, include_languages=arguments.include_languages,
    )


def get_subtitle_evidence(
    runtime: McpRuntime,
    arguments: GetSubtitleEvidenceInput,
) -> ToolOutcome:
    payload = runtime.require_application().get_subtitle_evidence(
        arguments.evidence_id
    )
    result = _record(payload, detail=arguments.detail)
    status = str(result.get("status") or "").strip().lower()
    return ToolOutcome(
        result=result,
        next_actions=(
            [_status_action(arguments.evidence_id)]
            if status in {"queued", "running"}
            else []
        ),
    )


def resolve_subtitle_evidence(
    runtime: McpRuntime,
    arguments: ResolveSubtitleEvidenceInput,
) -> dict[str, Any]:
    return _record(
        runtime.require_application().resolve_subtitle_evidence(
            arguments.session_id,
            arguments.evidence_id,
            action=arguments.action,
            candidate_id=arguments.candidate_id,
            text=arguments.text,
            note=arguments.note,
            idempotency_key=arguments.idempotency_key,
        )
    )
