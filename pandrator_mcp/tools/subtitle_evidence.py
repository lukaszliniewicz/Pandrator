"""Bounded subtitle audio-evidence request and resolution handlers."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.subtitle_evidence import (
    GetSubtitleEvidenceInput,
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


def _record(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("record")
    if not isinstance(source, dict):
        source = payload.get("evidence")
    if not isinstance(source, dict):
        source = payload.get("request")
    if not isinstance(source, dict):
        source = payload
    projected = {key: source[key] for key in _EVIDENCE_KEYS if key in source}
    evidence_id = str(
        projected.get("evidence_id") or projected.get("id") or ""
    ).strip()
    if evidence_id:
        projected["evidence_id"] = evidence_id
    job = payload.get("job")
    if isinstance(job, dict):
        projected.setdefault("job_id", job.get("id"))
        projected.setdefault("job_status", job.get("status"))
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
    )
    result = _record(payload)
    evidence_id = str(result.get("evidence_id") or "")
    return ToolOutcome(
        result=result,
        next_actions=[_status_action(evidence_id)] if evidence_id else [],
    )


def get_subtitle_evidence(
    runtime: McpRuntime,
    arguments: GetSubtitleEvidenceInput,
) -> ToolOutcome:
    payload = runtime.require_application().get_subtitle_evidence(
        arguments.evidence_id
    )
    result = _record(payload)
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
