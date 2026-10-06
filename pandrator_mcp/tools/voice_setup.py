"""Portable MCP tools for a session's single- or multi-voice mode."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from typing import Annotated, Any

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..schemas.voice_setup import (
    ConfigureVoiceSetupInput,
    GetVoiceSetupInput,
    SetupDesignedVoiceInput,
)
from ..work_mapping import application_work_reference


def get_voice_setup(
    runtime: McpRuntime,
    arguments: GetVoiceSetupInput,
) -> ToolOutcome:
    return ToolOutcome(
        result=runtime.require_application().get_voice_setup(arguments.session_id)
    )


def configure_voice_setup(
    runtime: McpRuntime,
    arguments: ConfigureVoiceSetupInput,
) -> ToolOutcome:
    result = runtime.require_application().configure_voice_setup(
        arguments.session_id,
        expected_revision=arguments.expected_revision,
        mode=arguments.mode,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(result=result)


def _setup_stage_key(arguments: SetupDesignedVoiceInput, stage: str) -> str:
    digest = hashlib.sha256(f"{arguments.idempotency_key}:{stage}".encode("utf-8")).hexdigest()
    return f"voice-setup:{stage}:{digest}"


def _setup_signature(arguments: SetupDesignedVoiceInput) -> str:
    immutable = json.dumps(
        arguments.model_dump(mode="json", exclude={"idempotency_key"}), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False,
    )
    digest = hashlib.sha256(immutable.encode("utf-8")).hexdigest()
    return digest


def _setup_resume(arguments: SetupDesignedVoiceInput) -> NextAction:
    return NextAction(
        tool="pandrator_setup_designed_voice",
        arguments=arguments.model_dump(mode="json"),
        reason="Resume the same durable voice setup with its original arguments and key.",
    )


def _setup_work(application: Any, mutation: dict[str, Any], stage: str,
                arguments: SetupDesignedVoiceInput) -> tuple[dict[str, Any] | None, ToolOutcome | None]:
    job_id = str(mutation.get("work_id") or mutation.get("job_id") or mutation.get("id") or "")
    if not job_id:
        raise PandratorMcpError("downstream_unavailable", "Voice setup returned no durable work ID.")
    durable = application.get_work(job_id)
    work = application_work_reference(durable)
    if work.state in {"failed", "cancelled"}:
        error = durable.get("error") or {}
        return None, ToolOutcome(
            result={
                "stage": stage, "status": work.state, "voice_id": arguments.voice_id,
                "error": {key: error.get(key) for key in ("code", "message")}
                if isinstance(error, dict) else None,
            },
            work=work,
            next_actions=[NextAction(
                tool="pandrator_get_work_log",
                arguments={"work_id": job_id, "work_type": "job"},
                reason="Inspect the failed voice setup stage before taking further action.",
            )],
        )
    if work.state != "succeeded":
        return None, ToolOutcome(
            result={"stage": stage, "status": work.state, "voice_id": arguments.voice_id},
            work=work, next_actions=[_setup_resume(arguments)],
        )
    result = durable.get("result_summary")
    if not isinstance(result, dict):
        raise PandratorMcpError("downstream_unavailable", "Completed voice setup stage has no receipt.")
    return result, None


def setup_designed_voice(runtime: McpRuntime, arguments: SetupDesignedVoiceInput) -> ToolOutcome:
    """Replay two durable, separately idempotent stages around one reviewed design."""
    application = runtime.require_application()
    try:
        preparation = application.promote_voice_design(
            arguments.voice_id, artifact_id=arguments.artifact_id,
            transcript=arguments.transcript, language=arguments.language,
            expected_voice_revision=arguments.expected_voice_revision,
            idempotency_key=_setup_stage_key(arguments, "prepare"),
            recipe_signature=_setup_signature(arguments),
        )
        if preparation.get("status") != "ready":
            preparation, outcome = _setup_work(
                application, preparation, "preparing_reference", arguments,
            )
            if outcome is not None:
                return outcome
        assert preparation is not None
        sample_id = str(preparation.get("sample_id") or "")
        artifact_id = str(preparation.get("artifact_id") or "")
        revision = preparation.get("voice_revision")
        if not sample_id or not artifact_id or type(revision) is not int or revision < 1:
            raise PandratorMcpError("downstream_unavailable", "Prepared voice reference has no exact receipt.")
        # The durable preparation hash is the pin. A mutable sample read can
        # confirm/expose it, but must never silently replace that original pin.
        sample_hash = str(preparation.get("sample_sha256") or "")
        if not sample_hash:
            samples = application.get_voice_samples(arguments.voice_id)
            sample = next((item for item in samples.get("items", [])
                           if isinstance(item, dict) and item.get("id") == sample_id), None)
            if isinstance(sample, dict) and sample.get("artifact_id") == artifact_id:
                sample_hash = str(sample.get("sample_sha256") or "")
        if re.fullmatch(r"[a-fA-F0-9]{64}", sample_hash) is None:
            raise PandratorMcpError("downstream_unavailable", "Prepared voice reference has no SHA256 pin.")
        publication = application.publish_voice(
            arguments.voice_id, arguments.service_id,
            expected_revision=revision, sample_id=sample_id, sample_sha256=sample_hash,
            idempotency_key=_setup_stage_key(arguments, "publish"),
            recipe_signature=_setup_signature(arguments),
        )
        receipt, outcome = _setup_work(application, publication, "publishing", arguments)
        if outcome is not None:
            return outcome
        assert receipt is not None
        if not receipt.get("provider_voice_id") or type(receipt.get("voice_revision")) is not int:
            raise PandratorMcpError("downstream_unavailable", "Completed voice publication has no registration receipt.")
        return ToolOutcome(result={
            "stage": "ready", "voice_id": arguments.voice_id,
            "service_id": arguments.service_id, "sample_id": sample_id,
            "artifact_id": artifact_id, "sample_sha256": sample_hash,
            "reused_reference": bool(preparation.get("reused_reference")),
            **{key: receipt[key] for key in (
                "provider_voice_id", "voice_revision", "linked", "reused_registration",
            ) if key in receipt},
        })
    except PandratorMcpError as error:
        if error.retryable or error.code in {"application_response_timeout", "application_unavailable"}:
            error.next_actions.append(_setup_resume(arguments))
        raise


def _register_tool(server, runtime, validated_call, *, model, handler, name, title,
                   description, annotations) -> None:
    def invoke(**values) -> dict[str, Any]:
        return validated_call(handler, runtime, model, values)

    parameters = []
    annotations_map: dict[str, object] = {"return": dict[str, Any]}
    for field_name, field in model.model_fields.items():
        annotations_map[field_name] = Annotated[field.annotation, field]
        parameters.append(
            inspect.Parameter(
                field_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotations_map[field_name],
                default=(
                    inspect.Parameter.empty
                    if field.is_required()
                    else field.default
                ),
            )
        )
    invoke.__name__ = name
    invoke.__doc__ = description
    invoke.__annotations__ = annotations_map
    invoke.__signature__ = inspect.Signature(
        parameters,
        return_annotation=dict[str, Any],
    )
    server.tool(name=name, title=title, annotations=annotations)(invoke)


def register_voice_setup_tools(
    server,
    runtime: McpRuntime,
    validated_call,
    *,
    read_only,
    write_action,
) -> None:
    """Register flat, typed voice-setup tools backed by the HTTP client."""

    _register_tool(
        server,
        runtime,
        validated_call,
        model=GetVoiceSetupInput,
        handler=get_voice_setup,
        name="pandrator_get_voice_setup",
        title="Inspect voice setup",
        description="Inspect the session's current single- or multi-voice mode.",
        annotations=read_only,
    )
    _register_tool(
        server,
        runtime,
        validated_call,
        model=ConfigureVoiceSetupInput,
        handler=configure_voice_setup,
        name="pandrator_configure_voice_setup",
        title="Configure voice setup",
        description=(
            "Set the session's single- or multi-voice mode using its current "
            "64-character revision and an idempotency key. This does not start work."
        ),
        annotations=write_action,
    )
    _register_tool(
        server, runtime, validated_call, model=SetupDesignedVoiceInput,
        handler=setup_designed_voice, name="pandrator_setup_designed_voice",
        title="Set up a reviewed designed voice",
        description=(
            "Prepare and publish an explicitly reviewed managed voice-design preview. "
            "Resume using the identical original arguments and idempotency key. "
            "Design/audition and sample review must be completed before this call."
        ),
        annotations=write_action,
    )


__all__ = [
    "configure_voice_setup",
    "get_voice_setup",
    "register_voice_setup_tools",
    "setup_designed_voice",
]
