"""MCP handlers for the normalized voice catalog and managed voice lifecycle."""

from __future__ import annotations

import inspect
from typing import Annotated, Any, Callable

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..schemas import VoiceCatalogInput
from ..schemas.voice_lifecycle import (
    AuditionVoiceInput,
    CreateVoiceCollectionInput,
    GetVoiceSamplesInput,
    ImportVoiceReferenceInput,
    ListVoiceCollectionsInput,
    PromoteVoiceDesignInput,
    PublishVoiceInput,
    ReviewVoiceTranscriptInput,
    TranscribeVoiceSampleInput,
    UpdateCatalogVoiceMetadataInput,
    UpdateVoiceCollectionInput,
    VoiceCatalogCapabilitiesInput,
    VoiceCreateInput,
)
from ..work_mapping import application_work_reference

_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "canceled"})


def _safe_reference(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("kind", "voice_id", "service_id", "model", "voice"):
        if key in value and value[key] is not None:
            result[key] = value[key]
    return result or None


def _safe_profile(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def safe_voice_projection(value: Any) -> dict[str, Any]:
    """Project one backend catalog item without arbitrary provider metadata."""

    item = value if isinstance(value, dict) else {}
    profile = _safe_profile(item.get("profile"))
    return {
        key: item.get(key)
        for key in (
            "key",
            "id",
            "kind",
            "name",
            "description",
            "language",
            "voice_category",
            "origin",
            "revision",
            "collections",
            "compatibility",
            "sample_count",
            "created_at",
            "updated_at",
            "bundled",
            "match_reasons",
        )
        if key in item
    } | {
        "reference": _safe_reference(item.get("reference")),
        "profile": profile,
        "evidence": item.get("evidence")
        if isinstance(item.get("evidence"), dict)
        else profile.get("evidence", {}),
        "safe_artifact_id": item.get("safe_artifact_id") or item.get("preview_artifact_id"),
    }


def safe_samples_projection(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in (
        "id",
        "voice_id",
        "status",
        "state",
        "artifact_id",
        "duration_ms",
        "transcript",
        "language",
        "transcript_reviewed",
        "created_at",
        "updated_at",
        "revision",
    ):
        if key in payload:
            result[key] = payload.get(key)
    if "voice_revision" in payload:
        result["voice_revision"] = payload.get("voice_revision")
    items = payload.get("items")
    result["items"] = []
    if isinstance(items, list):
        for raw in items:
            if not isinstance(raw, dict):
                continue
            result["items"].append(safe_sample_projection(raw))
    return result


def safe_sample_projection(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        key: item.get(key)
        for key in (
            "id",
            "voice_id",
            "status",
            "state",
            "artifact_id",
            "duration_ms",
            "transcript",
            "transcript_language",
            "file_status",
            "available",
            "language",
            "transcript_reviewed",
            "created_at",
            "updated_at",
            "revision",
            "voice_revision",
        )
        if key in item
    }


def safe_collection_projection(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    result = {
        key: item.get(key)
        for key in ("id", "name", "description", "revision", "created_at", "updated_at")
        if key in item
    }
    members: list[dict[str, Any]] = []
    for raw in item.get("members") or []:
        if not isinstance(raw, dict):
            continue
        member = {"key": raw.get("key"), "reference": _safe_reference(raw.get("reference"))}
        if member["key"] is not None or member["reference"] is not None:
            members.append(member)
    result["members"] = members
    if "member_count" in item:
        result["member_count"] = item.get("member_count")
    return result


def _safe_job(value: Any) -> dict[str, Any]:
    """Retain only work identity/status and safe lifecycle receipts."""

    item = value if isinstance(value, dict) else {}
    allowed = (
        "id",
        "job_id",
        "work_id",
        "state",
        "status",
        "progress",
        "detail",
        "cancellable",
        "poll_after_ms",
        "created_at",
        "updated_at",
        "finished_at",
        "error",
        "artifact_id",
        "sample_id",
        "voice_id",
        "voice_revision",
        "service_id",
        "model",
        "voice",
        "language",
        "transcript",
        "transcript_language",
        "word_timestamps_artifact_id",
    )
    result = {key: item.get(key) for key in allowed if key in item}
    error = result.get("error")
    if isinstance(error, dict):
        result["error"] = {
            key: str(error.get(key) or "")[:2_000]
            for key in ("code", "message")
            if error.get(key) is not None
        }
    elif "error" in result:
        result.pop("error", None)
    nested_result = item.get("result")
    if isinstance(nested_result, dict):
        for key in (
            "artifact_id",
            "sample_id",
            "voice_id",
            "voice_revision",
            "service_id",
            "model",
            "voice",
            "language",
            "transcript",
            "transcript_language",
            "word_timestamps_artifact_id",
        ):
            if key in nested_result and key not in result:
                result[key] = nested_result[key]
    transcript = result.get("transcript")
    transcript_language = result.get("transcript_language")
    if isinstance(transcript, str):
        result["transcript"] = transcript[:64_000]
    if isinstance(transcript_language, str):
        result["transcript_language"] = transcript_language[:40]
    return result


def _job_id(payload: dict[str, Any]) -> str:
    value = payload.get("work_id") or payload.get("job_id") or payload.get("id")
    job_id = str(value or "").strip()
    if not job_id:
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned durable work without an identifier.",
        )
    return job_id


def _job_outcome(
    runtime: McpRuntime,
    payload: dict[str, Any],
    *,
    next_actions: list[NextAction] | None = None,
) -> ToolOutcome:
    job_id = _job_id(payload)
    state = str(payload.get("state") or payload.get("status") or "").casefold()
    actions = list(next_actions or [])
    if state not in _TERMINAL_STATES and not any(
        action.tool == "pandrator_get_work" for action in actions
    ):
        actions.insert(
            0,
            NextAction(
                tool="pandrator_get_work",
                arguments={"work_id": job_id, "work_type": "job"},
                reason="Poll the durable voice operation until it reaches a terminal state.",
            ),
        )
    return ToolOutcome(
        result={"schema_version": "1", **_safe_job(payload)},
        work=application_work_reference(payload),
        next_actions=actions,
    )


def voice_catalog(runtime: McpRuntime, arguments: VoiceCatalogInput) -> dict[str, Any]:
    payload = runtime.require_application().voice_catalog(
        **arguments.model_dump(mode="json", exclude_none=True)
    )
    result = {
        key: payload.get(key)
        for key in (
            "schema_version",
            "catalog_revision",
            "total",
            "next_cursor",
            "facets",
            "taxonomy",
        )
        if key in payload
    }
    result["schema_version"] = str(result.get("schema_version") or "1")
    result["items"] = [
        safe_voice_projection(item)
        for item in (payload.get("items") or [])
        if isinstance(item, dict)
    ]
    collections = payload.get("collections")
    if isinstance(collections, list):
        result["collections"] = [
            {
                key: item.get(key)
                for key in ("id", "name", "revision", "member_count")
                if key in item
            }
            for item in collections
            if isinstance(item, dict)
        ]
    return result


def voice_catalog_capabilities(
    runtime: McpRuntime,
    arguments: VoiceCatalogCapabilitiesInput,
) -> dict[str, Any]:
    payload = runtime.require_application().voice_catalog_capabilities()
    requested_service = arguments.service_id.casefold() if arguments.service_id else None
    requested_model = arguments.model.casefold() if arguments.model else None
    result = {
        key: payload.get(key)
        for key in (
            "schema_version",
            "voice_profile_schema_version",
            "features",
            "markup",
        )
        if key in payload
    }
    models: list[dict[str, Any]] = []
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        if requested_service and str(item.get("service_id") or "").casefold() != requested_service:
            continue
        if requested_model and str(item.get("model") or "").casefold() != requested_model:
            continue
        models.append(
            {
                key: item.get(key)
                for key in (
                    "service_id",
                    "model",
                    "available",
                    "listed",
                    "availability_reason",
                    "modes",
                    "languages",
                    "license",
                    "usage_note",
                )
                if key in item
            }
        )
    result["models"] = models
    result.setdefault("schema_version", "1")
    return result


def create_voice(runtime: McpRuntime, arguments: VoiceCreateInput) -> ToolOutcome:
    application = runtime.require_application()
    result = application.create_voice(
        name=arguments.name,
        language=arguments.language,
        description=arguments.description,
        voice_category=arguments.voice_category,
        profile=arguments.profile,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(
        result=safe_voice_projection(result),
        next_actions=[
            NextAction(
                tool="pandrator_get_voice_catalog",
                arguments={},
                reason="Inspect the created managed voice in the normalized catalog.",
            )
        ],
    )


def get_voice_samples(runtime: McpRuntime, arguments: GetVoiceSamplesInput) -> dict[str, Any]:
    return safe_samples_projection(
        runtime.require_application().get_voice_samples(arguments.voice_id)
    )


def promote_voice_design(
    runtime: McpRuntime,
    arguments: PromoteVoiceDesignInput,
) -> ToolOutcome:
    result = runtime.require_application().promote_voice_design(
        arguments.voice_id,
        artifact_id=arguments.artifact_id,
        transcript=arguments.transcript,
        language=arguments.language,
        expected_voice_revision=arguments.expected_voice_revision,
        idempotency_key=arguments.idempotency_key,
    )
    return _job_outcome(
        runtime,
        result,
        next_actions=[
            NextAction(
                tool="pandrator_get_voice_samples",
                arguments={"voice_id": arguments.voice_id},
                reason="Inspect the normalized sample after the durable promotion completes.",
            ),
        ],
    )


def import_voice_reference(
    runtime: McpRuntime,
    arguments: ImportVoiceReferenceInput,
) -> ToolOutcome:
    result = runtime.require_application().import_voice_reference(
        arguments.voice_id,
        artifact_id=arguments.artifact_id,
        transcript=arguments.transcript,
        language=arguments.language,
        transcript_reviewed=arguments.transcript_reviewed,
        expected_voice_revision=arguments.expected_voice_revision,
        idempotency_key=arguments.idempotency_key,
    )
    return _job_outcome(
        runtime,
        result,
        next_actions=[
            NextAction(
                tool="pandrator_get_voice_samples",
                arguments={"voice_id": arguments.voice_id},
                reason="Inspect the imported managed sample after normalization completes.",
            ),
        ],
    )


def transcribe_voice_sample(
    runtime: McpRuntime,
    arguments: TranscribeVoiceSampleInput,
) -> ToolOutcome:
    result = runtime.require_application().transcribe_voice_sample(
        arguments.voice_id,
        arguments.sample_id,
        settings=arguments.settings,
        idempotency_key=arguments.idempotency_key,
    )
    return _job_outcome(
        runtime,
        result,
        next_actions=[
            NextAction(
                tool="pandrator_get_work",
                arguments={"work_id": _job_id(result), "work_type": "job"},
                reason="Read the bounded transcript and artifact IDs when transcription completes.",
            ),
            NextAction(
                tool="pandrator_get_voice_samples",
                arguments={"voice_id": arguments.voice_id},
                reason="Review the sample transcript after transcription completes.",
            ),
        ],
    )


def review_voice_transcript(
    runtime: McpRuntime,
    arguments: ReviewVoiceTranscriptInput,
) -> ToolOutcome:
    result = runtime.require_application().review_voice_transcript(
        arguments.voice_id,
        arguments.sample_id,
        transcript=arguments.transcript,
        language=arguments.language,
        expected_voice_revision=arguments.expected_voice_revision,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(
        result=safe_samples_projection(result),
        next_actions=[
            NextAction(
                tool="pandrator_get_voice_samples",
                arguments={"voice_id": arguments.voice_id},
                reason="Confirm the reviewed transcript and current voice revision.",
            )
        ],
    )


def publish_voice(runtime: McpRuntime, arguments: PublishVoiceInput) -> ToolOutcome:
    result = runtime.require_application().publish_voice(
        arguments.voice_id,
        arguments.service_id,
        expected_revision=arguments.expected_revision,
        idempotency_key=arguments.idempotency_key,
    )
    return _job_outcome(
        runtime,
        result,
        next_actions=[
            NextAction(
                tool="pandrator_get_voice_catalog",
                arguments={},
                reason="Inspect provider compatibility after publication completes.",
            )
        ],
    )


def audition_voice(runtime: McpRuntime, arguments: AuditionVoiceInput) -> ToolOutcome:
    result = runtime.require_application().audition_voice(
        service_id=arguments.service_id,
        text=arguments.text,
        model=arguments.model,
        voice=arguments.voice,
        language=arguments.language,
        generation_prompt=arguments.generation_prompt,
        seed=arguments.seed,
        idempotency_key=arguments.idempotency_key,
    )
    return _job_outcome(runtime, result)


def list_voice_collections(
    runtime: McpRuntime,
    arguments: ListVoiceCollectionsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_voice_collections()
    return {
        "schema_version": str(payload.get("schema_version") or "1"),
        "items": [
            safe_collection_projection(item)
            for item in (payload.get("items") or [])
            if isinstance(item, dict)
        ],
    }


def create_voice_collection(
    runtime: McpRuntime,
    arguments: CreateVoiceCollectionInput,
) -> dict[str, Any]:
    result = runtime.require_application().create_voice_collection(
        name=arguments.name,
        description=arguments.description,
        idempotency_key=arguments.idempotency_key,
    )
    return safe_collection_projection(result)


def update_voice_collection(
    runtime: McpRuntime,
    arguments: UpdateVoiceCollectionInput,
) -> dict[str, Any]:
    values = arguments.model_dump(mode="json", exclude_unset=True)
    values.pop("collection_id", None)
    values.pop("idempotency_key", None)
    values["add_members"] = [
        item.model_dump(mode="json", exclude_none=True) for item in arguments.add_members
    ]
    values["remove_members"] = [
        item.model_dump(mode="json", exclude_none=True) for item in arguments.remove_members
    ]
    return safe_collection_projection(
        runtime.require_application().update_voice_collection(
            arguments.collection_id,
            expected_revision=arguments.expected_revision,
            name=arguments.name,
            description=arguments.description,
            include_description="description" in arguments.model_fields_set,
            add_members=values["add_members"],
            remove_members=values["remove_members"],
            idempotency_key=arguments.idempotency_key,
        )
    )


def update_catalog_voice_metadata(
    runtime: McpRuntime,
    arguments: UpdateCatalogVoiceMetadataInput,
) -> dict[str, Any]:
    result = runtime.require_application().update_catalog_voice_metadata(
        reference=arguments.reference.model_dump(mode="json", exclude_none=True),
        expected_revision=arguments.expected_revision,
        changes=arguments.changes.model_dump(mode="json", exclude_unset=True),
        idempotency_key=arguments.idempotency_key,
    )
    return safe_voice_projection(result)


def register_voice_lifecycle_tools(
    server: Any,
    runtime: McpRuntime,
    validated_call: Callable[..., dict[str, Any]],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    """Register lifecycle tools with flat typed signatures derived from inputs."""

    registrations = (
        (
            "pandrator_get_voice_capabilities",
            "Inspect voice design and cloning capabilities",
            voice_catalog_capabilities,
            VoiceCatalogCapabilitiesInput,
            read_only,
        ),
        (
            "pandrator_create_voice",
            "Create a managed voice",
            create_voice,
            VoiceCreateInput,
            write_action,
        ),
        (
            "pandrator_get_voice_samples",
            "Inspect managed voice samples",
            get_voice_samples,
            GetVoiceSamplesInput,
            read_only,
        ),
        (
            "pandrator_promote_voice_design",
            "Promote a reviewed voice-design preview",
            promote_voice_design,
            PromoteVoiceDesignInput,
            write_action,
        ),
        (
            "pandrator_import_voice_reference",
            "Import a reviewed managed voice reference",
            import_voice_reference,
            ImportVoiceReferenceInput,
            write_action,
        ),
        (
            "pandrator_transcribe_voice_sample",
            "Transcribe one managed voice sample",
            transcribe_voice_sample,
            TranscribeVoiceSampleInput,
            write_action,
        ),
        (
            "pandrator_review_voice_transcript",
            "Review one managed voice transcript",
            review_voice_transcript,
            ReviewVoiceTranscriptInput,
            write_action,
        ),
        (
            "pandrator_publish_voice",
            "Publish one managed voice to a provider",
            publish_voice,
            PublishVoiceInput,
            write_action,
        ),
        (
            "pandrator_audition_voice",
            "Queue one bounded voice audition",
            audition_voice,
            AuditionVoiceInput,
            write_action,
        ),
        (
            "pandrator_list_voice_collections",
            "List voice collections",
            list_voice_collections,
            ListVoiceCollectionsInput,
            read_only,
        ),
        (
            "pandrator_create_voice_collection",
            "Create a voice collection",
            create_voice_collection,
            CreateVoiceCollectionInput,
            write_action,
        ),
        (
            "pandrator_update_voice_collection",
            "Update a voice collection",
            update_voice_collection,
            UpdateVoiceCollectionInput,
            write_action,
        ),
        (
            "pandrator_update_catalog_voice_metadata",
            "Update provider voice catalog metadata",
            update_catalog_voice_metadata,
            UpdateCatalogVoiceMetadataInput,
            write_action,
        ),
    )
    for name, title, function, model, tool_annotations in registrations:

        def invoke(*, _function=function, _model=model, **values) -> dict[str, Any]:
            return validated_call(_function, runtime, _model, values)

        parameters: list[inspect.Parameter] = []
        signature_annotations: dict[str, Any] = {"return": dict[str, Any]}
        for field_name, field in model.model_fields.items():
            annotation = Annotated[field.annotation, field]
            signature_annotations[field_name] = annotation
            parameters.append(
                inspect.Parameter(
                    field_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=inspect.Parameter.empty if field.is_required() else field.default,
                )
            )
        invoke.__name__ = name
        invoke.__doc__ = (
            title + ". Provider credentials, settings, and raw job payloads are never returned."
        )
        invoke.__annotations__ = signature_annotations
        invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])
        server.tool(name=name, title=title, annotations=tool_annotations)(invoke)


__all__ = [
    "audition_voice",
    "create_voice",
    "create_voice_collection",
    "get_voice_samples",
    "import_voice_reference",
    "list_voice_collections",
    "promote_voice_design",
    "publish_voice",
    "register_voice_lifecycle_tools",
    "review_voice_transcript",
    "safe_collection_projection",
    "safe_samples_projection",
    "safe_sample_projection",
    "safe_voice_projection",
    "transcribe_voice_sample",
    "update_catalog_voice_metadata",
    "update_voice_collection",
    "voice_catalog",
    "voice_catalog_capabilities",
]
