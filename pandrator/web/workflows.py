"""Source-aware workflow snapshots and prerequisite-safe stage queuing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select

from .artifact_selection import (
    STAGE_OUTPUT_ROLES,
    canonical_stage_key,
    selected_artifacts,
    stage_histories,
)
from .database import Database
from .export_contract import build_export_contract
from .jobs import JobQueue
from .models import (
    Artifact,
    GenerationPlan,
    GenerationRun,
    Job,
    OutcomePlan,
    SessionRecord,
    SessionSource,
    SourceAsset,
    UsageEvent,
    utcnow,
)
from .source_resolution import resolve_primary_source

WORKFLOW_HISTORY_PREVIEW_LIMIT = 10


def _attached_source_artifacts(db_session, session_id: str, *, current_only: bool = True) -> list[Artifact]:
    if not current_only:
        return list(
            db_session.scalars(
                select(Artifact)
                .join(SourceAsset, SourceAsset.artifact_id == Artifact.id)
                .join(SessionSource, SessionSource.source_asset_id == SourceAsset.id)
                .where(SessionSource.session_id == session_id)
                .order_by(
                    SessionSource.is_current.desc(),
                    SessionSource.updated_at.desc(),
                    Artifact.created_at.desc(),
                )
            ).all()
        )
    source = resolve_primary_source(db_session, session_id)
    return [source.artifact] if source.artifact else []


def _latest_current_artifacts_by_role(
    db_session,
    session_id: str,
    roles: set[str],
) -> list[Artifact]:
    if not roles:
        return []
    ranked = (
        select(
            Artifact.id.label("artifact_id"),
            func.row_number()
            .over(
                partition_by=Artifact.role,
                order_by=(Artifact.created_at.desc(), Artifact.id.desc()),
            )
            .label("role_rank"),
        )
        .where(
            Artifact.session_id == session_id,
            Artifact.state == "current",
            Artifact.role.in_(tuple(roles)),
        )
        .subquery()
    )
    return list(
        db_session.scalars(
            select(Artifact)
            .join(ranked, ranked.c.artifact_id == Artifact.id)
            .where(ranked.c.role_rank == 1)
        ).all()
    )


def _latest_jobs_by_kind(
    db_session,
    session_id: str,
    kinds: set[str],
) -> list[Job]:
    if not kinds:
        return []
    ranked = (
        select(
            Job.id.label("job_id"),
            func.row_number()
            .over(
                partition_by=Job.kind,
                order_by=(Job.created_at.desc(), Job.id.desc()),
            )
            .label("kind_rank"),
        )
        .where(
            Job.session_id == session_id,
            Job.kind.in_(tuple(kinds)),
        )
        .subquery()
    )
    return list(
        db_session.scalars(
            select(Job)
            .join(ranked, ranked.c.job_id == Job.id)
            .where(ranked.c.kind_rank == 1)
        ).all()
    )


@dataclass(frozen=True, slots=True)
class StageDefinition:
    key: str
    title: str
    explanation: str
    executable: bool = True
    prerequisite_roles: tuple[str, ...] = ()
    output_role: str | None = None
    job_kind: str | None = None


DUBBING_STAGES = (
    StageDefinition("transcribe", "Transcribe", "Create timed source-language subtitles from media.", prerequisite_roles=("upload",), output_role="transcription", job_kind="dubbing.transcribe"),
    StageDefinition("correct", "Correct", "Review punctuation, wording, merges, and splits without translating.", prerequisite_roles=("transcription", "upload"), output_role="correction", job_kind="dubbing.correct"),
    StageDefinition("translate", "Translate", "Create a separate target-language subtitle artifact.", prerequisite_roles=("correction", "transcription", "upload"), output_role="translation", job_kind="dubbing.translate"),
    StageDefinition("optimize_document", "Optimize subtitles before generation", "Optionally create a separate, reviewable speech-optimized revision before audio generation. This is useful when the LLM and TTS must not share limited GPU memory.", prerequisite_roles=("translation", "correction", "transcription", "upload"), output_role="tts_optimized", job_kind="text.optimize_tts"),
    StageDefinition("optimize_tts", "Optimize text for speech", "Choose one place for LLM speech optimization: create a reviewable whole-document revision before generation, or optimize segment batches while generation runs.", executable=False, prerequisite_roles=("translation", "correction", "transcription", "upload")),
    StageDefinition("preview", "Preview", "Compare source, correction, and translation with recorded lineage.", executable=False, prerequisite_roles=("translation", "correction", "transcription", "upload")),
    StageDefinition("generate_audio", "Generate audio", "Create reviewable per-segment takes, optionally optimizing each segment immediately before speech generation. Assembly remains manual.", prerequisite_roles=("translation", "correction", "transcription", "upload"), job_kind="dubbing.generate_audio"),
    StageDefinition("export", "Export", "Package selected takes, assembled audio, subtitle tracks, or a rendered video.", prerequisite_roles=("assembled_audio", "dubbing_audio", "translation", "correction", "transcription", "upload"), output_role="export", job_kind="export.create"),
)

SUBTITLE_STAGES = (
    StageDefinition("transcribe", "Transcribe", "Create timed source-language subtitles from media.", prerequisite_roles=("upload",), output_role="transcription", job_kind="dubbing.transcribe"),
    StageDefinition("correct", "Correct", "Review punctuation, wording, merges, and splits without translating.", prerequisite_roles=("transcription", "upload"), output_role="correction", job_kind="dubbing.correct"),
    StageDefinition("translate", "Translate", "Create a separate target-language subtitle artifact.", prerequisite_roles=("correction", "transcription", "upload"), output_role="translation", job_kind="dubbing.translate"),
    StageDefinition("preview", "Preview", "Compare source, correction, and translation with recorded lineage.", executable=False, prerequisite_roles=("translation", "correction", "transcription", "upload")),
    StageDefinition("export", "Export subtitles", "Save the selected cues as SRT or VTT, or concatenate them into a plain-text transcript.", prerequisite_roles=("translation", "correction", "transcription", "upload"), output_role="export", job_kind="export.create"),
)

AUDIOBOOK_STAGES = (
    StageDefinition("clean_source", "Clean source", "Review deterministic extraction and optional agentic cleanup.", prerequisite_roles=("upload",), output_role="clean_text", job_kind="source.clean"),
    StageDefinition("prepare_text", "Segment narration", "Create editable generation segments from the cleaned document. This controls text boundaries and pauses, not the TTS model.", prerequisite_roles=("clean_text",), output_role="prepared_text", job_kind="text.prepare"),
    StageDefinition("optimize_document", "Optimize narration before generation", "Optionally create a separate before-and-after narration revision for review before any audio is generated.", prerequisite_roles=("prepared_text",), output_role="tts_optimized", job_kind="text.optimize_tts"),
    StageDefinition("optimize_tts", "Optimize text for speech", "Choose one place for LLM speech optimization: create a reviewable whole-document revision before generation, or optimize segment batches while generation runs.", executable=False, prerequisite_roles=("prepared_text",)),
    StageDefinition("generate_audio", "Generate audio", "Run missing document preparation, then create reviewable narration takes from editable segments. Assembly remains manual.", prerequisite_roles=("prepared_text", "clean_text", "upload"), job_kind="audiobook.generate_audio"),
    StageDefinition("export", "Export", "Package the assembled audio with the selected format, metadata, and cover.", prerequisite_roles=("assembled_audio", "audiobook_audio"), output_role="export", job_kind="export.create"),
)


@dataclass(frozen=True, slots=True)
class ResolvedWorkflowStage:
    """Immutable queue submission resolved without changing durable state."""

    job_kind: str
    payload: dict[str, Any]
    resource_keys: tuple[str, ...]
    session_revision: int
    workflow_kind: str
    source_artifact_id: str | None
    source_content_hash: str | None
    outcome_revision: int


class WorkflowService:
    def __init__(self, database: Database, jobs: JobQueue):
        self.database = database
        self.jobs = jobs

    def definitions(self, record: SessionRecord, artifacts: list[Artifact] | None = None) -> tuple[StageDefinition, ...]:
        if record.workflow_kind == "audiobook":
            return AUDIOBOOK_STAGES
        if record.workflow_kind == "subtitles":
            definitions = SUBTITLE_STAGES
        else:
            definitions = DUBBING_STAGES
        upload = next((item for item in (artifacts or []) if item.role == "upload" and item.state == "current"), None)
        filename = str((upload.metadata_json or {}).get("original_filename") or upload.relative_path).lower() if upload else ""
        return tuple(item for item in definitions if not (filename.endswith(".srt") and item.key == "transcribe"))

    @staticmethod
    def _usable_input(definition: StageDefinition, artifact: Artifact, workflow_kind: str) -> bool:
        if artifact.role != "upload":
            # The caller has already selected this artifact through the exact
            # outcome-resolved role list (which may be narrower or newer than
            # the definition's broad compatibility roles).
            return True
        filename = str((artifact.metadata_json or {}).get("original_filename") or artifact.relative_path).lower()
        extension = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
        if definition.key == "transcribe":
            return extension in {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
        if definition.key in {"correct", "translate", "optimize_tts", "optimize_document", "preview"}:
            return extension == ".srt"
        if definition.key == "clean_source":
            return extension in {".txt", ".pdf", ".epub", ".docx", ".mobi"}
        if definition.key == "generate_audio":
            if workflow_kind == "audiobook":
                return extension in {".json", ".txt", ".md", ".pdf", ".epub", ".docx", ".mobi"}
            return extension == ".srt"
        return True

    def snapshot(self, session_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            attached_sources = _attached_source_artifacts(session, session_id)
            provisional_definitions = self.definitions(record, attached_sources)
            relevant_roles = {
                role
                for definition in provisional_definitions
                for role in (
                    *definition.prerequisite_roles,
                    *((definition.output_role,) if definition.output_role else ()),
                )
            }
            relevant_roles.add("upload")
            latest_current_artifacts = _latest_current_artifacts_by_role(
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
            definitions = self.definitions(record, artifacts)
            history_stage_keys = tuple(
                dict.fromkeys(
                    canonical_stage_key(definition.key)
                    for definition in definitions
                    if canonical_stage_key(definition.key) in STAGE_OUTPUT_ROLES
                )
            )
            histories, selections = stage_histories(
                session,
                session_id,
                history_stage_keys,
                limit=WORKFLOW_HISTORY_PREVIEW_LIMIT,
            )
            job_kinds = {
                definition.job_kind
                for definition in definitions
                if definition.job_kind
            }
            job_kinds.add("workflow.continue")
            latest_jobs = _latest_jobs_by_kind(session, session_id, job_kinds)
            roles: dict[str, Artifact] = {}
            for artifact in selections.values():
                roles.setdefault(artifact.role, artifact)
            for artifact in attached_sources:
                roles.setdefault("upload", artifact)
            for artifact in latest_current_artifacts:
                roles.setdefault(artifact.role, artifact)
            outcome = session.scalar(select(OutcomePlan).where(OutcomePlan.session_id == session_id))
            generation_plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
            generation_run = session.scalar(
                select(GenerationRun)
                .where(GenerationRun.session_id == session_id)
                .order_by(
                    GenerationRun.sequence_number.desc(),
                    GenerationRun.created_at.desc(),
                )
                .limit(1)
            )
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
            transformations = (outcome.value_json or {}).get("transformations", {}) if outcome and isinstance(outcome.value_json, dict) else {}
            optimization_enabled = bool(transformations.get("llm_tts_optimization"))
            document_optimization_enabled = bool(transformations.get("llm_tts_document_optimization"))
            input_choices = (outcome.value_json or {}).get("inputs", {}) if outcome and isinstance(outcome.value_json, dict) else {}
            latest_roles = {
                artifact.role: artifact for artifact in latest_current_artifacts
            }
            job_by_kind = {}
            for job in latest_jobs:
                job_by_kind.setdefault(job.kind, job)
            if "workflow.continue" in job_by_kind:
                job_by_kind["dubbing.generate_audio"] = job_by_kind["workflow.continue"]
                job_by_kind["audiobook.generate_audio"] = job_by_kind["workflow.continue"]
            stages = []
            stage_usage_scopes: dict[str, dict[str, str]] = {}
            document_definition = next((item for item in definitions if item.key == "optimize_document"), None)
            visible_definitions = tuple(item for item in definitions if item.key != "optimize_document")
            for index, definition in enumerate(visible_definitions, start=1):
                effective_definition = document_definition if definition.key == "optimize_tts" and document_optimization_enabled and document_definition else definition
                selection_key = canonical_stage_key(definition.key)
                history = histories.get(selection_key)
                artifact = (
                    selections.get(selection_key)
                    if history is not None
                    else latest_roles.get(effective_definition.output_role or "")
                )
                active = job_by_kind.get(effective_definition.job_kind or "")
                prerequisite_roles = effective_definition.prerequisite_roles
                if definition.key == "translate" and str(input_choices.get("translation") or "correction") != "correction":
                    prerequisite_roles = ("transcription", "upload")
                elif effective_definition.key in {"optimize_document", "generate_audio"}:
                    if definition.key == "generate_audio" and document_optimization_enabled:
                        prerequisite_roles = ("tts_optimized",)
                    elif record.workflow_kind == "audiobook":
                        prerequisite_roles = ("prepared_text",)
                    else:
                        prerequisite_roles = {
                            "translation": ("translation",),
                            "correction": ("correction",),
                            "source": ("transcription", "upload"),
                        }.get(str(input_choices.get("generation") or "translation"), effective_definition.prerequisite_roles)
                prerequisite = next(
                    (
                        roles[role]
                        for role in prerequisite_roles
                        if role in roles and self._usable_input(effective_definition, roles[role], record.workflow_kind)
                    ),
                    None,
                )
                if active and active.status in {"queued", "running", "cancel_requested"}:
                    status = "running"
                elif active and active.status in {"failed", "interrupted"} and (artifact is None or active.created_at >= artifact.updated_at):
                    status = "failed"
                elif artifact:
                    status = "completed"
                elif history and history["items"] and prerequisite is not None:
                    status = "stale"
                elif prerequisite_roles and prerequisite is None:
                    status = "unavailable"
                else:
                    status = "ready"
                if definition.key == "generate_audio" and not (
                    active and active.status in {"queued", "running", "cancel_requested"}
                ):
                    if generation_run is None:
                        status = "ready" if prerequisite is not None else "unavailable"
                    elif generation_plan is not None and generation_run.plan_revision_id != generation_plan.active_revision_id:
                        status = "stale"
                    elif generation_run.status in {"queued", "running", "pausing"}:
                        status = "running"
                    elif generation_run.status == "completed":
                        run_source_id = str((generation_run.settings_snapshot_json or {}).get("source_artifact_id") or "")
                        status = "stale" if prerequisite is not None and run_source_id and run_source_id != prerequisite.id else "completed"
                    elif generation_run.status == "failed":
                        status = "failed"
                    elif generation_run.status == "paused":
                        status = "ready"
                    else:
                        status = "ready"
                if definition.key == "optimize_tts" and prerequisite is not None and not document_optimization_enabled:
                    status = "completed" if optimization_enabled else "ready"
                if definition.key == "export" and status == "unavailable" and completed_generation_run is not None:
                    # Reviewable generation deliberately stops at per-segment
                    # takes. The Output step assembles the chosen completed run
                    # before exporting, so a prior assembly is not required to
                    # make this card available.
                    status = "ready"
                stage_enabled = (optimization_enabled or document_optimization_enabled) if definition.key == "optimize_tts" else None
                metric_job = active
                usage_scope: dict[str, str] = {}
                if definition.key == "generate_audio" and generation_run is not None:
                    usage_scope["generation_run_id"] = generation_run.id
                    if generation_run.job_id:
                        metric_job = session.get(Job, generation_run.job_id) or metric_job
                elif (
                    definition.key == "optimize_tts"
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
                        metric_job = session.get(Job, generation_run.job_id) or metric_job
                elif artifact is not None:
                    usage_scope["artifact_id"] = artifact.id
                    if active is not None:
                        # Research and the main LLM call share a job, while
                        # only the final call is linked to the output artifact.
                        # Keep both links so the card reports the whole stage.
                        usage_scope["job_id"] = active.id
                elif active is not None:
                    usage_scope["job_id"] = active.id
                elif definition.key == "optimize_tts" and stage_enabled:
                    # Compatibility for optimization usage recorded before
                    # artifact/job links were introduced.
                    usage_scope["usage_stage"] = "tts_optimization"
                stage_usage_scopes[definition.key] = usage_scope
                run_metrics = None
                if metric_job is not None:
                    duration_seconds = None
                    if metric_job.started_at is not None:
                        end = metric_job.finished_at or utcnow()
                        try:
                            duration_seconds = max(
                                0.0,
                                (end - metric_job.started_at).total_seconds(),
                            )
                        except TypeError:
                            duration_seconds = None
                    run_metrics = {
                        "started_at": (
                            metric_job.started_at.isoformat()
                            if metric_job.started_at
                            else None
                        ),
                        "finished_at": (
                            metric_job.finished_at.isoformat()
                            if metric_job.finished_at
                            else None
                        ),
                        "duration_seconds": duration_seconds,
                    }
                stages.append(
                    {
                        "number": index,
                        "key": definition.key,
                        "title": definition.title,
                        "explanation": definition.explanation,
                        "status": status,
                        "executable": bool(document_optimization_enabled) if definition.key == "optimize_tts" else definition.executable,
                        "toggle": definition.key == "optimize_tts",
                        "toggle_only": definition.key == "optimize_tts" and not document_optimization_enabled,
                        "enabled": stage_enabled,
                        "optimization_timing": "document" if document_optimization_enabled else "generation",
                        "included": definition.key in record.included_stages_json,
                        "required": definition.key == "transcribe" and any(key in record.included_stages_json for key in ("correct", "translate", "generate_audio")),
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
                        } if artifact else None,
                        "artifacts": history["items"] if history else [],
                        "selected_artifact_id": history["selected_artifact_id"] if history else (artifact.id if artifact else None),
                        "selection_revision": history["revision"] if history else 0,
                        "artifact_history_total": history["total"] if history else (1 if artifact else 0),
                        "artifact_history_has_more": history["has_more"] if history else False,
                        "artifact_history_next_before_version": history["next_before_version"] if history else None,
                        "job_id": active.id if active else None,
                        "progress": active.progress if active and status in {"running", "failed"} else None,
                        "detail": (
                            active.error_message
                            if active and status == "failed"
                            else (
                                active.progress_detail
                                or ("Waiting for an available worker" if active.status == "queued" else None)
                                if active and status == "running"
                                else None
                            )
                        ),
                        "usage": None,
                        "run_metrics": run_metrics,
                    }
                )

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
            for stage in stages:
                scope = stage_usage_scopes.get(str(stage["key"]), {})
                linked_job_id = artifact_job_links.get(
                    str(scope.get("artifact_id") or "")
                )
                if linked_job_id and not scope.get("job_id"):
                    scope["job_id"] = linked_job_id
                    metric_job = session.get(Job, linked_job_id)
                    if metric_job is not None:
                        duration_seconds = None
                        if metric_job.started_at is not None:
                            end = metric_job.finished_at or utcnow()
                            try:
                                duration_seconds = max(
                                    0.0,
                                    (end - metric_job.started_at).total_seconds(),
                                )
                            except TypeError:
                                duration_seconds = None
                        stage["run_metrics"] = {
                            "started_at": (
                                metric_job.started_at.isoformat()
                                if metric_job.started_at
                                else None
                            ),
                            "finished_at": (
                                metric_job.finished_at.isoformat()
                                if metric_job.finished_at
                                else None
                            ),
                            "duration_seconds": duration_seconds,
                        }
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
            if usage_stages:
                usage_filters.append(UsageEvent.stage.in_(usage_stages))
            usage_rows = []
            if usage_filters:
                usage_rows = session.execute(
                    select(
                        UsageEvent.job_id,
                        UsageEvent.artifact_id,
                        UsageEvent.generation_run_id,
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
                        UsageEvent.stage,
                        UsageEvent.model_id,
                    )
                ).all()
            for stage in stages:
                scope = stage_usage_scopes.get(str(stage["key"]), {})
                matching = []
                for row in usage_rows:
                    linked_scope = any(
                        scope.get(key)
                        for key in ("artifact_id", "generation_run_id", "job_id")
                    )
                    matched = bool(
                        (scope.get("artifact_id") and row.artifact_id == scope["artifact_id"])
                        or (
                            scope.get("generation_run_id")
                            and row.generation_run_id == scope["generation_run_id"]
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
                    "event_count": sum(
                        int(row.event_count or 0) for row in matching
                    ),
                    "model_ids": models,
                    "model_id": " · ".join(models),
                    "created_at": created_at.isoformat() if created_at else None,
                }
            return {
                "session_id": record.id,
                "workflow_kind": record.workflow_kind,
                "workflow_preset": record.workflow_preset,
                "revision": record.revision,
                "stages": stages,
                "sources": [
                    {
                        "id": artifact.id,
                        "filename": str((artifact.metadata_json or {}).get("original_filename") or artifact.relative_path.rsplit("/", 1)[-1]),
                        "kind": artifact.kind,
                        "role": artifact.role,
                    }
                    for artifact in attached_sources or [
                        item for item in artifacts if item.role == "upload" and item.state == "current"
                    ]
                ],
            }

    def resolve_stage(
        self,
        session_id: str,
        stage_key: str,
        settings: dict[str, Any] | None = None,
        *,
        continuation: bool = False,
    ) -> ResolvedWorkflowStage:
        """Resolve exact queue input without enqueuing work."""

        # Resolve persisted defaults before the run is enqueued.  The resulting
        # snapshot is immutable job input: later settings edits affect only
        # future runs, and Run Now values still take highest precedence.
        from .workspace import WorkspaceSettingsService, adapt_runtime_settings

        section_map: dict[str, tuple[str, ...]] = {
            "transcribe": ("stt", "subtitles"),
            "correct": ("correction", "subtitles"),
            "translate": ("translation", "subtitles"),
            "optimize_document": ("text",),
            "optimize_tts": ("text",),
            "clean_source": ("source_cleaning", "text"),
            "prepare_text": ("text", "audio"),
            "generate_audio": ("text", "tts", "audio", "rvc", "output"),
            "export": ("output", "audio", "subtitles"),
        }
        pipeline_sections = {
            section
            for key in ("transcribe", "correct", "translate", "clean_source", "prepare_text", "optimize_document", "optimize_tts", "generate_audio")
            for section in section_map[key]
        }
        requested_sections = sorted(pipeline_sections) if stage_key == "generate_audio" else list(section_map.get(stage_key, ()))
        run_values = dict(settings or {})
        # The server owns this immutable snapshot. Never accept a caller-supplied
        # contract and accidentally make it authoritative.
        run_values.pop("export_contract", None)
        requested_source_artifact_id = str(run_values.pop("source_artifact_id", "") or "")
        provided_stage_settings = run_values.pop("stage_settings", {})
        reuse_stages = [str(value) for value in (run_values.pop("reuse_stages", []) or []) if str(value)]
        structured_override = {
            section: dict(run_values.get(section) or {})
            for section in requested_sections
            if isinstance(run_values.get(section), dict)
        }
        resolved, settings_hash = WorkspaceSettingsService(self.database).resolve(
            session_id,
            requested_sections,
            structured_override,
        )
        flattened: dict[str, Any] = {}
        for section in requested_sections:
            flattened.update(adapt_runtime_settings(section, resolved.get(section, {})))
        # Existing stage dialogs submit flat values.  Preserve that contract
        # while accepting the newer section-shaped override form as well.
        flattened.update({key: value for key, value in run_values.items() if key not in requested_sections})
        # Flat Run Now overrides use stable web service IDs (for example
        # ``kokoro``).  Re-adapt after applying them so the legacy synthesis
        # boundary receives its canonical dispatcher label (``Kokoro``).
        if stage_key == "generate_audio":
            flattened = adapt_runtime_settings("tts", flattened)
        resolved_stage_settings: dict[str, dict[str, Any]] = {}
        for key, sections in section_map.items():
            stage_value: dict[str, Any] = {}
            for section in sections:
                stage_value.update(adapt_runtime_settings(section, resolved.get(section, {})))
            supplied = provided_stage_settings.get(key, {}) if isinstance(provided_stage_settings, dict) else {}
            resolved_stage_settings[key] = {**stage_value, **(supplied if isinstance(supplied, dict) else {})}
            if key == "generate_audio":
                resolved_stage_settings[key] = adapt_runtime_settings("tts", resolved_stage_settings[key])

        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            all_artifacts = list(session.scalars(select(Artifact).where(Artifact.session_id == session_id).order_by(Artifact.created_at.desc())).all())
            primary_source = resolve_primary_source(session, session_id)
            attached_sources = [primary_source.artifact] if primary_source.artifact else []
            attached_ids = {artifact.id for artifact in attached_sources}
            all_artifacts = [
                *attached_sources,
                *(artifact for artifact in all_artifacts if artifact.id not in attached_ids),
            ]
            definition = next((item for item in self.definitions(record, all_artifacts) if item.key == stage_key), None)
            if definition is None or not definition.executable or not definition.job_kind:
                raise ValueError(f"Stage '{stage_key}' cannot be run directly.")
            prerequisite_roles = definition.prerequisite_roles
            outcome = session.scalar(select(OutcomePlan).where(OutcomePlan.session_id == session_id))
            inputs = (outcome.value_json or {}).get("inputs", {}) if outcome and isinstance(outcome.value_json, dict) else {}
            if stage_key == "translate" and str(inputs.get("translation") or "correction") != "correction":
                prerequisite_roles = ("transcription", "upload")
            elif stage_key in {"optimize_document", "generate_audio"}:
                transformations = (outcome.value_json or {}).get("transformations", {}) if outcome and isinstance(outcome.value_json, dict) else {}
                if stage_key == "generate_audio" and bool(transformations.get("llm_tts_document_optimization")):
                    prerequisite_roles = ("tts_optimized",)
                elif record.workflow_kind == "audiobook":
                    prerequisite_roles = ("prepared_text",)
                else:
                    prerequisite_roles = {
                        "translation": ("translation",),
                        "correction": ("correction",),
                        "source": ("transcription", "upload"),
                    }.get(str(inputs.get("generation") or "translation"), prerequisite_roles)
            selections = selected_artifacts(session, session_id, all_artifacts)
            by_role: dict[str, Artifact] = {}
            for selected in selections.values():
                by_role.setdefault(selected.role, selected)
            for attached in attached_sources:
                by_role.setdefault("upload", attached)
            for candidate in all_artifacts:
                if candidate.state == "current":
                    by_role.setdefault(candidate.role, candidate)
            source = None
            if requested_source_artifact_id:
                requested = session.get(Artifact, requested_source_artifact_id)
                attached_ids = {artifact.id for artifact in attached_sources}
                if requested is None or (requested.session_id != session_id and requested.id not in attached_ids):
                    raise ValueError("The selected input artifact does not belong to this session.")
                if requested.role not in prerequisite_roles or not self._usable_input(definition, requested, record.workflow_kind):
                    raise ValueError(f"The selected artifact cannot be used by stage '{stage_key}'.")
                source = requested
            if source is None:
                source = next(
                    (
                        by_role[role]
                        for role in prerequisite_roles
                        if role in by_role and self._usable_input(definition, by_role[role], record.workflow_kind)
                    ),
                    None,
                )
            # The primary automatic-generation action is allowed to enqueue
            # before its exact derived input exists: workflow.continue creates
            # those missing prerequisites in order. Individual stage controls
            # remain locked by snapshot.status == unavailable.
            if source is None and stage_key == "generate_audio":
                source = next(
                    (artifact for artifact in attached_sources),
                    None,
                )
            if prerequisite_roles and source is None:
                raise ValueError(f"Stage '{stage_key}' is missing a required input artifact.")
            payload = {
                "session_id": session_id,
                "source_artifact_id": source.id if source else None,
                "settings": flattened,
                "resolved_settings_snapshot": resolved,
                "settings_hash": settings_hash,
            }
            if stage_key == "export":
                payload["export_contract"] = build_export_contract(
                    workflow_kind=record.workflow_kind,
                    settings=flattened,
                    source=primary_source,
                )
        if stage_key == "generate_audio" or continuation:
            payload.update({"target_stage": stage_key, "stage_settings": resolved_stage_settings})
            if reuse_stages:
                payload["reuse_stages"] = reuse_stages
            resource_keys = self._resource_keys(session_id, stage_key, flattened)
            transformations = (outcome.value_json or {}).get("transformations", {}) if outcome and isinstance(outcome.value_json, dict) else {}
            if any(bool(transformations.get(key)) for key in ("correction", "translation", "llm_tts_optimization", "llm_tts_document_optimization")):
                resource_keys.append("service:llm")
            upload = next(iter(attached_sources), None) or next((artifact for artifact in all_artifacts if artifact.state == "current" and artifact.role == "upload"), None)
            if upload is not None and record.workflow_kind != "audiobook":
                filename = str((upload.metadata_json or {}).get("original_filename") or upload.relative_path).lower()
                if not filename.endswith(".srt") and not any(artifact.state == "current" and artifact.role == "transcription" for artifact in all_artifacts):
                    resource_keys.append("service:stt")
            return ResolvedWorkflowStage(
                job_kind="workflow.continue",
                payload=payload,
                resource_keys=tuple(dict.fromkeys(resource_keys)),
                session_revision=record.revision,
                workflow_kind=record.workflow_kind,
                source_artifact_id=source.id if source else None,
                source_content_hash=(
                    source.content_hash if source else None
                ),
                outcome_revision=outcome.revision if outcome else 0,
            )
        return ResolvedWorkflowStage(
            job_kind=definition.job_kind,
            payload=payload,
            resource_keys=tuple(
                self._resource_keys(
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

    def run_stage(
        self,
        session_id: str,
        stage_key: str,
        settings: dict[str, Any] | None = None,
    ) -> Job:
        """Preserve the WebUI contract while sharing pure resolution."""

        resolved = self.resolve_stage(
            session_id,
            stage_key,
            settings,
        )
        return self.jobs.enqueue(
            resolved.job_kind,
            resolved.payload,
            session_id=session_id,
            resource_keys=list(resolved.resource_keys),
        )

    @staticmethod
    def _resource_keys(session_id: str, stage_key: str, settings: dict[str, Any]) -> list[str]:
        keys = [f"session:{session_id}"]
        if stage_key in {
            "correct",
            "translate",
            "optimize_tts",
            "optimize_document",
        } or (
            stage_key == "clean_source"
            and bool(settings.get("agentic", False))
        ):
            keys.append("service:llm")
        if stage_key == "generate_audio":
            service = str(settings.get("service") or "tts").lower().replace(" ", "_")
            keys.append(f"service:tts:{service}")
        if stage_key == "transcribe":
            keys.append("service:stt")
        compute = str(settings.get("compute_backend") or settings.get("device") or "auto").lower()
        if compute in {"cuda", "vulkan", "metal", "gpu"}:
            keys.append(f"gpu:{compute}")
        return keys
