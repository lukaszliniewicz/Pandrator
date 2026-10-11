"""MCP registration and reusable-sound library handlers."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from copy import copy
from typing import Annotated, Any

from pydantic.experimental.missing_sentinel import MISSING

from ..clients.application_sounds import MAX_AUDIO_IMPORT_BYTES
from ..context import McpRuntime
from ..results import ToolOutcome
from ..schemas.sounds import (
    DEFAULT_SOUND_MODEL_ID,
    GenerateSoundInput,
    ImportAudioReferenceInput,
    ProjectSoundsInput,
    SoundCatalogueInput,
    SoundLibraryInput,
)
from .e2e import _named_source_root, _open_contained_file, _relative_parts
from .project_sounds import (
    _safe_project_cue,
    _safe_project_job_receipt,
    _safe_project_reply,
    project_sounds,
)
from .sound_audio_import import _read_approved_audio, import_audio_reference
from .sound_results import (
    _application,
    _mapping,
    _safe_candidate,
    _safe_catalogue,
    _safe_generation_result,
    _safe_provenance,
    _safe_sound_detail,
    _safe_sound_item,
    _safe_sound_page,
    _safe_variant,
)


def sound_catalogue(runtime: McpRuntime, arguments: SoundCatalogueInput) -> dict[str, Any]:
    catalogue = _safe_catalogue(_application(runtime).sound_catalogue())
    if arguments.model_id is not None or arguments.kind is not None:
        models = catalogue.get("models")
        if isinstance(models, list):
            catalogue["models"] = [
                model
                for model in models
                if (arguments.model_id is None or model.get("id") == arguments.model_id)
                and (arguments.kind is None or arguments.kind in model.get("kinds", []))
            ]
    if arguments.model_id is not None:
        unavailable = catalogue.get("unavailable_models")
        if isinstance(unavailable, list):
            catalogue["unavailable_models"] = [
                model for model in unavailable if model.get("id") == arguments.model_id
            ]
    return catalogue


def generate_sound(runtime: McpRuntime, arguments: GenerateSoundInput) -> ToolOutcome:
    duration_seconds = arguments.duration_seconds
    if duration_seconds is None and arguments.model_id == DEFAULT_SOUND_MODEL_ID:
        duration_seconds = 4.0
    payload = _application(runtime).generate_sound(
        prompt=arguments.prompt,
        model_id=arguments.model_id,
        candidate_count=arguments.candidate_count,
        duration_seconds=duration_seconds,
        kind=arguments.kind,
        backend=arguments.backend,
        lyrics=arguments.lyrics,
        parameters=arguments.parameters,
        seed=arguments.seed,
        threads=arguments.threads,
        idempotency_key=arguments.idempotency_key,
    )
    result, outcome = _safe_generation_result(payload)
    return outcome or ToolOutcome(result=result)


def sound_library(
    runtime: McpRuntime, arguments: SoundLibraryInput
) -> ToolOutcome | dict[str, Any]:
    application = _application(runtime)
    action = arguments.action
    if action == "list":
        result = application.list_sounds(
            query=arguments.query,
            tag=arguments.tag,
            cursor=arguments.cursor,
            limit=arguments.limit,
            archived=arguments.archived,
        )
        return _safe_sound_page(result)
    if action == "get":
        limit = arguments.limit if "limit" in arguments.model_fields_set else 20
        result = application.get_sound(
            arguments.item_id or "",
            cursor=arguments.cursor,
            limit=limit,
        )
        return _safe_sound_detail(result, include_detail=arguments.include_detail)
    if action == "candidates":
        limit = arguments.limit if "limit" in arguments.model_fields_set else 20
        result = application.list_sound_candidates(
            cursor=arguments.cursor,
            limit=limit,
        )
        return _safe_sound_page(result, candidates=True)
    if action == "candidate":
        result = application.get_sound_candidate(arguments.candidate_id or "")
        return _safe_candidate(result, include_detail=arguments.include_detail)
    if action == "save":
        result = application.save_sound(
            candidate_id=arguments.candidate_id or "",
            name=arguments.name,
            tags=arguments.tags,
            item_id=arguments.item_id,
            expected_revision=arguments.expected_revision,
            idempotency_key=arguments.idempotency_key or "",
        )
        return ToolOutcome(
            result={
                "item_id": result.get("item_id"),
                "item": _safe_sound_item(result.get("item")),
                "candidate": _safe_candidate(result.get("candidate")),
                "reused": result.get("reused") is True,
            }
        )
    if action == "update":
        assert arguments.changes is not None
        result = application.update_sound(
            arguments.item_id or "",
            expected_revision=arguments.expected_revision or 0,
            changes=arguments.changes.model_dump(mode="json", exclude_unset=True),
            idempotency_key=arguments.idempotency_key or "",
        )
        return ToolOutcome(result=_safe_sound_item(result))
    if action == "discard":
        result = application.discard_sound_candidate(
            arguments.candidate_id or "",
            idempotency_key=arguments.idempotency_key or "",
        )
        return ToolOutcome(result=_safe_candidate(result))
    raise ValueError("Unknown sound-library action.")


def register_sound_tools(
    server: Any,
    runtime: McpRuntime,
    validated_call: Callable[..., dict[str, Any]],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    """Register typed sound tools using the shared validated-call envelope."""

    registrations = (
        (
            "pandrator_sound_catalogue",
            "Sound and music catalogue",
            sound_catalogue,
            SoundCatalogueInput,
            read_only,
        ),
        (
            "pandrator_generate_sound",
            "Generate sound or music",
            generate_sound,
            GenerateSoundInput,
            write_action,
        ),
        (
            "pandrator_sound_library",
            "Sound and music library",
            sound_library,
            SoundLibraryInput,
            write_action,
        ),
        (
            "pandrator_import_audio_reference",
            "Import audio reference",
            import_audio_reference,
            ImportAudioReferenceInput,
            write_action,
        ),
        (
            "pandrator_project_sounds",
            "Project sounds",
            project_sounds,
            ProjectSoundsInput,
            write_action,
        ),
    )
    for name, title, handler, model, tool_annotations in registrations:

        def invoke(*, _handler=handler, _model=model, **values) -> dict[str, Any]:
            return validated_call(
                _handler, runtime, _model,
                {key: value for key, value in values.items() if value is not MISSING},
            )

        parameters: list[inspect.Parameter] = []
        signature_annotations: dict[str, Any] = {"return": dict[str, Any]}
        for field_name, field in model.model_fields.items():
            optional = not field.is_required()
            signature_field = copy(field)
            if optional:
                signature_field.default = MISSING
            annotation = Annotated[
                field.annotation | MISSING if optional else field.annotation,
                signature_field,
            ]
            signature_annotations[field_name] = annotation
            parameters.append(
                inspect.Parameter(
                    field_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=MISSING if optional else inspect.Parameter.empty,
                )
            )
        invoke.__name__ = name
        invoke.__doc__ = title
        invoke.__annotations__ = signature_annotations
        invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])
        server.tool(name=name, title=title, annotations=tool_annotations)(invoke)


__all__ = [
    "MAX_AUDIO_IMPORT_BYTES",
    "_application",
    "_mapping",
    "_named_source_root",
    "_open_contained_file",
    "_read_approved_audio",
    "_relative_parts",
    "_safe_candidate",
    "_safe_catalogue",
    "_safe_generation_result",
    "_safe_provenance",
    "_safe_project_cue",
    "_safe_project_job_receipt",
    "_safe_project_reply",
    "_safe_sound_detail",
    "_safe_sound_item",
    "_safe_sound_page",
    "_safe_variant",
    "generate_sound",
    "import_audio_reference",
    "project_sounds",
    "register_sound_tools",
    "sound_catalogue",
    "sound_library",
]
