"""Build source-aware workflow snapshots through explicit read dependencies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .database import Database
from .models import (
    AgentRun,
    Artifact,
    ArtifactEdge,
    GenerationPlan,
    GenerationRun,
    GenerationSegment,
    Job,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    UsageEvent,
)
from .source_resolution import PrimarySourceResolution
from .workflow_stage_types import StageDefinition


class StageHistoriesProtocol(Protocol):
    def __call__(
        self,
        session: Session,
        session_id: str,
        stage_keys: list[str] | tuple[str, ...],
        *,
        limit: int,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Artifact]]: ...


class LatestJobsProtocol(Protocol):
    def __call__(
        self,
        session: Session,
        session_id: str,
        kinds: set[str],
        *,
        include_ids: set[str] | None,
    ) -> list[Job]: ...


class CorrectionAncestryProtocol(Protocol):
    def __call__(
        self,
        session: Session,
        *,
        ancestor: Artifact,
        output: Artifact,
        session_id: str,
    ) -> bool: ...


class SubtitleReadinessProtocol(Protocol):
    def __call__(
        self,
        session: Session,
        session_id: str,
        *,
        primary: PrimarySourceResolution | None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WorkflowSnapshotContext:
    database: Database
    definitions: Callable[[SessionRecord, list[Artifact] | None], tuple[StageDefinition, ...]]
    matches_active_media_edit_revision: Callable[[Artifact, MediaEditPlanRevision | None], bool]
    usable_input: Callable[[StageDefinition, Artifact, str], bool]
    valid_translation_source: Callable[[StageDefinition, Artifact | None, str, str, set[str]], bool]
    resolve_primary_source: Callable[[Session, str], PrimarySourceResolution]
    current_artifacts_by_role: Callable[[Session, str, set[str]], list[Artifact]]
    canonical_stage_key: Callable[[str], str]
    stage_output_roles: Callable[[], dict[str, tuple[str, ...]]]
    stage_histories: StageHistoriesProtocol
    history_preview_limit: Callable[[], int]
    latest_jobs_by_kind: LatestJobsProtocol
    workflow_transformations: Callable[[Session, str, OutcomePlan | None, Database], dict[str, Any]]
    attached_source_artifact_ids: Callable[[Session, str], set[str]]
    correction_output_descends_from: CorrectionAncestryProtocol
    job_run_metrics: Callable[[Job], dict[str, Any]]
    subtitle_source_status_in_session: SubtitleReadinessProtocol


def build_workflow_snapshot(
    context: WorkflowSnapshotContext, session_id: str
) -> dict[str, Any]:
    with context.database.session() as session:
        record_row = session.execute(
            select(
                SessionRecord,
                SessionSetting,
                OutcomePlan,
                GenerationPlan,
                MediaEditPlanRevision,
            )
            .outerjoin(
                SessionSetting,
                and_(
                    SessionSetting.session_id == SessionRecord.id,
                    SessionSetting.section == "translation",
                ),
            )
            .outerjoin(OutcomePlan, OutcomePlan.session_id == SessionRecord.id)
            .outerjoin(
                GenerationPlan,
                GenerationPlan.session_id == SessionRecord.id,
            )
            .outerjoin(
                MediaEditPlan,
                MediaEditPlan.session_id == SessionRecord.id,
            )
            .outerjoin(
                MediaEditPlanRevision,
                MediaEditPlanRevision.id == MediaEditPlan.active_revision_id,
            )
            .where(SessionRecord.id == session_id)
        ).one_or_none()
        if record_row is None:
            raise KeyError(session_id)
        (
            record,
            translation_setting,
            outcome,
            generation_plan,
            active_media_edit_revision,
        ) = record_row
        configured_translation_source_id = str(
            (
                translation_setting.value_json
                if translation_setting is not None
                and isinstance(translation_setting.value_json, dict)
                else {}
            ).get("source_artifact_id")
            or ""
        )
        primary_source = context.resolve_primary_source(session, session_id)
        attached_sources = (
            [primary_source.artifact] if primary_source.artifact else []
        )
        attached_source_ids = (
            {primary_source.artifact.id}
            if primary_source.resolution == "attached"
            and primary_source.artifact is not None
            else set()
        )
        provisional_definitions = context.definitions(record, attached_sources)
        relevant_roles = {
            role
            for definition in provisional_definitions
            for role in (
                *definition.prerequisite_roles,
                *((definition.output_role,) if definition.output_role else ()),
            )
        }
        relevant_roles.add("upload")
        latest_current_artifacts = context.current_artifacts_by_role(
            session,
            session_id,
            relevant_roles,
        )
        artifacts = list(
            {
                artifact.id: artifact
                for artifact in [
                    *attached_sources,
                    *latest_current_artifacts,
                ]
            }.values()
        )
        known_artifacts = {artifact.id: artifact for artifact in artifacts}
        definitions = context.definitions(record, artifacts)
        history_stage_keys = tuple(
            dict.fromkeys(
                context.canonical_stage_key(definition.key)
                for definition in definitions
                if context.canonical_stage_key(definition.key) in context.stage_output_roles()
            )
        )
        histories, selections = context.stage_histories(
            session,
            session_id,
            history_stage_keys,
            limit=context.history_preview_limit(),
        )
        generation_run = session.scalar(
            select(GenerationRun)
            .where(GenerationRun.session_id == session_id)
            .order_by(
                GenerationRun.sequence_number.desc(),
                GenerationRun.created_at.desc(),
            )
            .limit(1)
        )
        # A resumed generation owns the card even when the workflow
        # wrapper that originally started it has already succeeded.  Pick
        # a running generation job first; when none is running, retain the
        # oldest queued one so a newer queued request cannot hide work
        # that is already waiting for a worker.
        generation_job_rows = list(
            session.scalars(
                select(Job)
                .where(
                    Job.session_id == session_id,
                    Job.kind == "generation.run",
                    Job.status.in_(
                        ("running", "queued", "pausing", "cancel_requested")
                    ),
                )
                .order_by(Job.created_at.asc(), Job.id.asc())
            ).all()
        )
        generation_job_runs: dict[str, GenerationRun] = {}
        generation_jobs: list[Job] = []
        for candidate in generation_job_rows:
            payload = (
                candidate.payload_json
                if isinstance(candidate.payload_json, dict)
                else {}
            )
            candidate_run_id = str(payload.get("generation_run_id") or "")
            candidate_run = (
                session.get(GenerationRun, candidate_run_id)
                if candidate_run_id
                else None
            )
            if candidate_run is None or candidate_run.session_id != session_id:
                continue
            generation_jobs.append(candidate)
            generation_job_runs[candidate.id] = candidate_run
        running_generation_jobs = [
            job
            for job in generation_jobs
            if job.status in {"running", "pausing"}
        ]
        queued_generation_jobs = [
            job for job in generation_jobs if job.status == "queued"
        ]
        generation_job = (
            max(
                running_generation_jobs, key=lambda item: (item.created_at, item.id)
            )
            if running_generation_jobs
            else min(
                queued_generation_jobs, key=lambda item: (item.created_at, item.id)
            )
            if queued_generation_jobs
            else next(
                (
                    job
                    for job in generation_jobs
                    if job.status == "cancel_requested"
                ),
                None,
            )
        )
        if generation_job is not None:
            generation_run = generation_job_runs[generation_job.id]
        job_kinds = {
            definition.job_kind for definition in definitions if definition.job_kind
        }
        job_kinds.add("workflow.continue")
        latest_jobs = context.latest_jobs_by_kind(
            session,
            session_id,
            job_kinds,
            include_ids={job.id for job in generation_jobs}
            | (
                {generation_run.job_id}
                if generation_run is not None and generation_run.job_id
                else set()
            ),
        )
        job_by_id = {job.id: job for job in latest_jobs}
        agentic_job_kinds = {
            definition.job_kind
            for definition in definitions
            if context.canonical_stage_key(definition.key)
            in {"correct", "translate", "optimize_tts"}
            and definition.job_kind
        }
        # Completed artifacts carry their AgentRun ID in immutable
        # metadata.  Loading every historical run on every snapshot added
        # a query even for sessions with no live or failed agentic work.
        # Query the run table only when its mutable status is needed.
        needs_agent_run_status = any(
            job.kind in {*agentic_job_kinds, "workflow.continue"}
            and job.status
            in {
                "queued",
                "running",
                "cancel_requested",
                "failed",
                "interrupted",
            }
            for job in latest_jobs
        )
        agent_runs = (
            list(
                session.scalars(
                    select(AgentRun)
                    .where(AgentRun.session_id == session_id)
                    .order_by(AgentRun.updated_at.desc())
                ).all()
            )
            if needs_agent_run_status
            else []
        )
        roles: dict[str, Artifact] = {}
        artifact: Artifact | None
        for artifact in selections.values():
            if context.matches_active_media_edit_revision(
                artifact, active_media_edit_revision
            ):
                roles.setdefault(artifact.role, artifact)
        for artifact in attached_sources:
            roles.setdefault("upload", artifact)
        for artifact in latest_current_artifacts:
            if context.matches_active_media_edit_revision(
                artifact, active_media_edit_revision
            ):
                roles.setdefault(artifact.role, artifact)
        completed_generation_run = (
            generation_run
            if generation_run is not None and generation_run.status == "completed"
            else (
                session.scalar(
                    select(GenerationRun)
                    .where(
                        GenerationRun.session_id == session_id,
                        GenerationRun.status == "completed",
                    )
                    .order_by(
                        GenerationRun.sequence_number.desc(),
                        GenerationRun.created_at.desc(),
                    )
                    .limit(1)
                )
                if generation_run is not None
                else None
            )
        )
        transformations = context.workflow_transformations(session, session_id, outcome, context.database)
        optimization_enabled = bool(transformations.get("llm_tts_optimization"))
        document_optimization_enabled = bool(
            transformations.get("llm_tts_document_optimization")
        )
        input_choices = (
            (outcome.value_json or {}).get("inputs", {})
            if outcome and isinstance(outcome.value_json, dict)
            else {}
        )
        latest_roles = {
            artifact.role: artifact
            for artifact in latest_current_artifacts
            if context.matches_active_media_edit_revision(
                artifact, active_media_edit_revision
            )
        }
        job_by_kind: dict[str, Job] = {}
        for job in latest_jobs:
            current = job_by_kind.get(job.kind)
            if current is None or (job.created_at, job.id) > (
                current.created_at,
                current.id,
            ):
                job_by_kind[job.kind] = job
        if "workflow.continue" in job_by_kind and generation_job is None:
            job_by_kind["dubbing.generate_audio"] = job_by_kind["workflow.continue"]
            job_by_kind["audiobook.generate_audio"] = job_by_kind[
                "workflow.continue"
            ]
        generation_plan_revision_id = (
            generation_run.plan_revision_id
            if generation_job is not None and generation_run is not None
            else generation_plan.active_revision_id
            if generation_plan is not None
            else generation_run.plan_revision_id
            if generation_run is not None
            else None
        )
        generation_total = 0
        generation_completed = 0
        if generation_plan_revision_id:
            generation_total = int(
                session.scalar(
                    select(func.count())
                    .select_from(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id
                        == generation_plan_revision_id,
                        GenerationSegment.removed.is_(False),
                    )
                )
                or 0
            )
            generation_completed = int(
                session.scalar(
                    select(func.count())
                    .select_from(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id
                        == generation_plan_revision_id,
                        GenerationSegment.removed.is_(False),
                        GenerationSegment.status == "completed",
                    )
                )
                or 0
            )
        generation_progress = (
            min(1.0, max(0.0, generation_completed / generation_total))
            if generation_total
            else 0.0
        )
        generation_progress_detail = (
            f"Generated {generation_completed} of {generation_total} segments"
            if generation_total
            else "Generated 0 of 0 segments"
        )
        generation_job_progress = (
            min(1.0, max(0.0, float(generation_job.progress)))
            if generation_job is not None
            else None
        )
        stages = []
        stage_usage_scopes: dict[str, dict[str, str]] = {}
        document_definition = next(
            (item for item in definitions if item.key == "optimize_document"), None
        )
        visible_definitions = tuple(
            item for item in definitions if item.key != "optimize_document"
        )
        for index, definition in enumerate(visible_definitions, start=1):
            effective_definition = (
                document_definition
                if definition.key == "optimize_tts"
                and document_optimization_enabled
                and document_definition
                else definition
            )
            selection_key = context.canonical_stage_key(definition.key)
            history = histories.get(selection_key)
            artifact = (
                selections.get(selection_key)
                if history is not None
                else latest_roles.get(effective_definition.output_role or "")
            )
            if (
                artifact is not None
                and not context.matches_active_media_edit_revision(
                    artifact, active_media_edit_revision
                )
            ):
                artifact = None
            active = (
                generation_job
                if definition.key == "generate_audio" and generation_job is not None
                else job_by_kind.get(effective_definition.job_kind or "")
            )
            agent_kind = {
                "correct": "correction",
                "translate": "translation",
                "optimize_tts": "tts_optimization",
            }.get(definition.key)
            agent_run = next(
                (
                    run
                    for run in agent_runs
                    if run.kind == agent_kind
                    and (
                        (
                            artifact is not None
                            and run.result_artifact_id == artifact.id
                        )
                        or (active is not None and run.job_id == active.id)
                    )
                ),
                None,
            )
            artifact_agent_run_id = str(
                (
                    (artifact.metadata_json or {}) if artifact is not None else {}
                ).get("agent_run_id")
                or ""
            )
            known_agent_run_id = (
                agent_run.id if agent_run is not None else artifact_agent_run_id
            ) or None
            prerequisite_roles = effective_definition.prerequisite_roles
            if (
                definition.key == "translate"
                and str(input_choices.get("translation") or "correction")
                != "correction"
            ):
                prerequisite_roles = (
                    ("media_edit_subtitles",)
                    if record.workflow_kind == "media_edit"
                    else ("transcription", "upload")
                )
            elif effective_definition.key in {
                "optimize_document",
                "generate_audio",
            }:
                if (
                    definition.key == "generate_audio"
                    and document_optimization_enabled
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
                        str(input_choices.get("generation") or "translation"),
                        effective_definition.prerequisite_roles,
                    )
            explicit_prerequisite = None
            if definition.key == "translate":
                recorded_source_id = (
                    str(
                        (artifact.metadata_json or {}).get("source_artifact_id")
                        or ""
                    )
                    if artifact is not None
                    else ""
                )
                explicit_source_id = (
                    configured_translation_source_id or recorded_source_id
                )
                candidate = (
                    known_artifacts.get(explicit_source_id)
                    or session.get(Artifact, explicit_source_id)
                    if explicit_source_id
                    else None
                )
                if (
                    candidate is not None
                    and candidate.session_id != session_id
                    and candidate.id not in attached_source_ids
                ):
                    attached_source_ids.update(
                        context.attached_source_artifact_ids(session, session_id)
                    )
                if context.valid_translation_source(
                    effective_definition,
                    candidate,
                    record.workflow_kind,
                    session_id,
                    attached_source_ids,
                ):
                    explicit_prerequisite = candidate
            prerequisite = explicit_prerequisite or next(
                (
                    roles[role]
                    for role in prerequisite_roles
                    if role in roles
                    and context.usable_input(
                        effective_definition,
                        roles[role],
                        record.workflow_kind,
                    )
                ),
                None,
            )
            artifact_matches_prerequisite = True
            if artifact is not None and prerequisite is not None:
                metadata = (
                    artifact.metadata_json
                    if isinstance(artifact.metadata_json, dict)
                    else {}
                )
                recorded_source_id = str(metadata.get("source_artifact_id") or "")
                recorded_source_hash = str(
                    metadata.get("source_content_hash") or ""
                )
                if definition.key == "correct":
                    # A correction is fresh only on the currently selected
                    # source branch. Identical bytes do not make a sibling
                    # revision interchangeable with that selection.
                    artifact_matches_prerequisite = bool(
                        recorded_source_id == prerequisite.id
                        or context.correction_output_descends_from(
                            session,
                            ancestor=prerequisite,
                            output=artifact,
                            session_id=session_id,
                        )
                    )
                elif recorded_source_id or recorded_source_hash:
                    exact_parent = bool(
                        recorded_source_id == prerequisite.id
                        or session.get(
                            ArtifactEdge,
                            (prerequisite.id, artifact.id),
                        )
                        is not None
                    )
                    # A translation belongs to the exact subtitle revision
                    # the user chose. Two revisions can legitimately have
                    # identical bytes while representing different branches,
                    # so content hashes are not a safe lineage substitute.
                    artifact_matches_prerequisite = exact_parent or bool(
                        definition.key != "translate"
                        and recorded_source_hash
                        and prerequisite.content_hash
                        and recorded_source_hash == prerequisite.content_hash
                    )
            stale_reason = None
            if active and active.status in {
                "queued",
                "running",
                "cancel_requested",
            }:
                status = "running"
            elif (
                active
                and active.status in {"failed", "interrupted"}
                and (artifact is None or active.created_at >= artifact.updated_at)
            ):
                status = "failed"
            elif artifact:
                if artifact_matches_prerequisite:
                    status = "completed"
                else:
                    status = "stale"
                    stale_reason = "prerequisite_superseded"
            elif history and history["items"] and prerequisite is not None:
                status = "stale"
                stale_reason = "selection_required"
            elif prerequisite_roles and prerequisite is None:
                status = "unavailable"
            else:
                status = "ready"
            if definition.key == "generate_audio" and not (
                active
                and active.status in {"queued", "running", "cancel_requested"}
            ):
                if generation_run is None:
                    status = "ready" if prerequisite is not None else "unavailable"
                elif (
                    generation_plan is not None
                    and generation_run.plan_revision_id
                    != generation_plan.active_revision_id
                ):
                    status = "stale"
                    stale_reason = "generation_plan_superseded"
                elif generation_run.status in {"queued", "running", "pausing"}:
                    status = "running"
                elif generation_run.status == "completed":
                    run_source_id = str(
                        (generation_run.settings_snapshot_json or {}).get(
                            "source_artifact_id"
                        )
                        or ""
                    )
                    if (
                        prerequisite is not None
                        and run_source_id
                        and run_source_id != prerequisite.id
                    ):
                        status = "stale"
                        stale_reason = "generation_source_mismatch"
                    else:
                        status = "completed"
                elif generation_run.status == "failed":
                    status = "failed"
                elif generation_run.status == "paused":
                    status = "ready"
                else:
                    status = "ready"
            if (
                definition.key == "optimize_tts"
                and prerequisite is not None
                and not document_optimization_enabled
            ):
                status = "completed" if optimization_enabled else "ready"
            if (
                definition.key == "export"
                and status == "unavailable"
                and completed_generation_run is not None
            ):
                # Reviewable generation deliberately stops at per-segment
                # takes. The Output step assembles the chosen completed run
                # before exporting, so a prior assembly is not required to
                # make this card available.
                status = "ready"
            stage_enabled = (
                (optimization_enabled or document_optimization_enabled)
                if definition.key == "optimize_tts"
                else None
            )
            metric_job = active
            usage_scope: dict[str, str] = {}
            if definition.key == "generate_audio" and generation_run is not None:
                usage_scope["generation_run_id"] = generation_run.id
                if generation_run.job_id:
                    metric_job = job_by_id.get(generation_run.job_id) or metric_job
            elif (
                definition.key == "optimize_tts"
                and optimization_enabled
                and not document_optimization_enabled
                and generation_run is not None
            ):
                usage_scope.update(
                    {
                        "generation_run_id": generation_run.id,
                        "usage_stage": "tts_optimization",
                    }
                )
                if generation_run.job_id:
                    metric_job = job_by_id.get(generation_run.job_id) or metric_job
            elif artifact is not None:
                usage_scope["artifact_id"] = artifact.id
                if active is not None:
                    # Research and the main LLM call share a job, while
                    # only the final call is linked to the output artifact.
                    # Keep both links so the card reports the whole stage.
                    usage_scope["job_id"] = active.id
            if known_agent_run_id is not None:
                usage_scope["agent_run_id"] = known_agent_run_id
            elif active is not None:
                usage_scope["job_id"] = active.id
            elif definition.key == "optimize_tts" and stage_enabled:
                # Compatibility for optimization usage recorded before
                # artifact/job links were introduced.
                usage_scope["usage_stage"] = "tts_optimization"
            stage_usage_scopes[definition.key] = usage_scope
            run_metrics = (
                context.job_run_metrics(metric_job) if metric_job is not None else None
            )
            if active and status == "failed":
                stage_detail = active.error_message
            elif (
                definition.key == "generate_audio"
                and generation_job_progress is not None
            ):
                # Progress exists only when active is the selected generation job.
                stage_detail = cast(Job, active).progress_detail or (
                    "Waiting for an available worker"
                    if cast(Job, active).status == "queued"
                    else None
                )
            elif active and status == "running":
                stage_detail = active.progress_detail or (
                    "Waiting for an available worker"
                    if active.status == "queued"
                    else None
                )
            elif definition.key == "generate_audio":
                stage_detail = generation_progress_detail
            else:
                stage_detail = None
            resolved_generation_input = None
            if definition.key == "generate_audio" and prerequisite is not None:
                input_stage = {
                    "transcription": "transcribe",
                    "correction": "correct",
                    "translation": "translate",
                    "tts_optimized": "optimize_tts",
                }.get(prerequisite.role, "source")
                input_history = histories.get(input_stage)
                input_item = next(
                    (
                        item
                        for item in (input_history or {}).get("items", [])
                        if item.get("id") == prerequisite.id
                    ),
                    None,
                )
                is_current_attachment = prerequisite.id in {
                    item.id for item in attached_sources
                }
                source_origin = (
                    "attached"
                    if prerequisite.id in attached_source_ids
                    else "current"
                    if prerequisite.role == "upload"
                    else "stage"
                )
                label = {
                    "transcription": "Transcription",
                    "correction": "Correction",
                    "translation": "Translation",
                    "tts_optimized": "Speech-optimized subtitles",
                }.get(prerequisite.role, "Current source")
                if source_origin == "attached":
                    label = (
                        "Current attached source"
                        if is_current_attachment
                        else "Attached source"
                    )
                resolved_generation_input = {
                    "artifact_id": prerequisite.id,
                    "role": prerequisite.role,
                    "stage_key": input_stage,
                    "version": input_item.get("version") if input_item else None,
                    "label": label,
                    "origin": source_origin,
                    "selection_stage": input_stage,
                    "selected_artifact_id": (
                        input_history.get("selected_artifact_id")
                        if input_history
                        else prerequisite.id
                    ),
                }
            stage = {
                "number": index,
                "key": definition.key,
                "title": definition.title,
                "explanation": definition.explanation,
                "status": status,
                "stale_reason": stale_reason if status == "stale" else None,
                "executable": bool(document_optimization_enabled)
                if definition.key == "optimize_tts"
                else definition.executable,
                "toggle": definition.key == "optimize_tts",
                "toggle_only": definition.key == "optimize_tts"
                and not document_optimization_enabled,
                "enabled": stage_enabled,
                "optimization_timing": "document"
                if document_optimization_enabled
                else "generation",
                "included": definition.key in record.included_stages_json,
                "required": definition.key == "transcribe"
                and any(
                    key in record.included_stages_json
                    for key in ("correct", "translate", "generate_audio")
                ),
                "artifact": {
                    "id": artifact.id,
                    "role": artifact.role,
                    "path": artifact.relative_path,
                    "relative_path": artifact.relative_path,
                    "kind": artifact.kind,
                    "mime_type": artifact.mime_type,
                    "size_bytes": artifact.size_bytes,
                    "state": artifact.state,
                    "metadata_json": artifact.metadata_json or {},
                }
                if artifact
                else None,
                "artifacts": history["items"] if history else [],
                "selected_artifact_id": history["selected_artifact_id"]
                if history
                else (artifact.id if artifact else None),
                "selection_revision": history["revision"] if history else 0,
                "artifact_history_total": history["total"]
                if history
                else (1 if artifact else 0),
                "artifact_history_has_more": history["has_more"]
                if history
                else False,
                "artifact_history_next_before_version": history[
                    "next_before_version"
                ]
                if history
                else None,
                "job_id": active.id if active else None,
                "agent_run_id": known_agent_run_id,
                "resumable": bool(
                    status == "failed"
                    and agent_run is not None
                    and agent_run.status in {"failed", "interrupted"}
                ),
                "progress": (
                    generation_job_progress
                    if definition.key == "generate_audio"
                    and generation_job_progress is not None
                    else generation_progress
                    if definition.key == "generate_audio"
                    else active.progress
                    if active and status in {"running", "failed"}
                    else None
                ),
                "detail": stage_detail,
                "usage": None,
                "run_metrics": run_metrics,
            }
            if definition.key == "generate_audio":
                stage["progress_basis"] = (
                    "job"
                    if generation_job_progress is not None
                    else "segments"
                )
                stage["resolved_input"] = resolved_generation_input
            stages.append(stage)

        artifact_ids = {
            scope["artifact_id"]
            for scope in stage_usage_scopes.values()
            if scope.get("artifact_id")
        }
        unlinked_artifact_ids = {
            scope["artifact_id"]
            for scope in stage_usage_scopes.values()
            if scope.get("artifact_id") and not scope.get("job_id")
        }
        # Automatic workflows can run several stages inside one
        # ``workflow.continue`` job, so that job is not necessarily the
        # latest job under the stage's direct kind. The final usage event
        # is linked to the artifact and tells us which job owns the whole
        # stage, including research/tool turns that are not artifact-linked.
        artifact_job_links: dict[str, str] = {}
        if unlinked_artifact_ids:
            for artifact_id, job_id in session.execute(
                select(UsageEvent.artifact_id, UsageEvent.job_id)
                .where(
                    UsageEvent.session_id == session_id,
                    UsageEvent.artifact_id.in_(unlinked_artifact_ids),
                    UsageEvent.job_id.is_not(None),
                )
                .order_by(UsageEvent.created_at.desc())
            ):
                if artifact_id and job_id:
                    artifact_job_links.setdefault(artifact_id, job_id)
        missing_job_ids = set(artifact_job_links.values()) - job_by_id.keys()
        if missing_job_ids:
            job_by_id.update(
                {
                    job.id: job
                    for job in session.scalars(
                        select(Job).where(Job.id.in_(missing_job_ids))
                    ).all()
                }
            )
        for stage in stages:
            scope = stage_usage_scopes.get(str(stage["key"]), {})
            linked_job_id = artifact_job_links.get(
                str(scope.get("artifact_id") or "")
            )
            if linked_job_id and not scope.get("job_id"):
                scope["job_id"] = linked_job_id
                metric_job = job_by_id.get(linked_job_id)
                if metric_job is not None:
                    stage["run_metrics"] = context.job_run_metrics(metric_job)
        metric_job_ids = {
            scope["job_id"]
            for scope in stage_usage_scopes.values()
            if scope.get("job_id")
        }
        generation_run_ids = {
            scope["generation_run_id"]
            for scope in stage_usage_scopes.values()
            if scope.get("generation_run_id")
        }
        agent_run_ids = {
            scope["agent_run_id"]
            for scope in stage_usage_scopes.values()
            if scope.get("agent_run_id")
        }
        usage_stages = {
            scope["usage_stage"]
            for scope in stage_usage_scopes.values()
            if scope.get("usage_stage")
        }
        usage_filters = []
        if artifact_ids:
            usage_filters.append(UsageEvent.artifact_id.in_(artifact_ids))
        if metric_job_ids:
            usage_filters.append(UsageEvent.job_id.in_(metric_job_ids))
        if generation_run_ids:
            usage_filters.append(
                UsageEvent.generation_run_id.in_(generation_run_ids)
            )
        if agent_run_ids:
            usage_filters.append(UsageEvent.agent_run_id.in_(agent_run_ids))
        if usage_stages:
            usage_filters.append(UsageEvent.stage.in_(usage_stages))
        usage_rows: list[Any] = []
        if usage_filters:
            usage_rows = list(
                session.execute(
                    select(
                        UsageEvent.job_id,
                        UsageEvent.artifact_id,
                        UsageEvent.generation_run_id,
                        UsageEvent.agent_run_id,
                        UsageEvent.stage,
                        UsageEvent.model_id,
                        func.sum(UsageEvent.input_tokens).label("input_tokens"),
                        func.sum(UsageEvent.cached_input_tokens).label(
                            "cached_input_tokens"
                        ),
                        func.sum(UsageEvent.output_tokens).label("output_tokens"),
                        func.sum(UsageEvent.cost_usd).label("cost_usd"),
                        func.count(UsageEvent.cost_usd).label("priced_event_count"),
                        func.count(UsageEvent.id).label("event_count"),
                        func.max(UsageEvent.created_at).label("created_at"),
                    )
                    .where(
                        UsageEvent.session_id == session_id,
                        or_(*usage_filters),
                    )
                    .group_by(
                        UsageEvent.job_id,
                        UsageEvent.artifact_id,
                        UsageEvent.generation_run_id,
                        UsageEvent.agent_run_id,
                        UsageEvent.stage,
                        UsageEvent.model_id,
                    )
                ).all()
            )
        for stage in stages:
            scope = stage_usage_scopes.get(str(stage["key"]), {})
            matching = []
            for row in usage_rows:
                linked_scope = any(
                    scope.get(key)
                    for key in (
                        "artifact_id",
                        "generation_run_id",
                        "agent_run_id",
                        "job_id",
                    )
                )
                matched = bool(
                    (
                        scope.get("artifact_id")
                        and row.artifact_id == scope["artifact_id"]
                    )
                    or (
                        scope.get("generation_run_id")
                        and row.generation_run_id == scope["generation_run_id"]
                    )
                    or (
                        scope.get("agent_run_id")
                        and row.agent_run_id == scope["agent_run_id"]
                    )
                    or (scope.get("job_id") and row.job_id == scope["job_id"])
                    or (not linked_scope and scope.get("usage_stage"))
                )
                if matched and scope.get("usage_stage"):
                    matched = row.stage == scope["usage_stage"]
                if matched:
                    matching.append(row)
            if not matching:
                continue
            input_tokens = sum(int(row.input_tokens or 0) for row in matching)
            cached_input_tokens = sum(
                int(row.cached_input_tokens or 0) for row in matching
            )
            output_tokens = sum(int(row.output_tokens or 0) for row in matching)
            priced_event_count = sum(
                int(row.priced_event_count or 0) for row in matching
            )
            models = sorted(
                {
                    str(row.model_id)
                    for row in matching
                    if str(row.model_id or "").strip()
                }
            )
            created_at = max(
                (row.created_at for row in matching if row.created_at),
                default=None,
            )
            stage["usage"] = {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cost_usd": (
                    sum(float(row.cost_usd or 0.0) for row in matching)
                    if priced_event_count
                    else None
                ),
                "event_count": sum(int(row.event_count or 0) for row in matching),
                "model_ids": models,
                "model_id": " · ".join(models),
                "created_at": created_at.isoformat() if created_at else None,
            }
        source_readiness = context.subtitle_source_status_in_session(
            session, session_id, primary=primary_source
        )
        if source_readiness["adoption_required"]:
            for stage in stages:
                if stage["key"] in {"correct", "translate", "optimize_document", "generate_audio"} and stage["status"] == "ready":
                    stage["status"] = "unavailable"
                    stage["stale_reason"] = "Register the existing subtitle source to create a timed revision first."
        return {
            "session_id": record.id,
            "workflow_kind": record.workflow_kind,
            "workflow_preset": record.workflow_preset,
            "revision": record.revision,
            "stages": stages,
            "subtitle_source": source_readiness,
            "sources": [
                {
                    "id": artifact.id,
                    "filename": str(
                        (artifact.metadata_json or {}).get("original_filename")
                        or artifact.relative_path.rsplit("/", 1)[-1]
                    ),
                    "kind": artifact.kind,
                    "role": artifact.role,
                }
                for artifact in attached_sources
                or [
                    item
                    for item in artifacts
                    if item.role == "upload" and item.state == "current"
                ]
            ],
        }
