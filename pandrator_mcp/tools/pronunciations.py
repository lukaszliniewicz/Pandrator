"""Typed MCP tools for the reusable pronunciation dictionary."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Annotated, Any, Literal, get_args

from ..context import McpRuntime
from ..native_enums import native_nullable_enum
from ..native_text import NativeNullableString
from ..results import ToolOutcome
from ..schemas.pronunciations import (
    CreatePronunciationInput,
    DeletePronunciationInput,
    ListPronunciationsInput,
    UpdatePronunciationInput,
)

_PUBLIC_ENTRY_FIELDS = (
    "id",
    "scope",
    "session_id",
    "session_name",
    "source_form",
    "language",
    "phonetic",
    "alphabet",
    "backend",
    "status",
    "source",
    "notes",
    "revision",
    "created_at",
    "updated_at",
)
_MUTATION_EFFECTS = {
    "existing_recordings_modified": False,
    "manual_speech_overrides_preserved": True,
    "requires_speech_preparation_for_existing_audio": True,
}
_MUTATION_DESCRIPTION = (
    "Edit the reusable pronunciation dictionary. Existing audio and manual speech "
    "overrides stay unchanged; re-run speech preparation, then refresh changed audio "
    "to apply it to existing recordings."
)


def _public_entry(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {field: payload[field] for field in _PUBLIC_ENTRY_FIELDS if field in payload}


def list_pronunciations(runtime: McpRuntime, arguments: ListPronunciationsInput) -> ToolOutcome:
    request = arguments.model_dump(mode="json", exclude_none=True)
    payload = runtime.require_application().pronunciation_request("list", request)
    items = payload.get("items")
    if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
        raise ValueError("The pronunciation API returned an invalid list response.")
    return ToolOutcome(result={"items": [_public_entry(item) for item in items]})


def create_pronunciation(runtime: McpRuntime, arguments: CreatePronunciationInput) -> ToolOutcome:
    request = arguments.model_dump(mode="json", exclude_unset=True)
    payload = runtime.require_application().pronunciation_request("create", request)
    if not isinstance(payload, Mapping):
        raise ValueError("The pronunciation API returned an invalid entry response.")
    return ToolOutcome(result={**_public_entry(payload), "effects": dict(_MUTATION_EFFECTS)})


def update_pronunciation(runtime: McpRuntime, arguments: UpdatePronunciationInput) -> ToolOutcome:
    request = {
        "entry_id": arguments.entry_id,
        "expected_revision": arguments.expected_revision,
        "changes": arguments.changes.model_dump(mode="json", exclude_unset=True),
    }
    payload = runtime.require_application().pronunciation_request("update", request)
    if not isinstance(payload, Mapping):
        raise ValueError("The pronunciation API returned an invalid entry response.")
    return ToolOutcome(result={**_public_entry(payload), "effects": dict(_MUTATION_EFFECTS)})


def delete_pronunciation(runtime: McpRuntime, arguments: DeletePronunciationInput) -> ToolOutcome:
    runtime.require_application().pronunciation_request(
        "delete",
        {
            "entry_id": arguments.entry_id,
            "expected_revision": arguments.expected_revision,
        },
    )
    return ToolOutcome(
        result={
            "id": arguments.entry_id,
            "deleted": True,
            "effects": dict(_MUTATION_EFFECTS),
        }
    )


def register_pronunciation_tools(
    server,
    runtime: McpRuntime,
    validated_call,
    response,
    *,
    read_only,
    revisioned_write_action,
    destructive_action,
) -> None:
    """Register four flat, typed tools for pronunciation dictionary CRUD."""

    def register(model, handler, *, name, title, description, annotations) -> None:
        def invoke(**values) -> dict[str, Any]:
            response_mode = values.pop("response_mode", "standard")
            if response_mode not in {"standard", "structured"}:
                raise ValueError("response_mode must be standard or structured.")
            envelope = validated_call(handler, runtime, model, values)
            return response(envelope, response_mode)

        parameters = []
        annotations_map: dict[str, Any] = {"return": dict[str, Any]}
        for field_name, field in model.model_fields.items():
            field_annotation = field.annotation
            field_args = get_args(field_annotation)
            if type(None) in field_args:
                non_nullable = tuple(item for item in field_args if item is not type(None))
                if len(non_nullable) == 1 and non_nullable[0] is str:
                    field_annotation = NativeNullableString
                elif len(non_nullable) == 1:
                    field_annotation = native_nullable_enum(field_annotation)
            annotation = (
                field_annotation
                if field.default_factory is not None
                else Annotated[field_annotation, field]
            )
            annotations_map[field_name] = annotation
            default = field.default
            if field.default_factory is not None:
                default = field.default_factory()
            parameters.append(
                inspect.Parameter(
                    field_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=inspect.Parameter.empty if field.is_required() else default,
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
        invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])
        server.tool(name=name, title=title, annotations=annotations)(invoke)

    register(
        ListPronunciationsInput,
        list_pronunciations,
        name="pandrator_list_pronunciations",
        title="List pronunciation entries",
        description="Find reusable respellings by text, language, scope and review status.",
        annotations=read_only,
    )
    register(
        CreatePronunciationInput,
        create_pronunciation,
        name="pandrator_create_pronunciation",
        title="Create pronunciation entry",
        description=_MUTATION_DESCRIPTION,
        annotations=revisioned_write_action,
    )
    register(
        UpdatePronunciationInput,
        update_pronunciation,
        name="pandrator_update_pronunciation",
        title="Update pronunciation entry",
        description=_MUTATION_DESCRIPTION,
        annotations=revisioned_write_action,
    )
    register(
        DeletePronunciationInput,
        delete_pronunciation,
        name="pandrator_delete_pronunciation",
        title="Delete pronunciation entry",
        description=_MUTATION_DESCRIPTION,
        annotations=destructive_action,
    )


__all__ = [
    "create_pronunciation",
    "delete_pronunciation",
    "list_pronunciations",
    "register_pronunciation_tools",
    "update_pronunciation",
]
