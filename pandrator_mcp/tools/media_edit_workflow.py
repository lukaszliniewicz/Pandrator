"""Read-only, live media-edit workflow procedure planning."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..schemas.media_edit_workflow import (
    MediaEditSourceReference,
    PlanMediaEditWorkflowInput,
)

_ACTIVE_RUN_STATES = frozenset({"queued", "ready", "running", "finalizing"})
_COMPLETE_STATES = frozenset({"complete", "completed", "succeeded"})
_VAD_OPTION_KEYS = (
    "crispasr_vad_model",
    "crispasr_vad_threshold",
    "crispasr_vad_min_speech_ms",
    "crispasr_vad_min_silence_ms",
    "crispasr_vad_max_speech_seconds",
    "crispasr_vad_speech_pad_ms",
)


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _artifact_id(value: object) -> str | None:
    if isinstance(value, dict):
        candidate = value.get("id") or value.get("artifact_id")
    else:
        candidate = value
    normalized = str(candidate or "").strip()
    return normalized or None


def _revision(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and value.is_integer():
        parsed = int(value)
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return default
    else:
        return default
    return parsed if parsed >= 0 else default


def _safe_session(session: dict[str, Any], session_id: str) -> dict[str, Any]:
    workflow_kind = session.get("workflow_kind") or session.get("kind")
    return {
        "id": session.get("id") or session_id,
        "name": session.get("name"),
        "workflow_kind": workflow_kind,
        "status": session.get("status"),
        "revision": session.get("revision"),
    }


def _stage(workflow: dict[str, Any], key: str) -> dict[str, Any]:
    stages = workflow.get("stages")
    if not isinstance(stages, list):
        return {}
    aliases = {key}
    if key == "transcribe":
        aliases.add("transcription")
    for item in stages:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("key") or item.get("stage_key") or "").strip()
        if candidate in aliases:
            return item
    return {}


def _stage_artifact(stage: dict[str, Any]) -> dict[str, Any]:
    artifact = stage.get("artifact")
    if isinstance(artifact, dict):
        return artifact
    history = stage.get("artifacts")
    if isinstance(history, list):
        selected = str(stage.get("selected_artifact_id") or "")
        for item in history:
            if isinstance(item, dict) and str(item.get("id") or "") == selected:
                return item
    return {}


def _artifact_metadata(artifact: dict[str, Any]) -> dict[str, Any]:
    metadata = artifact.get("metadata_json")
    if not isinstance(metadata, dict):
        metadata = artifact.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded alignment facts; paths and arbitrary provider data stay out."""

    allowed = (
        "alignment_method",
        "timing_source",
        "alignment_coverage",
        "eligible_alignment_coverage",
        "alignment_confidence",
        "confidence",
        "word_count",
        "cue_count",
        "fallback_triggered",
    )
    return {key: metadata[key] for key in allowed if key in metadata}


