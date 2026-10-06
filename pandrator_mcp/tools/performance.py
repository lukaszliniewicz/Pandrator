"""Portable performance tools using the same authoritative target API as the UI."""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Annotated, Any, Literal

from ..compact_packets import compact_manifest_packet
from ..context import McpRuntime
from ..errors import NextAction
from ..native_enums import native_nullable_enum
from ..native_text import NativeNullableString
from ..performance_actions import PERFORMANCE_ACTIONS
from ..results import ToolOutcome
from ..schemas import performance as schemas
from ..work_mapping import application_work_reference


def performance_action(runtime: McpRuntime, action: str, arguments) -> ToolOutcome:
    values = arguments.model_dump(mode="json", exclude_none=True)
    for field in ("packet_format", "known_manifest_hash", "include_units"):
        values.pop(field, None)
    payload = runtime.require_application().performance_plan_request(action, values)
    projected = dict(payload)
    if action in {"create", "get"} and not arguments.include_units:
        for field in ("items", "offset", "limit", "filter"):
            projected.pop(field, None)
    if action == "claim" and arguments.packet_format == "compact":
        batch = payload.get("batch")
        if isinstance(batch, dict):
            manifest = {key: value for key, value in batch.items() if key != "items"}
            projected = compact_manifest_packet(
                projected, manifest, known_manifest_hash=arguments.known_manifest_hash
            )
            projected["batch"] = {"items": batch["items"]} if "items" in batch else {}
    next_actions = []
    plan_id = payload.get("id") or payload.get("plan_id") or getattr(arguments, "plan_id", None)
    if (action == "create" and arguments.mode == "passive") or action == "submit":
        claim_identity = json.dumps(
            [arguments.session_id, plan_id, arguments.idempotency_key],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        next_actions.append(
            NextAction(
                tool="pandrator_claim_performance_batch",
                arguments={
                    "session_id": arguments.session_id, "plan_id": plan_id,
                    "idempotency_key": "performance-claim:" + hashlib.sha256(claim_identity).hexdigest(),
                },
                reason="Claim a bounded batch. The target supplies the selected XML or pSSML contract, read-only context and immutable actionable text.",
            )
        )
    elif (
        action in {"create", "edit", "analyse"}
        or (action == "claim" and payload.get("batch") is None and payload.get("complete") is True)
    ) and plan_id:
        next_actions.append(
            NextAction(
                tool="pandrator_get_performance_plan",
                arguments={"session_id": arguments.session_id, "plan_id": plan_id,
                           "include_units": True},
                reason="Inspect the completed annotations and current version before previewing or adopting them.",
            )
        )
    work = None
    if payload.get("job_id") and action in {"create", "analyse"}:
        work = application_work_reference({"job_id": payload["job_id"], "state": "queued"})
    return ToolOutcome(
        result={"schema_version": "1", **projected}, work=work, next_actions=next_actions
    )


def register_performance_tools(
    server, runtime: McpRuntime, validated_call, response, *, read_only, write_action
) -> None:
    """Generate flat, typed signatures from the same models the tools validate.

    No eval or arbitrary call routing: actions come from the static manifest.
    This avoids eleven hand-maintained copies of each field's MCP constraints.
    """

    def make_tool(action, model, name, title):
        def handle(current_runtime, arguments):
            return performance_action(current_runtime, action, arguments)

        def invoke(**values) -> dict[str, Any]:
            response_mode = (
                values.pop("response_mode", "standard")
                if action in {"claim", "create", "get"}
                else "standard"
            )
            envelope = validated_call(handle, runtime, model, values)
            if action in {"claim", "create", "get"}:
                return response(envelope, response_mode)
            return envelope

        parameters = []
        annotations: dict[str, object] = {"return": dict[str, Any]}
        for field_name, field in model.model_fields.items():
            field_annotation = field.annotation
            if model is schemas.PreviewPerformancePlanInput and field_name == "context_mode":
                field_annotation = native_nullable_enum(field_annotation)
            elif field_name == "known_manifest_hash":
                field_annotation = NativeNullableString
            annotation = Annotated[field_annotation, field]
            annotations[field_name] = annotation
            parameters.append(
                inspect.Parameter(
                    field_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=inspect.Parameter.empty if field.is_required() else field.default,
                )
            )
        if action in {"claim", "create", "get"}:
            annotation = Literal["standard", "structured"]
            annotations["response_mode"] = annotation
            parameters.append(
                inspect.Parameter(
                    "response_mode",
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default="standard",
                )
            )
        invoke.__name__ = name
        invoke.__doc__ = (
            title
            + ". Source/context content is read-only. Generation text and boundaries never change. Adopt only reviewed saved annotations."
        )
        invoke.__annotations__ = annotations
        invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])
        return invoke

    for (
        action,
        name,
        title,
        model_name,
        risk,
        _scope,
        _operation,
        _method,
        _suffix,
    ) in PERFORMANCE_ACTIONS:
        model = getattr(schemas, model_name)
        server.tool(
            name=name, title=title, annotations=read_only if risk == "read" else write_action
        )(make_tool(action, model, name, title))
