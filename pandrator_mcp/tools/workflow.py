"""Review-first workflow planning and exact execution."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..schemas import (
    CreateDispatchRunInput,
    CreateSpeechOptimizationDispatchRunInput,
    ExecuteWorkflowPlanInput,
    PlanOrchestratedWorkflowInput,
    PlanWorkflowInput,
)
from ..schemas.common import WarningMessage
from ..work_mapping import application_work_reference

_PASSIVE_STAGE_ORDER = (
    "correction",
    "translation",
    "speech_optimization",
)
_WORKFLOW_STAGE_ALIASES = {
    "correction": ("correction", "correct"),
    "translation": ("translation", "translate"),
    "speech_optimization": ("speech_optimization", "optimize_tts"),
}


def _safe_revision(value: object) -> object:
    """Keep revision identity finite and stable without exposing live metadata."""

    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return str(value)


def _orchestrated_idempotency_key(
    *,
    session_id: str,
    session_revision: object,
    workflow_revision: object,
    goal: str,
    passive_stages: tuple[str, ...],
    stage: str,
    create_arguments: dict[str, object],
    explicit_overrides: object = None,
) -> str:
    """Return a stable procedure retry identity for one live passive stage."""

    identity = {
        "schema_version": "1",
        "session_id": session_id,
        "session_revision": _safe_revision(session_revision),
        "workflow_revision": _safe_revision(workflow_revision),
        "goal": goal,
        "passive_stages": list(passive_stages),
        "stage": stage,
        "create_arguments": create_arguments,
    }
    # Include the stage's explicit setting overlay so a changed caller intent
    # cannot accidentally reuse a retained ledger reservation, even when an
    # unknown setting does not affect the currently supported transport shape.
    if explicit_overrides is not None:
        identity["explicit_overrides"] = explicit_overrides
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"orchestrated-{stage}:{digest}"


def _safe_session(session: dict[str, object], session_id: str) -> dict[str, object]:
    workflow_kind = session.get("workflow_kind") or session.get("kind")
    return {
        "id": session.get("id") or session_id,
        "name": session.get("name"),
        "workflow_kind": workflow_kind,
        "kind": workflow_kind,
        "revision": session.get("revision"),
        "status": session.get("status"),
    }


def _safe_stage_statuses(workflow: dict[str, object]) -> list[dict[str, object]]:
    source = workflow.get("stages")
    if not isinstance(source, list):
        return []
    statuses: list[dict[str, object]] = []
    for item in source[:50]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("stage_key") or "").strip()
        if not key:
            continue
        stage: dict[str, object] = {
            "stage": key,
            "key": key,
            "status": item.get("status"),
        }
        for field in (
            "included",
            "executable",
            "enabled",
            "stale_reason",
            "job_id",
            "agent_run_id",
            "selected_artifact_id",
            "selection_revision",
        ):
            if field in item:
                stage[field] = item[field]
        current_artifact = item.get("artifact")
        if not isinstance(current_artifact, dict):
            current_artifact = item.get("current_artifact")
        if isinstance(current_artifact, dict):
            artifact = {
                field: current_artifact[field]
                for field in ("id", "role", "state", "content_hash")
                if field in current_artifact
            }
            if artifact:
                stage["artifact"] = artifact
        statuses.append(stage)
    return statuses


def _effective_settings(
    application: object,
    session_id: str,
    section: str,
) -> dict[str, object]:
    payload = application.get_session_settings(session_id, section)
    effective = payload.get("effective") if isinstance(payload, dict) else None
    return dict(effective) if isinstance(effective, dict) else {}


def _merged_settings(
    application: object,
    session_id: str,
    section: str,
    override: object,
) -> dict[str, object]:
    settings = _effective_settings(application, session_id, section)
    if isinstance(override, dict):
        settings.update(copy.deepcopy(override))
    return settings


def _normalise_glossary(value: object, *, depth: int = 0) -> dict[str, str]:
    """Mirror the core glossary parser while accepting only safe string pairs."""

    if depth > 3 or value is None:
        return {}
    if isinstance(value, Mapping):
        result: dict[str, str] = {}
        for source, target in value.items():
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            source_text = source.strip()
            target_text = target.strip()
            if source_text and target_text:
                result[source_text] = target_text
        return result
    if isinstance(value, (list, tuple)):
        result = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            source = next(
                (
                    item.get(key)
                    for key in ("source", "term")
                    if isinstance(item.get(key), str) and item.get(key).strip()
                ),
                None,
            )
            target = next(
                (
                    item.get(key)
                    for key in ("target", "translation", "value")
                    if isinstance(item.get(key), str) and item.get(key).strip()
                ),
                None,
            )
            if isinstance(source, str) and isinstance(target, str):
                result[source.strip()] = target.strip()
        return result
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    else:
        if parsed != value:
            return _normalise_glossary(parsed, depth=depth + 1)
    result = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        source, target = line.split("=", 1)
        source_text = source.strip()
        target_text = target.strip()
        if source_text and target_text:
            result[source_text] = target_text
    return result


def _validated_packet(
    model: type[Any],
    packet: dict[str, object],
    stage: str,
) -> dict[str, object]:
    candidate = dict(packet)
    candidate.setdefault("idempotency_key", f"orchestrated-{stage}:00000000")
    try:
        validated = model.model_validate(candidate)
    except ValidationError as error:
        raise PandratorMcpError(
            "validation_error",
            f"Live {stage} settings cannot form a valid passive run.",
            details={"errors": error.errors(include_url=False)},
        ) from error
    return validated.model_dump(
        mode="json",
        exclude={"idempotency_key"},
        exclude_unset=True,
    )


def _passive_override(arguments: PlanOrchestratedWorkflowInput, stage: str) -> object:
    if stage in {"correction", "translation"}:
        return arguments.overrides.get(stage)
    text = arguments.overrides.get("text")
    tts = arguments.overrides.get("tts")
    return {"text": text, "tts": tts}


def _resolved_passive_packet(
    application: object,
    session: dict[str, object],
    workflow: dict[str, object],
    arguments: PlanOrchestratedWorkflowInput,
    stage: str,
) -> tuple[str, dict[str, object]]:
    session_id = arguments.session_id
    session_source = session.get("source_language")
    session_target = session.get("target_language")
    stage_override = arguments.overrides.get(stage)
    packet_model: type[Any]
    if stage in {"correction", "translation"}:
        packet_model = CreateDispatchRunInput
        settings = _merged_settings(application, session_id, stage, stage_override)
        packet: dict[str, object] = {
            "session_id": session_id,
            "kind": stage,
            "execution_mode": arguments.execution_mode,
            "max_parallel_batches": arguments.max_parallel_batches,
            "context_capsule": arguments.context_capsule.model_dump(mode="json"),
            "source_language": settings.get("source_language") or session_source,
            "target_language": (
                settings.get("target_language") or session_target
                if stage == "translation"
                else None
            ),
            "instructions": settings.get("instructions") or "",
            "char_limit": settings.get("char_limit", 6_000),
            "max_segments_per_batch": settings.get("max_segments_per_batch", 40),
            "no_remove_subtitles": settings.get("no_remove_subtitles", False),
            "context_before": settings.get("context_before", 8),
            "context_after": settings.get("context_after", 2),
            "timing_context_mode": settings.get("timing_context_mode", "full"),
            "substantial_gap_ms": settings.get("substantial_gap_ms", 2_000),
            "glossary": {},
        }
        if "source_artifact_id" in settings:
            packet["source_artifact_id"] = settings["source_artifact_id"]
        if stage == "translation":
            explicit_glossary = (
                stage_override.get("glossary") if isinstance(stage_override, dict) else None
            )
            if _normalise_glossary(explicit_glossary):
                packet["glossary"] = _normalise_glossary(explicit_glossary)
            elif settings.get("glossary_enabled"):
                packet["glossary"] = _normalise_glossary(settings.get("glossary"))
        resolved = _validated_packet(packet_model, packet, stage)
    else:
        packet_model = CreateSpeechOptimizationDispatchRunInput
        text_override = arguments.overrides.get("text")
        tts_override = arguments.overrides.get("tts")
        text_settings = _merged_settings(application, session_id, "text", text_override)
        tts_settings = _merged_settings(application, session_id, "tts", tts_override)
        packet = {
            "session_id": session_id,
            "execution_mode": arguments.execution_mode,
            "max_parallel_batches": arguments.max_parallel_batches,
            "context_capsule": arguments.context_capsule.model_dump(mode="json"),
            "language": None,
            "voice_language": tts_settings.get("language") or None,
            "tts_service": tts_settings.get("service") or None,
            "instructions": text_settings.get("combined_prompt") or "",
            "char_limit": 20_000,
            "max_units_per_batch": text_settings.get("llm_tts_document_batch_size", 8),
            "context_before": 4,
            "context_after": 2,
            "include_timing": True,
        }
        if isinstance(text_override, dict) and "source_artifact_id" in text_override:
            packet["source_artifact_id"] = text_override["source_artifact_id"]
        try:
            raw_batch_size = packet["max_units_per_batch"]
            batch_size = (
                8 if raw_batch_size is None or raw_batch_size == "" else int(raw_batch_size)
            )
        except (TypeError, ValueError):
            batch_size = 8
        packet["max_units_per_batch"] = max(1, min(batch_size, 500))
        resolved = _validated_packet(packet_model, packet, stage)
    explicit = _passive_override(arguments, stage)
    key = _orchestrated_idempotency_key(
        session_id=session_id,
        session_revision=session.get("revision"),
        workflow_revision=workflow.get("revision"),
        goal=arguments.goal,
        passive_stages=arguments.passive_stages,
        stage=stage,
        create_arguments=resolved,
        explicit_overrides=explicit,
    )
    resolved = _validated_packet(
        packet_model,
        {**resolved, "idempotency_key": key},
        stage,
    )
    resolved["idempotency_key"] = key
    return key, resolved


def _passive_phase(
    session_id: str,
    stage: str,
    current_status: object,
    create_arguments: dict[str, object],
) -> dict[str, object]:
    execution_mode = str(create_arguments.get("execution_mode") or "serial")
    max_parallel_batches = int(create_arguments.get("max_parallel_batches") or 1)
    if stage in {"correction", "translation"}:
        create_tool = "pandrator_create_dispatch_run"
        get_tool = "pandrator_get_dispatch_run"
        claim_tool = "pandrator_claim_dispatch_batch"
        submit_tool = "pandrator_submit_dispatch_batch"
        completion_condition = (
            "The dispatch run status is completed after every batch has been claimed, "
            "processed, and submitted exactly once in the selected serial or bounded-"
            "parallel mode; stop on a terminal failure."
        )
    else:
        create_tool = "pandrator_create_speech_optimization_dispatch_run"
        get_tool = "pandrator_get_speech_optimization_dispatch_run"
        claim_tool = "pandrator_claim_speech_optimization_dispatch_batch"
        submit_tool = "pandrator_submit_speech_optimization_dispatch_batch"
        completion_condition = (
            "The speech-optimization run status is completed after every batch has "
            "been claimed, optimized, and submitted exactly once in the selected "
            "serial or bounded-parallel mode; stop on a terminal failure."
        )
    create = {
        "tool": create_tool,
        "arguments": create_arguments,
        "reason": f"Create the passive {stage} run for the model to process.",
    }
    loop = {
        "get_tool": get_tool,
        "claim_tool": claim_tool,
        "submit_tool": submit_tool,
        "execution_mode": execution_mode,
        "max_parallel_batches": max_parallel_batches,
        "claim_strategy": (
            "Claim one batch, submit it, then claim the next so each batch receives "
            "the accumulated capsule and prior accepted output."
            if execution_mode == "serial"
            else (
                f"For each wave, claim up to {max_parallel_batches} sibling batches "
                "with distinct idempotency keys before assigning them concurrently. "
                "Every sibling receives the same pre-wave capsule; submit or release "
                "every sibling before claiming the next wave."
            )
        ),
        "context_strategy": (
            "Treat delegation.context_capsule and boundary context as read-only. "
            "Return only newly learned terminology, entities, style rules, decisions, "
            "or notes in context_delta; Pandrator merges accepted deltas by source "
            "ordinal rather than worker completion order."
        ),
        "completion_condition": completion_condition,
    }
    return {
        "stage": stage,
        "mode": "passive",
        "current_status": current_status,
        "tool": create_tool,
        "arguments": create_arguments,
        "create_tool": create_tool,
        "create_arguments": create_arguments,
        "create": create,
        "loop": loop,
        "claim_tool": claim_tool,
        "submit_tool": submit_tool,
        "completion_condition": completion_condition,
    }


def plan_orchestrated_workflow(
    runtime: McpRuntime,
    arguments: PlanOrchestratedWorkflowInput,
) -> ToolOutcome:
    """Build a safe procedure for passive stages followed by one native plan."""

    application = runtime.require_application()
    session = application.get_session(arguments.session_id)
    workflow_kind = str(session.get("workflow_kind") or session.get("kind") or "").strip().lower()
    allowed_passive = {
        "audiobook": {"speech_optimization"},
        "subtitles": {"correction", "translation"},
        "voiceover": set(_PASSIVE_STAGE_ORDER),
    }.get(workflow_kind)
    if allowed_passive is not None:
        unsupported = [stage for stage in arguments.passive_stages if stage not in allowed_passive]
        if unsupported:
            raise PandratorMcpError(
                "validation_error",
                f"{unsupported[0]} is not available for {workflow_kind} workflows.",
                details={
                    "workflow_kind": workflow_kind,
                    "unsupported_passive_stages": unsupported,
                },
            )
    if workflow_kind == "subtitles" and arguments.final_stage == "generate_audio":
        raise PandratorMcpError(
            "validation_error",
            "Subtitle-only workflows cannot generate audio.",
            details={"workflow_kind": workflow_kind, "final_stage": "generate_audio"},
        )
    workflow = application.get_workflow(arguments.session_id)
    statuses = _safe_stage_statuses(workflow)
    status_by_stage = {
        str(item["stage"]): item.get("status") for item in statuses if item.get("stage")
    }

    safe_overrides = copy.deepcopy(arguments.overrides)
    if arguments.final_stage == "export":
        output = safe_overrides.get("output")
        merged_output = dict(output) if isinstance(output, dict) else {}
        merged_output.update(
            {
                "export_mode": arguments.export_mode,
                "audio_mode": arguments.audio_mode,
                "subtitle_mode": arguments.subtitle_mode,
                "subtitle_selection": arguments.subtitle_selection,
                "subtitle_format": arguments.subtitle_format,
            }
        )
        safe_overrides["output"] = merged_output

    final_plan_arguments = {
        "session_id": arguments.session_id,
        "target_stage": arguments.final_stage,
        "overrides": safe_overrides,
        "expires_in_minutes": arguments.expires_in_minutes,
    }
    phases = [
        _passive_phase(
            arguments.session_id,
            stage,
            next(
                (
                    status_by_stage[alias]
                    for alias in _WORKFLOW_STAGE_ALIASES.get(stage, (stage,))
                    if alias in status_by_stage
                ),
                None,
            ),
            _resolved_passive_packet(
                application,
                session,
                workflow,
                arguments,
                stage,
            )[1],
        )
        for stage in arguments.passive_stages
    ]
    final_phase: dict[str, object] = {
        "stage": arguments.final_stage,
        "final_stage": arguments.final_stage,
        "mode": "native",
        "tool": "pandrator_plan_workflow",
        "arguments": final_plan_arguments,
        "plan_tool": "pandrator_plan_workflow",
        "plan_arguments": final_plan_arguments,
        "execute_tool": "pandrator_execute_workflow_plan",
        "execute": {
            "tool": "pandrator_execute_workflow_plan",
            "source": "pandrator_plan_workflow.next_action",
        },
        "execute_condition": (
            "Use the exact pandrator_execute_workflow_plan next_action returned by "
            "pandrator_plan_workflow after reviewing its immutable plan and confirmations."
        ),
        "monitor_tool": "pandrator_get_work",
        "monitor_arguments": {
            "work_type": "job",
            "work_id": "<work id returned by pandrator_execute_workflow_plan>",
            "include_events": True,
            "wait_seconds": arguments.wait_seconds,
        },
        "completion_condition": (
            "The returned durable work state is succeeded; inspect its error and "
            "result summary if it is failed or cancelled."
        ),
    }
    phases.append(final_phase)
    if arguments.materialize:
        phases.append(
            {
                "stage": "materialize",
                "mode": "delivery",
                "list_tool": "pandrator_list_artifacts",
                "list_arguments": {
                    "session_id": arguments.session_id,
                    "role": "export",
                    "limit": 100,
                },
                "selection": {
                    "role": "export",
                    "state": "current",
                },
                "download_tool": "pandrator_download_artifact",
                "download_arguments": {
                    "artifact_id": "<artifact id returned by pandrator_list_artifacts>",
                    **({"filename": arguments.filename} if arguments.filename is not None else {}),
                },
                "completion_condition": (
                    "Select the current export artifact, then download it with its "
                    "returned id and a plain filename."
                ),
            }
        )

    first_phase = phases[0]
    if arguments.passive_stages:
        first_action = NextAction(
            tool=str(first_phase["create_tool"]),
            arguments=dict(first_phase["create_arguments"]),
            reason=str((first_phase["create"] or {}).get("reason")),
        )
    else:
        first_action = NextAction(
            tool="pandrator_plan_workflow",
            arguments=final_plan_arguments,
            reason=(
                "Create the exact native workflow plan only after reviewing this "
                "procedural sequence and its current live state."
            ),
        )
    result = {
        "schema_version": "1",
        "plan_type": "model_orchestrated_workflow",
        "goal": arguments.goal,
        "session": _safe_session(session, arguments.session_id),
        "current_stage_statuses": statuses,
        "current_stages": statuses,
        "phase_order": [str(phase["stage"]) for phase in phases],
        "phases": phases,
        "next_action": first_action.model_dump(mode="json"),
        "immutability": (
            "This procedural plan is not an immutable execution snapshot. Create the "
            "exact native plan only after passive stages complete because their "
            "artifacts change the state fingerprint."
        ),
    }
    return ToolOutcome(result=result, next_actions=[first_action])


def plan_workflow(
    runtime: McpRuntime,
    arguments: PlanWorkflowInput,
) -> ToolOutcome:
    plan = runtime.require_application().create_workflow_plan(
        arguments.session_id,
        target_stage=arguments.target_stage,
        overrides=arguments.overrides,
        expires_in_minutes=arguments.expires_in_minutes,
    )
    plan_id = str(plan.get("plan_id") or "")
    digest = str(plan.get("plan_digest") or "")
    confirmations = tuple(str(value) for value in plan.get("required_confirmations", []) or [])
    warnings = [
        WarningMessage(code="prerequisite_rerun", message=str(value))
        for value in (plan.get("warnings") or [])
        if str(value).strip()
    ]
    return ToolOutcome(
        result=plan,
        warnings=warnings,
        next_actions=[
            NextAction(
                tool="pandrator_execute_workflow_plan",
                arguments={
                    "plan_id": plan_id,
                    "plan_digest": digest,
                    "accepted_confirmations": list(confirmations),
                    "idempotency_key": f"workflow-plan:{plan_id}",
                },
                reason=(
                    "Execute only after the user reviews the exact stages, "
                    "provider disclosures, data categories, and confirmations."
                ),
            )
        ],
    )


def execute_workflow_plan(
    runtime: McpRuntime,
    arguments: ExecuteWorkflowPlanInput,
) -> ToolOutcome:
    result = runtime.require_application().execute_workflow_plan(
        arguments.plan_id,
        plan_digest=arguments.plan_digest,
        accepted_confirmations=arguments.accepted_confirmations,
        idempotency_key=arguments.idempotency_key,
    )
    work = application_work_reference(result)
    return ToolOutcome(
        result=result,
        work=work,
        next_actions=[
            NextAction(
                tool="pandrator_get_work",
                arguments={
                    "work_type": "job",
                    "work_id": work.id,
                    "include_events": True,
                },
                reason="Observe the durable job by its returned work handle.",
            )
        ],
    )