def _alignment_matches(
    metadata: dict[str, Any],
    method: str,
    *,
    authoritative_transcript_id: str | None,
    source_media_id: str | None,
    expected_ctc_options: dict[str, Any],
    expected_vad_settings: dict[str, Any],
) -> bool:
    if not authoritative_transcript_id or not source_media_id:
        return False
    if str(metadata.get("authoritative_transcript_artifact_id") or "") != (
        authoritative_transcript_id
    ):
        return False
    if str(metadata.get("source_artifact_id") or "") != source_media_id:
        return False
    method_value = str(metadata.get("alignment_method") or "").strip().lower()
    timing_source = str(metadata.get("timing_source") or "").strip().lower()
    if method == "ctc":
        method_matches = (
            method_value in {"ctc", "ctc_alignment", "ctc_cue_alignment"}
            or timing_source == "ctc_alignment"
        )
    elif method == "ctc_asr_fallback":
        # A fallback-enabled run keeps the ordinary CTC method when every
        # cue passes.  When ASR actually fills rejected cues, the backend
        # records the more specific final method below.
        method_matches = (
            method_value
            in {
                "ctc",
                "ctc_alignment",
                "ctc_cue_alignment",
                "ctc_with_asr_fallback",
            }
            or timing_source == "ctc_alignment"
        )
    else:
        return (
            method_value
            in {
                "asr",
                "asr_alignment",
                "asr_lexical_projection",
            }
            or timing_source == "asr_alignment"
        )
    if not method_matches:
        return False
    if method in {"ctc", "ctc_asr_fallback"}:
        if "crispasr_vad_enabled" in expected_vad_settings and bool(
            metadata.get("vad_enabled")
        ) != bool(expected_vad_settings["crispasr_vad_enabled"]):
            return False
        expected_vad_options = {
            key: expected_vad_settings[key]
            for key in _VAD_OPTION_KEYS
            if key in expected_vad_settings
        }
        if expected_vad_options:
            recorded_vad_options = metadata.get("vad_options")
            if not isinstance(recorded_vad_options, dict) or any(
                recorded_vad_options.get(key) != value
                for key, value in expected_vad_options.items()
            ):
                return False
    recorded_options = metadata.get("ctc_options")
    if not isinstance(recorded_options, dict):
        return False
    return all(recorded_options.get(key) == value for key, value in expected_ctc_options.items())


def _caption_alignment_options(arguments: PlanMediaEditWorkflowInput) -> dict[str, Any]:
    return {
        "caption_alignment_method": arguments.caption_alignment_method,
        "caption_alignment_ctc_model": arguments.caption_alignment_ctc_model,
        "caption_alignment_padding_ms": arguments.caption_alignment_padding_ms,
        "caption_alignment_batch_seconds": arguments.caption_alignment_batch_seconds,
        "caption_alignment_min_confidence": arguments.caption_alignment_min_confidence,
        "caption_alignment_fallback_coverage": arguments.caption_alignment_fallback_coverage,
    }


