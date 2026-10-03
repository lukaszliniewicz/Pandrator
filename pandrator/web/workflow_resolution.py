"""Resolve source-aware queue input inside the caller's read snapshot."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import Database
from .models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutcomePlan,
    SessionRecord,
    SpeechPlanReview,
)
from .source_resolution import PrimarySourceResolution
from .workflow_resolution_types import ResolvedWorkflowStage
from .workflow_stage_types import StageDefinition


class ExportAssemblyRequirementProtocol(Protocol):
    def __call__(
        self, *, workflow_kind: str, settings: dict[str, Any]
    ) -> bool: ...


class NormalizeExportModeProtocol(Protocol):
    def __call__(self, value: Any, *, workflow_kind: str) -> str: ...


class ClassifySourceProtocol(Protocol):
    def __call__(
        self, *, name: str, kind: str, mime_type: str
    ) -> str: ...


class ExportContractProtocol(Protocol):
    def __call__(
        self, *, workflow_kind: str, settings: dict[str, Any], source: PrimarySourceResolution
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WorkflowResolutionContext:
    database: Callable[[], Database]
    definitions: Callable[[SessionRecord, list[Artifact] | None], tuple[StageDefinition, ...]]
    matches_active_media_edit_revision: Callable[[Artifact, MediaEditPlanRevision | None], bool]
    usable_input: Callable[[StageDefinition, Artifact, str], bool]
    resource_keys: Callable[[str, str, dict[str, Any]], list[str]]
    validate_generation_language_payload: Callable[[Session, dict[str, Any]], None]
    selected_artifacts: Callable[[Session, str, list[Artifact] | None], dict[str, Artifact]]
    resolve_primary_source: Callable[[Session, str], PrimarySourceResolution]
    attached_source_artifact_ids: Callable[[Session, str], set[str]]
    workflow_transformations: Callable[[Session, str, OutcomePlan | None, Database], dict[str, Any]]
    export_requires_generation_assembly: ExportAssemblyRequirementProtocol
    capture_display_subtitle_snapshot: Callable[[Session, str, dict[str, Any]], dict[str, Any] | None]
    resolve_media_source: Callable[[Session, str], PrimarySourceResolution]
    normalize_export_mode: NormalizeExportModeProtocol
    classify_source: ClassifySourceProtocol
    build_export_contract: ExportContractProtocol
    primary_source_type: Callable[[], type[PrimarySourceResolution]]
    resolved_stage_type: Callable[[], type[ResolvedWorkflowStage]]


def resolve_workflow_stage_in_session(
    context: WorkflowResolutionContext,
    session: Session,
    session_id: str,
    stage_key: str,
    settings: dict[str, Any] | None,
    *,
    continuation: bool,
) -> ResolvedWorkflowStage:

    # Resolve persisted defaults before the run is enqueued.  The resulting
    # snapshot is immutable job input: later settings edits affect only
    # future runs, and Run Now values still take highest precedence.
    from .settings_policy import adapt_runtime_settings, normalize_subtitle_limit_override
    from .workspace_settings import WorkspaceSettingsService

    section_map: dict[str, tuple[str, ...]] = {
        "transcribe": ("stt", "subtitles"),
        "correct": ("correction", "subtitles", "source_passages"),
        "translate": ("translation", "subtitles", "source_passages"),
        "optimize_document": ("text",),
        "optimize_tts": ("text",),
        "clean_source": ("source_cleaning", "text"),
        # TTS is part of the immutable prepare snapshot because the
        # provider-aware chunk policy depends on it.  It is copied into a
        # private setting below rather than flattened into text runtime
        # parameters consumed by the preprocessor.
        "prepare_text": ("text", "audio", "tts"),
        "generate_audio": ("text", "tts", "audio", "rvc", "output"),
        "export": ("output", "audio", "subtitles"),
    }
    pipeline_sections = {
        section
        for key in (
            "transcribe",
            "correct",
            "translate",
            "clean_source",
            "prepare_text",
            "optimize_document",
            "optimize_tts",
            "generate_audio",
        )
        for section in section_map[key]
    }
    requested_sections = (
        sorted(pipeline_sections)
        if stage_key == "generate_audio"
        else list(section_map.get(stage_key, ()))
    )
    run_values = normalize_subtitle_limit_override(dict(settings or {}), runtime=True)
    # The server owns this immutable snapshot. Never accept a caller-supplied
    # contract and accidentally make it authoritative.
    run_values.pop("export_contract", None)
    requested_source_artifact_id = str(
        run_values.pop("source_artifact_id", "") or ""
    )
    explicit_requested_source = bool(requested_source_artifact_id)
    provided_stage_settings = run_values.pop("stage_settings", {})
    reuse_stages = [
        str(value)
        for value in (run_values.pop("reuse_stages", []) or [])
        if str(value)
    ]
    structured_override = {
        section: dict(run_values.get(section) or {})
        for section in requested_sections
        if isinstance(run_values.get(section), dict)
    }
    resolved, settings_hash = WorkspaceSettingsService(context.database()).resolve_in_session(
        session,
        session_id,
        requested_sections,
        structured_override,
    )
    flattened: dict[str, Any] = {}
    for section in requested_sections:
        if stage_key == "prepare_text" and section == "tts":
            continue
        flattened.update(adapt_runtime_settings(section, resolved.get(section, {})))
    if stage_key == "translate" and not requested_source_artifact_id:
        requested_source_artifact_id = str(
            flattened.get("source_artifact_id") or ""
        )
    # Existing stage dialogs submit flat values.  Preserve that contract
    # while accepting the newer section-shaped override form as well.
    flattened.update(
        {
            key: value
            for key, value in run_values.items()
            if key not in requested_sections
        }
    )
    if stage_key == "prepare_text":
        tts_snapshot = deepcopy(resolved.get("tts", {}))
        # Flat legacy stage submissions may contain provider fields.  The
        # immutable resolved TTS section is authoritative; keep those
        # fields out of the preprocessor settings and pass one private
        # captured snapshot to the worker instead.
        for key in tts_snapshot:
            flattened.pop(key, None)
        flattened["_audiobook_tts_settings"] = deepcopy(tts_snapshot)
    # Flat Run Now overrides use stable web service IDs (for example
    # ``kokoro``).  Re-adapt after applying them so the legacy synthesis
    # boundary receives its canonical dispatcher label (``Kokoro``).
    if stage_key == "generate_audio":
        flattened = adapt_runtime_settings("tts", flattened)
    resolved_stage_settings: dict[str, dict[str, Any]] = {}
    for key, sections in section_map.items():
        stage_value: dict[str, Any] = {}
        for section in sections:
            if key == "prepare_text" and section == "tts":
                continue
            stage_value.update(
                adapt_runtime_settings(section, resolved.get(section, {}))
            )
        if key == "prepare_text":
            stage_value["_audiobook_tts_settings"] = deepcopy(
                resolved.get("tts", {})
            )
        supplied = (
            provided_stage_settings.get(key, {})
            if isinstance(provided_stage_settings, dict)
            else {}
        )
        resolved_stage_settings[key] = {
            **stage_value,
            **normalize_subtitle_limit_override(
                supplied if isinstance(supplied, dict) else {}, runtime=True,
            ),
        }
        if key == "prepare_text":
            # Caller-supplied stage settings cannot replace the captured
            # provider snapshot or flatten its fields into preprocessing.
            captured_tts = stage_value["_audiobook_tts_settings"]
            for field in captured_tts:
                resolved_stage_settings[key].pop(field, None)
            resolved_stage_settings[key]["_audiobook_tts_settings"] = deepcopy(
                captured_tts
            )
        if key == "generate_audio":
            resolved_stage_settings[key] = adapt_runtime_settings(
                "tts", resolved_stage_settings[key]
            )

    record = session.get(SessionRecord, session_id)
    if record is None:
        raise KeyError(session_id)
    media_edit_plan = (
        session.scalar(
            select(MediaEditPlan).where(MediaEditPlan.session_id == session_id)
        )
        if record.workflow_kind in {"media_edit", "voiceover"}
        else None
    )
    active_media_edit_revision = (
        session.get(MediaEditPlanRevision, media_edit_plan.active_revision_id)
        if media_edit_plan is not None and media_edit_plan.active_revision_id
        else None
    )
    all_artifacts = list(
        session.scalars(
            select(Artifact)
            .where(Artifact.session_id == session_id)
            .order_by(Artifact.created_at.desc())
        ).all()
    )
    primary_source = context.resolve_primary_source(session, session_id)
    attached_sources = (
        [primary_source.artifact] if primary_source.artifact else []
    )
    attached_ids = {artifact.id for artifact in attached_sources}
    attached_source_ids = context.attached_source_artifact_ids(session, session_id)
    all_artifacts = [
        *attached_sources,
        *(
            artifact
            for artifact in all_artifacts
            if artifact.id not in attached_ids
        ),
    ]
    definition = next(
        (
            item
            for item in context.definitions(record, all_artifacts)
            if item.key == stage_key
        ),
        None,
    )
    if (
        definition is None
        or not definition.executable
        or not definition.job_kind
    ):
        raise ValueError(f"Stage '{stage_key}' cannot be run directly.")
    prerequisite_roles = definition.prerequisite_roles
    outcome = session.scalar(
        select(OutcomePlan).where(OutcomePlan.session_id == session_id)
    )
    transformations = context.workflow_transformations(session, session_id, outcome, context.database())
    inputs = (
        (outcome.value_json or {}).get("inputs", {})
        if outcome and isinstance(outcome.value_json, dict)
        else {}
    )
    if (
        stage_key == "translate"
        and str(inputs.get("translation") or "correction") != "correction"
    ):
        prerequisite_roles = (
            ("media_edit_subtitles",)
            if record.workflow_kind == "media_edit"
            else ("transcription", "upload")
        )
    elif stage_key in {"optimize_document", "generate_audio"}:
        if stage_key == "generate_audio" and bool(
            transformations.get("llm_tts_document_optimization")
        ):
            prerequisite_roles = ("tts_optimized",)
        elif record.workflow_kind == "audiobook":
            prerequisite_roles = ("prepared_text",)
        else:
            prerequisite_roles = {
                "translation": ("translation",),
                "correction": ("correction",),
                "media_edit": ("media_edit_subtitles",),
                "source": ("transcription", "upload"),
            }.get(
                str(inputs.get("generation") or "translation"),
                prerequisite_roles,
            )
    selections = context.selected_artifacts(session, session_id, all_artifacts)
    by_role: dict[str, Artifact] = {}
    for selected in selections.values():
        if context.matches_active_media_edit_revision(
            selected, active_media_edit_revision
        ):
            by_role.setdefault(selected.role, selected)
    for attached in attached_sources:
        by_role.setdefault("upload", attached)
    for candidate in all_artifacts:
        if (
            candidate.state == "current"
            and context.matches_active_media_edit_revision(
                candidate, active_media_edit_revision
            )
        ):
            by_role.setdefault(candidate.role, candidate)
    source = None
    if requested_source_artifact_id:
        requested = session.get(Artifact, requested_source_artifact_id)
        allowed_requested_roles = (
            {
                "media_edit_subtitles",
                "transcription",
                "correction",
                "upload",
            }
            if stage_key == "translate"
            else set(prerequisite_roles)
        )
        if (
            requested is None
            or requested.state == "deleted"
            or requested.role not in allowed_requested_roles
            or (
                requested.session_id != session_id
                and requested.id not in attached_source_ids
            )
            or not context.usable_input(
                definition, requested, record.workflow_kind
            )
            or not context.matches_active_media_edit_revision(
                requested, active_media_edit_revision
            )
        ):
            if explicit_requested_source:
                raise ValueError(
                    f"The selected artifact cannot be used by stage '{stage_key}'."
                )
        else:
            source = requested
    if source is None:
        source = next(
            (
                by_role[role]
                for role in prerequisite_roles
                if role in by_role
                and context.usable_input(
                    definition, by_role[role], record.workflow_kind
                )
            ),
            None,
        )
    generation_input_selected = source is not None
    # The primary automatic-generation action is allowed to enqueue
    # before its exact derived input exists: workflow.continue creates
    # those missing prerequisites in order. Individual stage controls
    # remain locked by snapshot.status == unavailable.
    if source is None and stage_key == "generate_audio":
        source = next(
            (artifact for artifact in attached_sources),
            None,
        )
    deferred_export_assembly = False
    if stage_key == "export":
        deferred_export_assembly = context.export_requires_generation_assembly(
            workflow_kind=record.workflow_kind,
            settings=flattened,
        )
        if deferred_export_assembly:
            export_generation_run_id = str(
                flattened.get("generation_run_id") or ""
            ).strip()
            generation_run = session.get(
                GenerationRun,
                export_generation_run_id,
            )
            if (
                generation_run is None
                or generation_run.session_id != session_id
            ):
                raise ValueError(
                    "The selected generation run does not belong to this session."
                )
            if generation_run.status != "completed":
                raise ValueError(
                    "Only a completed generation run can be exported."
                )
            from .workspace import find_matching_output_assembly

            matching_assembly, _assembly_snapshot, _assembly_hash = (
                find_matching_output_assembly(
                    session,
                    session_id=session_id,
                    run=generation_run,
                    resolved_settings_snapshot=resolved,
                )
            )
            source = (
                session.get(Artifact, matching_assembly.artifact_id)
                if matching_assembly is not None
                and matching_assembly.artifact_id
                else None
            )
    if (
        prerequisite_roles
        and source is None
        and not deferred_export_assembly
    ):
        if record.workflow_kind == "media_edit" and stage_key == "export":
            raise ValueError(
                "Render and review the active media edit before exporting it."
            )
        raise ValueError(
            f"Stage '{stage_key}' is missing a required input artifact."
        )
    payload: dict[str, Any] = {
        "session_id": session_id,
        "source_artifact_id": source.id if source else None,
        "settings": flattened,
        "resolved_settings_snapshot": resolved,
        "settings_hash": settings_hash,
    }
    if stage_key == "generate_audio":
        generation_plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
        generation_revision = session.get(GenerationPlanRevision, generation_plan.active_revision_id) if generation_plan and generation_plan.active_revision_id else None
        if generation_input_selected and generation_revision is not None and (session.get(SpeechPlanReview, generation_revision.id) or generation_revision.operation_json or (generation_revision.settings_json or {}).get("_prepared_for_review") or session.scalar(
            select(GenerationSegment.id).where(GenerationSegment.plan_revision_id == generation_revision.id, GenerationSegment.revision > 1).limit(1)
        )):
            payload["speech_plan_revision_id"] = generation_revision.id
    if stage_key == "export":
        from .export_subtitles import subtitle_profile
        from .workspace_settings import subtitle_settings_provenance

        subtitle_snapshot = WorkspaceSettingsService(context.database()).get_in_session(
            session, session_id, "subtitles"
        )
        original_run = dict(settings or {})
        structured_subtitles = original_run.get("subtitles")
        provenance = subtitle_settings_provenance(
            subtitle_snapshot,
            structured=structured_subtitles if isinstance(structured_subtitles, dict) else {},
            flat=original_run,
        )
        payload["subtitle_settings_provenance"] = provenance
        payload["subtitle_profiles"] = {
            "source": subtitle_profile(
                flattened, provenance, language=str(record.source_language or ""),
                language_origin="session_source_language",
            ),
            "target": subtitle_profile(
                flattened, provenance, language=str(record.target_language or ""),
                language_origin="session_target_language",
            ) if record.target_language else None,
        }
        payload["subtitle_profile_scope"] = "source_target_alternatives"
        payload["display_subtitle_snapshot"] = context.capture_display_subtitle_snapshot(
            session, session_id, flattened
        )
        export_source = context.resolve_media_source(session, session_id)
        # Converting an edited recording to voiceover retains its cut
        # timeline. Export must keep using the matching render.
        if record.workflow_kind == "media_edit" or (
            record.workflow_kind == "voiceover"
            and media_edit_plan is not None
            and context.normalize_export_mode(
                flattened.get("export_mode"),
                workflow_kind=record.workflow_kind,
            ) in {"media", "audio"}
        ):
            edited_media = by_role.get("media_edit_media")
            if edited_media is None:
                raise ValueError(
                    "Render and review the media edit before exporting it."
                )
            edited_name = str(
                (edited_media.metadata_json or {}).get("original_filename")
                or edited_media.relative_path.rsplit("/", 1)[-1]
            )
            edited_kind = str(edited_media.kind or "video")
            edited_mime = str(edited_media.mime_type or "")
            export_source = context.primary_source_type()(
                artifact=edited_media,
                source_asset=None,
                attachment=None,
                profile=context.classify_source(
                    name=edited_name,
                    kind=edited_kind,
                    mime_type=edited_mime,
                ),
                name=edited_name,
                kind=edited_kind,
                mime_type=edited_mime,
                resolution="derived_media_edit",
            )
        payload["export_contract"] = context.build_export_contract(
            workflow_kind=record.workflow_kind,
            settings=flattened,
            source=export_source,
        )
    if stage_key == "generate_audio" or continuation:
        payload.update(
            {"target_stage": stage_key, "stage_settings": resolved_stage_settings}
        )
        if stage_key == "generate_audio":
            payload["_tts_language_preflight_input_selected"] = (
                generation_input_selected
            )
        if reuse_stages:
            payload["reuse_stages"] = reuse_stages
        resource_keys = context.resource_keys(session_id, stage_key, flattened)
        if any(
            bool(transformations.get(key))
            for key in (
                "correction",
                "translation",
                "llm_tts_optimization",
                "llm_tts_document_optimization",
            )
        ):
            resource_keys.append("service:llm")
        upload = next(iter(attached_sources), None) or next(
            (
                artifact
                for artifact in all_artifacts
                if artifact.state == "current" and artifact.role == "upload"
            ),
            None,
        )
        if upload is not None and record.workflow_kind != "audiobook":
            filename = str(
                (upload.metadata_json or {}).get("original_filename")
                or upload.relative_path
            ).lower()
            if not filename.endswith((".srt", ".vtt")) and not any(
                artifact.state == "current" and artifact.role == "transcription"
                for artifact in all_artifacts
            ):
                resource_keys.append("service:stt")
        if stage_key == "generate_audio":
            context.validate_generation_language_payload(session, payload)
        return context.resolved_stage_type()(
            job_kind="workflow.continue",
            payload=payload,
            resource_keys=tuple(dict.fromkeys(resource_keys)),
            session_revision=record.revision,
            workflow_kind=record.workflow_kind,
            source_artifact_id=source.id if source else None,
            source_content_hash=(source.content_hash if source else None),
            outcome_revision=outcome.revision if outcome else 0,
        )
    # Direct one-click export must assemble first when the selected
    # generation run needs it. The frontend submits a single runStage call;
    # routing to export.variant keeps assembly and export pinned to the
    # same resolved intent instead of requiring a second click.
    job_kind = (
        "export.variant"
        if stage_key == "export" and deferred_export_assembly
        else definition.job_kind
    )
    return context.resolved_stage_type()(
        job_kind=job_kind,
        payload=payload,
        resource_keys=tuple(
            context.resource_keys(
                session_id,
                stage_key,
                flattened,
            )
        ),
        session_revision=record.revision,
        workflow_kind=record.workflow_kind,
        source_artifact_id=source.id if source else None,
        source_content_hash=source.content_hash if source else None,
        outcome_revision=outcome.revision if outcome else 0,
    )
