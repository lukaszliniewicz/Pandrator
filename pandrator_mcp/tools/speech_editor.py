"""Portable MCP tools for previewing and applying speech selection batches."""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Annotated, Any, Literal

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.speech_editor import (
    ApplySpeechSelectionBatchInput,
    PreviewSpeechSelectionBatchInput,
)


def _derived_idempotency_key(prefix: str, identity: Any) -> str:
    serialized = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{prefix}:" + hashlib.sha256(serialized).hexdigest()


def preview_speech_selection_batch(
    runtime: McpRuntime,
    arguments: PreviewSpeechSelectionBatchInput,
) -> ToolOutcome:
    """Compile an atomic set of original-codepoint speech edits without applying it."""

    changes = [
        change.model_dump(mode="json", exclude_unset=True)
        for change in arguments.changes
    ]
    body = {"changes": changes, "detailed_preview": arguments.detailed_preview}
    result = runtime.require_application().preview_speech_selection_batch(
        arguments.session_id, body=body
    )
    preview_revision = result.get("preview_revision")
    next_actions: list[NextAction] = []
    if isinstance(preview_revision, str) and len(preview_revision) == 64:
        apply_key = _derived_idempotency_key(
            "speech-selection-batch-apply",
            [arguments.session_id, preview_revision, changes],
        )
        next_actions.append(
            NextAction(
                tool="pandrator_apply_speech_selection_batch",
                arguments={
                    "session_id": arguments.session_id,
                    "changes": changes,
                    "expected_preview_revision": preview_revision,
                    "idempotency_key": apply_key,
                },
                reason=(
                    "Apply these exact codepoint selections only after reviewing the batch preview."
                ),
            )
        )
    return ToolOutcome(result=result, next_actions=next_actions)


def apply_speech_selection_batch(
    runtime: McpRuntime,
    arguments: ApplySpeechSelectionBatchInput,
) -> ToolOutcome:
    """Apply a reviewed batch and offer targeted regeneration with current settings."""

    body = arguments.model_dump(
        mode="json",
        exclude_unset=True,
        exclude={"session_id", "idempotency_key"},
    )
    # Re-dump child objects explicitly: an explicit null must stay distinct from
    # an omitted nested patch field (voice/delivery clears are meaningful).
    body["changes"] = [
        change.model_dump(mode="json", exclude_unset=True)
        for change in arguments.changes
    ]
    result = runtime.require_application().apply_speech_selection_batch(
        arguments.session_id,
        body=body,
        idempotency_key=arguments.idempotency_key,
    )
    raw_segment_ids = result.get("affected_segment_ids")
    segment_ids = (
        [value for value in raw_segment_ids if isinstance(value, str) and value]
        if isinstance(raw_segment_ids, list)
        else []
    )
    if not segment_ids:
        segment_ids = [change.segment_id for change in arguments.changes]
    segment_ids = list(dict.fromkeys(segment_ids))
    generation_key = _derived_idempotency_key(
        "speech-selection-batch-regenerate",
        [arguments.session_id, arguments.expected_preview_revision, segment_ids],
    )
    return ToolOutcome(
        result=result,
        next_actions=[
            NextAction(
                tool="pandrator_regenerate_segments",
                arguments={
                    "session_id": arguments.session_id,
                    "segment_ids": segment_ids,
                    "idempotency_key": generation_key,
                    "settings_source": "current",
                },
                reason=(
                    "Regenerate only the changed segments using the current session settings."
                ),
            )
        ],
    )


def register_speech_editor_tools(
    server,
    runtime: McpRuntime,
    validated_call,
    response,
    *,
    read_only,
    write_action,
) -> None:
    """Register strict flat MCP signatures backed by the portable input models."""

    def make_tool(handler, model, name: str, title: str, annotation, description: str):
        def invoke(**values) -> dict[str, Any]:
            response_mode = values.pop("response_mode", "standard")
            envelope = validated_call(handler, runtime, model, values)
            return response(envelope, response_mode)

        parameters = []
        annotations_map: dict[str, Any] = {"return": dict[str, Any]}
        for field_name, field in model.model_fields.items():
            field_annotation = Annotated[field.annotation, field]
            annotations_map[field_name] = field_annotation
            parameters.append(
                inspect.Parameter(
                    field_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=field_annotation,
                    default=(
                        inspect.Parameter.empty
                        if field.is_required()
                        else field.default
                    ),
                )
            )
        response_annotation = Literal["standard", "structured"]
        annotations_map["response_mode"] = response_annotation
        parameters.append(
            inspect.Parameter(
                "response_mode",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=response_annotation,
                default="standard",
            )
        )
        invoke.__name__ = name
        invoke.__doc__ = description
        invoke.__annotations__ = annotations_map
        invoke.__signature__ = inspect.Signature(
            parameters, return_annotation=dict[str, Any]
        )
        server.tool(name=name, title=title, annotations=annotation)(invoke)

    make_tool(
        preview_speech_selection_batch,
        PreviewSpeechSelectionBatchInput,
        "pandrator_preview_speech_selection_batch",
        "Preview a speech selection batch",
        read_only,
        "Preview ordered codepoint speech edits together; this does not adopt edits, generate audio, or call a provider.",
    )
    make_tool(
        apply_speech_selection_batch,
        ApplySpeechSelectionBatchInput,
        "pandrator_apply_speech_selection_batch",
        "Apply a speech selection batch",
        write_action,
        "Apply a reviewed speech selection batch using its preview revision and idempotency key.",
    )


__all__ = [
    "apply_speech_selection_batch",
    "preview_speech_selection_batch",
    "register_speech_editor_tools",
]