def _idempotency_key(
    action: str,
    *,
    session_id: str,
    session_revision: int,
    plan: dict[str, Any],
    instructions: str,
    inputs: dict[str, Any],
) -> str:
    identity = {
        "schema_version": "1",
        "action": action,
        "session_id": session_id,
        "session_revision": session_revision,
        "plan_id": plan.get("plan_id"),
        "plan_revision_id": plan.get("revision_id"),
        "plan_revision": plan.get("revision"),
        "instructions": instructions,
        "inputs": inputs,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"media-edit-workflow-{action}:{hashlib.sha256(encoded).hexdigest()}"


def _next_action(tool: str, arguments: dict[str, Any], reason: str) -> NextAction:
    return NextAction(tool=tool, arguments=arguments, reason=reason)


def _source_action(
    source: MediaEditSourceReference,
    *,
    role: str,
    session_id: str,
    session_revision: int,
    plan: dict[str, Any],
    instructions: str,
) -> NextAction:
    arguments: dict[str, Any] = {
        "session_id": session_id,
        "expected_session_revision": session_revision,
        "role": role,
    }
    if source.source_asset_id is not None:
        tool = "pandrator_attach_existing_source"
        arguments["source_asset_id"] = source.source_asset_id
    else:
        tool = "pandrator_import_local_source"
        arguments.update({"root": source.root, "relative_path": source.relative_path})
    arguments["idempotency_key"] = _idempotency_key(
        "attach" if tool.endswith("attach_existing_source") else "import",
        session_id=session_id,
        session_revision=session_revision,
        plan=plan,
        instructions=instructions,
        inputs={"role": role, **arguments},
    )
    return _next_action(
        tool,
        arguments,
        f"Attach the supplied {role} source using the inspected session revision.",
    )


def _workflow_transcribe_summary(
    stage: dict[str, Any],
    *,
    needs_execution: bool,
    alignment_matches: bool,
) -> dict[str, Any]:
    artifact = _stage_artifact(stage)
    metadata = _artifact_metadata(artifact)
    return {
        "status": stage.get("status"),
        "job_id": stage.get("job_id"),
        "artifact_id": _artifact_id(artifact),
        "alignment_metadata": _safe_metadata(metadata),
        "alignment_matches": alignment_matches,
        "execution_needed": needs_execution,
    }


def _run_items(payload: object) -> list[dict[str, Any]]:
    values = payload.get("items") if isinstance(payload, dict) else payload
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _artifact_items(payload: object) -> list[dict[str, Any]]:
    return _run_items(payload)


def plan_media_edit_workflow(
    runtime: McpRuntime,
    arguments: PlanMediaEditWorkflowInput,
) -> ToolOutcome:
    """Build a bounded live procedure without performing any downstream mutation."""

    application = runtime.require_application()
    session = _mapping(application.get_session(arguments.session_id))
    if (session.get("workflow_kind") or session.get("kind")) != "media_edit":
        raise PandratorMcpError(
            "ineligible_session",
            "The media-edit workflow planner requires a media_edit session.",
        )
    workflow = _mapping(application.get_workflow(arguments.session_id))
    media_edit = _mapping(application.get_media_edit(arguments.session_id))
    settings = _mapping(application.get_session_settings(arguments.session_id, "stt"))

    session_revision = _revision(session.get("revision"), 1)
    workflow_revision = _revision(workflow.get("revision"), session_revision)
    readiness = _mapping(media_edit.get("readiness"))
    plan = _mapping(media_edit.get("plan"))
    primary_artifact = _mapping(readiness.get("source_media_artifact"))
    external_transcript = _mapping(readiness.get("external_transcript_artifact"))
    transcription_artifact = _mapping(readiness.get("transcription_artifact"))
    timing_artifact = _mapping(readiness.get("timing_artifact"))
    primary_id = _artifact_id(primary_artifact)
    external_transcript_id = _artifact_id(external_transcript)
    transcription_id = _artifact_id(transcription_artifact)
    timing_id = _artifact_id(timing_artifact)

    resolved_mode = (
        "captions"
        if arguments.transcript_mode == "captions"
        or (
            arguments.transcript_mode == "auto"
            and (external_transcript_id or arguments.transcript_source)
        )
        else "asr"
    )
    stage = _stage(workflow, "transcribe")
    stage_status = str(stage.get("status") or "").strip().lower()
    stage_artifact = _stage_artifact(stage)
    stage_metadata = _artifact_metadata(stage_artifact)
    caption_alignment_options = _caption_alignment_options(arguments)
    current_override = settings.get("override")
    current_override = dict(current_override) if isinstance(current_override, dict) else {}
    effective = settings.get("effective")
    effective = dict(effective) if isinstance(effective, dict) else {}
    desired_override = copy.deepcopy(current_override)
    desired_override.update(copy.deepcopy(arguments.stt_overrides))
    if resolved_mode == "captions":
        desired_override.update(caption_alignment_options)
    expected_stt_settings = copy.deepcopy(effective)
    expected_stt_settings.update(desired_override)
    alignment_matches = bool(
        timing_id
        and _alignment_matches(
            stage_metadata,
            arguments.caption_alignment_method,
            authoritative_transcript_id=external_transcript_id,
            source_media_id=primary_id,
            expected_ctc_options=caption_alignment_options,
            expected_vad_settings=expected_stt_settings,
        )
    )
    stage_requires_retry = stage_status in {"stale", "failed"}
    if resolved_mode == "asr":
        standalone_asr_artifact = not bool(
            stage_metadata.get("authoritative_transcript_artifact_id")
        )
        transcribe_needed = (
            stage_requires_retry
            or not standalone_asr_artifact
            or not bool(transcription_id and timing_id)
        )
    else:
        transcribe_needed = stage_requires_retry or not alignment_matches
    stage_running = stage_status in {"queued", "running", "cancel_requested"} and bool(
        stage.get("job_id")
    )

    status_summary: dict[str, Any] = {
        "workflow_revision": workflow_revision,
        "readiness": {
            "primary_video": bool(primary_id),
            "external_transcript": bool(external_transcript_id),
            "transcription": bool(transcription_id),
            "timing": bool(timing_id),
        },
        "transcribe": _workflow_transcribe_summary(
            stage,
            needs_execution=transcribe_needed,
            alignment_matches=alignment_matches,
        ),
        "media_edit_plan": {
            "present": bool(plan),
            "revision": plan.get("revision"),
            "revision_id": plan.get("revision_id"),
            "reviewed": bool(plan.get("reviewed")) if plan else False,
        },
    }

    phases: list[dict[str, Any]] = [
        {
            "phase": "source_setup",
            "status": "complete" if primary_id else "pending",
            "recording_source_supplied": arguments.recording_source is not None,
            "transcript_source_supplied": arguments.transcript_source is not None,
        },
        {"phase": "settings", "status": "pending"},
        {"phase": "transcribe", "status": "pending"},
        {"phase": "prepare", "status": "pending"},
        {"phase": "passive_edit", "status": "pending"},
        {"phase": "review", "status": "pending"},
        {"phase": "render", "status": "pending"},
    ]
    if arguments.materialize:
        phases.append({"phase": "materialize", "status": "pending"})

    next_action: NextAction | None = None
    blocking_reason: str | None = None
    if not primary_id:
        if arguments.recording_source is not None:
            next_action = _source_action(
                arguments.recording_source,
                role="primary",
                session_id=arguments.session_id,
                session_revision=session_revision,
                plan=plan,
                instructions=arguments.instructions,
            )
        else:
            next_action = _next_action(
                "pandrator_browse_local_sources",
                {},
                "No primary video is attached; browse an approved local root before selecting a source file.",
            )
        phases[0]["next_action"] = next_action.model_dump(mode="json")
    elif resolved_mode == "captions" and not external_transcript_id:
        if arguments.transcript_source is not None:
            next_action = _source_action(
                arguments.transcript_source,
                role="transcript",
                session_id=arguments.session_id,
                session_revision=session_revision,
                plan=plan,
                instructions=arguments.instructions,
            )
        else:
            next_action = _next_action(
                "pandrator_browse_local_sources",
                {},
                "Caption mode needs an attached transcript; browse an approved local root before selecting it.",
            )
        phases[0]["status"] = "pending"
        phases[0]["next_action"] = next_action.model_dump(mode="json")
    elif arguments.transcript_mode == "asr" and external_transcript_id:
        blocking_reason = (
            "ASR mode is blocked because an attached transcript is authoritative in the current backend; "
            "ASR cannot supersede those captions."
        )
        phases[0]["status"] = "blocked"
        phases[1]["status"] = "blocked"
        phases[2]["status"] = "blocked"
    else:
        changed_keys = [
            key for key, value in desired_override.items() if effective.get(key) != value
        ]
        phases[1].update(
            {
                "status": "pending" if changed_keys else "complete",
                "changed_keys": changed_keys,
                "revision": settings.get("revision", 0),
            }
        )
        if changed_keys:
            update_arguments = {
                "session_id": arguments.session_id,
                "section": "stt",
                "expected_revision": _revision(settings.get("revision")),
                "value": desired_override,
            }
            update_arguments["idempotency_key"] = _idempotency_key(
                "settings",
                session_id=arguments.session_id,
                session_revision=session_revision,
                plan=plan,
                instructions=arguments.instructions,
                inputs=update_arguments,
            )
            next_action = _next_action(
                "pandrator_update_session_settings",
                update_arguments,
                "Apply the merged STT override section at the inspected settings revision.",
            )
            phases[1]["next_action"] = next_action.model_dump(mode="json")
        elif stage_running:
            phases[2]["status"] = "running"
            next_action = _next_action(
                "pandrator_get_work",
                {
                    "work_type": "job",
                    "work_id": str(stage.get("job_id")),
                    "include_events": True,
                    "wait_seconds": arguments.wait_seconds,
                },
                "The transcribe/alignment stage is already running; monitor its durable work item.",
            )
            phases[2]["next_action"] = next_action.model_dump(mode="json")
        elif transcribe_needed:
            plan_arguments: dict[str, Any] = {
                "session_id": arguments.session_id,
                "target_stage": "transcribe",
                "overrides": {"stt": desired_override},
                "expires_in_minutes": arguments.expires_in_minutes,
            }
            next_action = _next_action(
                "pandrator_plan_workflow",
                plan_arguments,
                "Create the exact transcribe/alignment plan, then execute its returned action and monitor via pandrator_get_work.",
            )
            phases[2]["next_action"] = next_action.model_dump(mode="json")
        else:
            phases[2]["status"] = "complete"
            expected_editorial = (
                external_transcript_id if resolved_mode == "captions" else transcription_id
            )
            plan_media_id = _artifact_id(plan.get("source_media_artifact")) or plan.get(
                "source_media_artifact_id"
            )
            plan_editorial_id = _artifact_id(plan.get("editorial_transcript_artifact")) or plan.get(
                "editorial_transcript_artifact_id"
            )
            plan_timing_id = _artifact_id(plan.get("timing_artifact")) or plan.get(
                "timing_artifact_id"
            )
            active_revision = _revision(plan.get("revision"))
            plan_matches = bool(
                plan
                and active_revision >= 1
                and str(plan_media_id or "") == str(primary_id or "")
                and str(plan_editorial_id or "") == str(expected_editorial or "")
                and str(plan_timing_id or "") == str(timing_id or "")
            )
            if not plan_matches:
                prepare_arguments = {
                    "session_id": arguments.session_id,
                    # The backend's non-forced prepare may reuse an older
                    # revision when newly selected artifacts have identical
                    # bytes. Force only when replacing a mismatched plan so
                    # the new revision is pinned to the exact selected IDs.
                    "force": bool(plan),
                }
                prepare_arguments["idempotency_key"] = _idempotency_key(
                    "prepare",
                    session_id=arguments.session_id,
                    session_revision=session_revision,
                    plan=plan,
                    instructions=arguments.instructions,
                    inputs=prepare_arguments,
                )
                next_action = _next_action(
                    "pandrator_prepare_media_edit",
                    prepare_arguments,
                    "Prepare the media-edit revision pinned to the current media, transcript, and timing artifacts.",
                )
                phases[3]["status"] = "pending"
                phases[3]["next_action"] = next_action.model_dump(mode="json")
            else:
                phases[3]["status"] = "complete"
                runs = _run_items(
                    application.list_media_edit_dispatch_runs(arguments.session_id, limit=100)
                )
                active_revision_id = str(plan.get("revision_id") or "")
                active_run: dict[str, Any] | None = None
                completed_run: dict[str, Any] | None = None
                completed_proposal = bool(
                    plan.get("reviewed")
                    and str(plan.get("instructions") or "").strip() == arguments.instructions
                )
                evidence = _mapping(plan.get("evidence"))
                dispatch_evidence = _mapping(evidence.get("passive_dispatch"))
                provenance_run_id = str(dispatch_evidence.get("dispatch_run_id") or "")
                for candidate in runs:
                    if str(candidate.get("instructions") or "").strip() != arguments.instructions:
                        continue
                    run_id = str(candidate.get("id") or candidate.get("run_id") or "")
                    source_id = str(candidate.get("source_revision_id") or "")
                    source_revision = _revision(
                        candidate.get("source_revision") or candidate.get("source_revision_number")
                    )
                    status = str(candidate.get("status") or "").strip().lower()
                    result_revision_id = str(candidate.get("result_revision_id") or "")
                    if status == "completed" and (
                        (active_revision_id and result_revision_id == active_revision_id)
                        or (provenance_run_id and run_id == provenance_run_id)
                    ):
                        completed_run = candidate
                        completed_proposal = True
                        break
                    if active_run is None and (
                        (active_revision_id and source_id == active_revision_id)
                        or (active_revision and source_revision == active_revision)
                    ):
                        active_run = candidate
                matching_run = completed_run or active_run
                if matching_run is not None and not completed_proposal:
                    run_status = str(matching_run.get("status") or "").strip().lower()
                    if run_status in _ACTIVE_RUN_STATES:
                        phases[4]["status"] = "running"
                        next_action = _next_action(
                            "pandrator_get_media_edit_dispatch_run",
                            {
                                "run_id": str(
                                    matching_run.get("id") or matching_run.get("run_id") or ""
                                )
                            },
                            "Monitor the applicable passive media-edit dispatch run.",
                        )
                        phases[4]["next_action"] = next_action.model_dump(mode="json")
                    else:
                        matching_run = None
                if matching_run is None and not completed_proposal:
                    create_arguments = {
                        "session_id": arguments.session_id,
                        "revision": active_revision,
                        "instructions": arguments.instructions,
                    }
                    create_arguments["idempotency_key"] = _idempotency_key(
                        "dispatch",
                        session_id=arguments.session_id,
                        session_revision=session_revision,
                        plan=plan,
                        instructions=arguments.instructions,
                        inputs=create_arguments,
                    )
                    phases[4]["next_action"] = _next_action(
                        "pandrator_create_media_edit_dispatch_run",
                        create_arguments,
                        "Create one passive whole-recording media-edit run pinned to the active prepared revision.",
                    ).model_dump(mode="json")
                    next_action = NextAction.model_validate(phases[4]["next_action"])
                elif completed_proposal:
                    phases[4]["status"] = "complete"
                if next_action is None and completed_proposal:
                    if not bool(plan.get("reviewed")):
                        phases[5]["status"] = "pending_review"
                        approval_arguments = {
                            "session_id": arguments.session_id,
                            "expected_revision": active_revision,
                            "keep_ranges": list(plan.get("keep_ranges") or []),
                            "reviewed": True,
                        }
                        approval_arguments["idempotency_key"] = _idempotency_key(
                            "approve",
                            session_id=arguments.session_id,
                            session_revision=session_revision,
                            plan=plan,
                            instructions=arguments.instructions,
                            inputs=approval_arguments,
                        )
                        approval_action = _next_action(
                            "pandrator_update_media_edit",
                            approval_arguments,
                            "Execute only after a human or model reviews the current keep_ranges and explicitly approves this proposal.",
                        )
                        phases[5]["approval_action"] = approval_action.model_dump(mode="json")
                        next_action = _next_action(
                            "pandrator_list_media_edit_cuts",
                            {
                                "session_id": arguments.session_id,
                                "revision": active_revision,
                            },
                            "List the unreviewed cuts, then inspect each relevant start/end boundary before considering the gated approval action.",
                        )
                        phases[5]["next_action"] = next_action.model_dump(mode="json")
                    else:
                        phases[5]["status"] = "complete"
                        edit_stage = _stage(workflow, "edit_media")
                        edit_status = str(edit_stage.get("status") or "").strip().lower()
                        rendered = edit_status in _COMPLETE_STATES
                        render_job_id = str(edit_stage.get("job_id") or "")
                        if not render_job_id and not rendered:
                            render_work = _run_items(
                                application.list_work(
                                    session_id=arguments.session_id,
                                    kinds=("media_edit.render",),
                                    states=(
                                        "queued",
                                        "running",
                                        "waiting",
                                        "cancel_requested",
                                    ),
                                    limit=20,
                                )
                            )
                            if render_work:
                                render_job_id = str(render_work[0].get("id") or "")
                                edit_status = str(render_work[0].get("state") or "running")
                        if (
                            edit_status in {"queued", "running", "waiting", "cancel_requested"}
                            and render_job_id
                        ):
                            phases[6]["status"] = "running"
                            next_action = _next_action(
                                "pandrator_get_work",
                                {
                                    "work_type": "job",
                                    "work_id": render_job_id,
                                    "include_events": True,
                                    "wait_seconds": arguments.wait_seconds,
                                },
                                "The reviewed media-edit render is already running; monitor its durable work item.",
                            )
                            phases[6]["next_action"] = next_action.model_dump(mode="json")
                        elif not rendered:
                            render_arguments = {
                                "session_id": arguments.session_id,
                                "revision": active_revision,
                                "wait": arguments.wait_seconds > 0,
                                "timeout_seconds": arguments.wait_seconds,
                            }
                            render_arguments["idempotency_key"] = _idempotency_key(
                                "render",
                                session_id=arguments.session_id,
                                session_revision=session_revision,
                                plan=plan,
                                instructions=arguments.instructions,
                                inputs=render_arguments,
                            )
                            next_action = _next_action(
                                "pandrator_render_media_edit",
                                render_arguments,
                                "Render only the reviewed media-edit revision; use the wait/timeout controls and monitor returned work if needed.",
                            )
                            phases[6]["next_action"] = next_action.model_dump(mode="json")
                        else:
                            phases[6]["status"] = "complete"
                            if arguments.materialize:
                                artifacts = _artifact_items(
                                    application.list_artifacts(
                                        session_id=arguments.session_id,
                                        limit=100,
                                    )
                                )
                                current_media = [
                                    {
                                        "id": item.get("id"),
                                        "role": item.get("role"),
                                        "state": item.get("state"),
                                        "mime_type": item.get("mime_type"),
                                        "size_bytes": item.get("size_bytes"),
                                    }
                                    for item in artifacts
                                    if item.get("role") == "media_edit_media"
                                    and item.get("state") == "current"
                                ][:20]
                                phases[7]["status"] = "pending_download"
                                phases[7]["artifacts"] = current_media
                                if len(current_media) == 1 and current_media[0].get("id"):
                                    download_arguments = {
                                        "artifact_id": str(current_media[0]["id"]),
                                    }
                                    if arguments.filename is not None:
                                        download_arguments["filename"] = arguments.filename
                                    next_action = _next_action(
                                        "pandrator_download_artifact",
                                        download_arguments,
                                        "Download the sole current media-edit render to the approved local output root.",
                                    )
                                else:
                                    list_action_arguments = {
                                        "session_id": arguments.session_id,
                                        "role": "media_edit_media",
                                        "limit": 100,
                                    }
                                    next_action = _next_action(
                                        "pandrator_list_artifacts",
                                        list_action_arguments,
                                        "List current media_edit_media artifacts, then download the selected artifact with pandrator_download_artifact.",
                                    )
                                phases[7]["next_action"] = next_action.model_dump(mode="json")
                            else:
                                # Materialization is optional; with no delivery
                                # request the render phase is the terminal phase.
                                pass

    if blocking_reason:
        result = {
            "schema_version": "1",
            "plan_type": "media_edit_workflow",
            "session": _safe_session(session, arguments.session_id),
            "resolved_transcript_mode": resolved_mode,
            "current_status": status_summary,
            "phases": phases,
            "phase_order": [str(item["phase"]) for item in phases],
            "blocking_reason": blocking_reason,
            "next_action": None,
            "procedure_semantics": "This is a live read-only procedure, not an atomic execution snapshot.",
        }
        return ToolOutcome(result=result)

    result = {
        "schema_version": "1",
        "plan_type": "media_edit_workflow",
        "session": _safe_session(session, arguments.session_id),
        "resolved_transcript_mode": resolved_mode,
        "current_status": status_summary,
        "phases": phases,
        "phase_order": [str(item["phase"]) for item in phases],
        "next_action": next_action.model_dump(mode="json") if next_action else None,
        "procedure_semantics": (
            "This is a live read-only procedure, not an atomic execution snapshot. "
            "Re-inspect state after every returned action and use the exact action "
            "returned by downstream planning/execution tools."
        ),
    }
    return ToolOutcome(result=result, next_actions=[next_action] if next_action else [])
