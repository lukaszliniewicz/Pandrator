"""Source-aware workflow snapshots and prerequisite-safe stage queuing."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .artifact_selection import (
    STAGE_OUTPUT_ROLES,
    canonical_stage_key,
    selected_artifacts,
    stage_histories,
)
from .database import Database
from .export_contract import (
    build_export_contract,
    export_requires_generation_assembly,
    normalize_export_mode,
)
from .generation_subtitles import capture_display_subtitle_snapshot
from .jobs import JobQueue
from .models import (
    Artifact,
    ArtifactEdge,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutcomePlan,
    SessionRecord,
    SessionSource,
    SourceAsset,
    SpeechPlanReview,
    utcnow,
)
from .source_resolution import (
    PrimarySourceResolution,
    classify_source,
    resolve_media_source,
    resolve_primary_source,
)
from .subtitle_sources import subtitle_source_status_in_session
from .workflow_inputs import workflow_transformations
from .workflow_snapshot import WorkflowSnapshotContext, build_workflow_snapshot
from .workflow_stage_types import StageDefinition as StageDefinition

WORKFLOW_HISTORY_PREVIEW_LIMIT = 10
MAX_CORRECTION_ANCESTRY_EDGES = 512


def _as_utc(value: datetime) -> datetime:
    """Interpret SQLite's naive timestamps as the UTC values we persisted."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _correction_output_descends_from(
    session: Session,
    *,
    ancestor: Artifact,
    output: Artifact,
    session_id: str,
) -> bool:
    """Check bounded, same-session ArtifactEdge ancestry for a correction."""
    if (
        ancestor.session_id != session_id
        or output.session_id != session_id
    ):
        return False
    if ancestor.id == output.id:
        return True

    pending = deque([output.id])
    visited = {output.id}
    examined_edges = 0
    while pending and examined_edges < MAX_CORRECTION_ANCESTRY_EDGES:
        child_id = pending.popleft()
        remaining_edges = MAX_CORRECTION_ANCESTRY_EDGES - examined_edges
        parent_ids = list(
            session.scalars(
                select(ArtifactEdge.parent_artifact_id)
                .join(Artifact, Artifact.id == ArtifactEdge.parent_artifact_id)
                .where(
                    ArtifactEdge.child_artifact_id == child_id,
                    Artifact.session_id == session_id,
                )
                .limit(remaining_edges + 1)
            ).all()
        )
        if len(parent_ids) > remaining_edges:
            return False
        examined_edges += len(parent_ids)
        for parent_id in parent_ids:
            if parent_id == ancestor.id:
                return True
            if parent_id not in visited:
                visited.add(parent_id)
                pending.append(parent_id)
    return False


def _job_run_metrics(job: Job) -> dict[str, Any]:
    """Return timezone-safe timing data for a workflow-stage job."""
    started_at = _as_utc(job.started_at) if job.started_at else None
    finished_at = _as_utc(job.finished_at) if job.finished_at else None
    duration_seconds = None
    if started_at is not None:
        end = finished_at or _as_utc(utcnow())
        duration_seconds = max(0.0, (end - started_at).total_seconds())
    return {
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
        "duration_seconds": duration_seconds,
    }


def _attached_source_artifacts(
    db_session, session_id: str, *, current_only: bool = True
) -> list[Artifact]:
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


def _attached_source_artifact_ids(db_session, session_id: str) -> set[str]:
    """All library artifacts attached to a session, including non-current ones."""

    return set(
        db_session.scalars(
            select(Artifact.id)
            .join(SourceAsset, SourceAsset.artifact_id == Artifact.id)
            .join(SessionSource, SessionSource.source_asset_id == SourceAsset.id)
            .where(SessionSource.session_id == session_id)
        ).all()
    )


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
    *,
    include_ids: set[str] | None = None,
) -> list[Job]:
    include_ids = include_ids or set()
    if not kinds and not include_ids:
        return []
    ranked = None
    if kinds:
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
    statement = select(Job).where(Job.session_id == session_id)
    if ranked is not None:
        statement = statement.outerjoin(ranked, ranked.c.job_id == Job.id)
        latest_filter = ranked.c.kind_rank == 1
        statement = statement.where(
            or_(latest_filter, Job.id.in_(include_ids))
            if include_ids
            else latest_filter
        )
    else:
        statement = statement.where(Job.id.in_(include_ids))
    return list(db_session.scalars(statement).all())


DUBBING_STAGES = (
    StageDefinition(
        "transcribe",
        "Transcribe",
        "Create timed source-language subtitles from media.",
        prerequisite_roles=("upload",),
        output_role="transcription",
        job_kind="dubbing.transcribe",
    ),
    StageDefinition(
        "correct",
        "Correct",
        "Review punctuation, wording, merges, and splits without translating.",
        prerequisite_roles=("transcription", "upload"),
        output_role="correction",
        job_kind="dubbing.correct",
    ),
    StageDefinition(
        "translate",
        "Translate",
        "Create a separate target-language subtitle artifact.",
        prerequisite_roles=("correction", "transcription", "upload"),
        output_role="translation",
        job_kind="dubbing.translate",
    ),
    StageDefinition(
        "optimize_document",
        "Optimize subtitles before generation",
        "Optionally create a separate, reviewable speech-optimized revision before audio generation. This is useful when the LLM and TTS must not share limited GPU memory.",
        prerequisite_roles=("translation", "correction", "transcription", "upload"),
        output_role="tts_optimized",
        job_kind="text.optimize_tts",
    ),
    StageDefinition(
        "optimize_tts",
        "Optimize text for speech",
        "Choose document-level speech optimization or optimize final speech units while preparing a reviewable speech plan.",
        executable=False,
        prerequisite_roles=("translation", "correction", "transcription", "upload"),
    ),
    StageDefinition(
        "preview",
        "Preview",
        "Compare source, correction, and translation with recorded lineage.",
        executable=False,
        prerequisite_roles=("translation", "correction", "transcription", "upload"),
    ),
    StageDefinition(
        "generate_audio",
        "Generate audio",
        "Record the selected speech plan with the current voices and delivery settings. Review the takes before assembly.",
        prerequisite_roles=("translation", "correction", "transcription", "upload"),
        job_kind="dubbing.generate_audio",
    ),
    StageDefinition(
        "export",
        "Export",
        "Package selected takes, assembled audio, subtitle tracks, or a rendered video.",
        prerequisite_roles=(
            "assembled_audio",
            "dubbing_audio",
            "translation",
            "correction",
            "transcription",
            "upload",
        ),
        output_role="export",
        job_kind="export.create",
    ),
)

SUBTITLE_STAGES = (
    StageDefinition(
        "transcribe",
        "Transcribe",
        "Create timed source-language subtitles from media.",
        prerequisite_roles=("upload",),
        output_role="transcription",
        job_kind="dubbing.transcribe",
    ),
    StageDefinition(
        "correct",
        "Correct",
        "Review punctuation, wording, merges, and splits without translating.",
        prerequisite_roles=("transcription", "upload"),
        output_role="correction",
        job_kind="dubbing.correct",
    ),
    StageDefinition(
        "translate",
        "Translate",
        "Create a separate target-language subtitle artifact.",
        prerequisite_roles=("correction", "transcription", "upload"),
        output_role="translation",
        job_kind="dubbing.translate",
    ),
    StageDefinition(
        "preview",
        "Preview",
        "Compare source, correction, and translation with recorded lineage.",
        executable=False,
        prerequisite_roles=("translation", "correction", "transcription", "upload"),
    ),
    StageDefinition(
        "export",
        "Export subtitles",
        "Save the selected cues as SRT or VTT, or concatenate them into a plain-text transcript.",
        prerequisite_roles=("translation", "correction", "transcription", "upload"),
        output_role="export",
        job_kind="export.create",
    ),
)

MEDIA_EDIT_STAGES = (
    StageDefinition(
        "transcribe",
        "Transcript or caption alignment",
        "Generate a word-timed transcript when captions are absent, or align attached captions directly while preserving their wording and speakers.",
        prerequisite_roles=("upload",),
        output_role="transcription",
        job_kind="dubbing.transcribe",
    ),
    StageDefinition(
        "edit_media",
        "Review and render edit",
        "Use the transcript-guided editor to propose removals, audit exact boundaries, and render a reversible edit revision.",
        executable=False,
        output_role="media_edit_subtitles",
    ),
    StageDefinition(
        "correct",
        "Correct edited subtitles",
        "Review punctuation, wording, merges, and splits after the timeline has been retimed.",
        prerequisite_roles=("media_edit_subtitles",),
        output_role="correction",
        job_kind="dubbing.correct",
    ),
    StageDefinition(
        "translate",
        "Translate",
        "Translate the retimed edited subtitles into a separate target-language artifact.",
        prerequisite_roles=("correction", "media_edit_subtitles"),
        output_role="translation",
        job_kind="dubbing.translate",
    ),
    StageDefinition(
        "optimize_document",
        "Optimize subtitles before generation",
        "Optionally create a separate, reviewable speech-optimized revision before voice generation.",
        prerequisite_roles=(
            "translation",
            "correction",
            "media_edit_subtitles",
        ),
        output_role="tts_optimized",
        job_kind="text.optimize_tts",
    ),
    StageDefinition(
        "optimize_tts",
        "Optimize text for speech",
        "Choose whether speech optimization happens before or during optional voice generation.",
        executable=False,
        prerequisite_roles=(
            "translation",
            "correction",
            "media_edit_subtitles",
        ),
    ),
    StageDefinition(
        "preview",
        "Preview",
        "Compare the edited source subtitles, correction, and translation with recorded lineage.",
        executable=False,
        prerequisite_roles=(
            "translation",
            "correction",
            "media_edit_subtitles",
        ),
    ),
    StageDefinition(
        "generate_audio",
        "Generate voiceover",
        "Create reviewable per-segment takes from the retimed edited subtitles or a later text revision.",
        prerequisite_roles=(
            "translation",
            "correction",
            "media_edit_subtitles",
        ),
        job_kind="dubbing.generate_audio",
    ),
    StageDefinition(
        "export",
        "Export",
        "Package the rendered edit with optional corrected, translated, or generated tracks.",
        prerequisite_roles=(
            "media_edit_media",
            "assembled_audio",
            "dubbing_audio",
            "translation",
            "correction",
            "media_edit_subtitles",
        ),
        output_role="export",
        job_kind="export.create",
    ),
)

AUDIOBOOK_STAGES = (
    StageDefinition(
        "clean_source",
        "Clean source",
        "Review deterministic extraction and optional agentic cleanup.",
        prerequisite_roles=("upload",),
        output_role="clean_text",
        job_kind="source.clean",
    ),
    StageDefinition(
        "prepare_text",
        "Segment narration",
        "Create editable generation segments from the cleaned document. This controls text boundaries and pauses, not the TTS model.",
        prerequisite_roles=("clean_text",),
        output_role="prepared_text",
        job_kind="text.prepare",
    ),
    StageDefinition(
        "optimize_document",
        "Optimize narration before generation",
        "Optionally create a separate before-and-after narration revision for review before any audio is generated.",
        prerequisite_roles=("prepared_text",),
        output_role="tts_optimized",
        job_kind="text.optimize_tts",
    ),
    StageDefinition(
        "optimize_tts",
        "Optimize text for speech",
        "Choose document-level speech optimization or optimize final speech units while preparing a reviewable speech plan.",
        executable=False,
        prerequisite_roles=("prepared_text",),
    ),
    StageDefinition(
        "generate_audio",
        "Generate audio",
        "Run missing document preparation, then create reviewable narration takes from editable segments. Assembly remains manual.",
        prerequisite_roles=("prepared_text", "clean_text", "upload"),
        job_kind="audiobook.generate_audio",
    ),
    StageDefinition(
        "export",
        "Export",
        "Package the assembled audio with the selected format, metadata, and cover.",
        prerequisite_roles=("assembled_audio", "audiobook_audio"),
        output_role="export",
        job_kind="export.create",
    ),
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


class GenerationPreflightError(ValueError):
    """A generation request needs language or speech-plan preparation first."""


class WorkflowService:
    def __init__(self, database: Database, jobs: JobQueue):
        self.database = database
        self.jobs = jobs

    def definitions(
        self, record: SessionRecord, artifacts: list[Artifact] | None = None
    ) -> tuple[StageDefinition, ...]:
        if record.workflow_kind == "audiobook":
            return AUDIOBOOK_STAGES
        definitions: tuple[StageDefinition, ...]
        if record.workflow_kind == "subtitles":
            definitions = SUBTITLE_STAGES
        elif record.workflow_kind == "media_edit":
            definitions = MEDIA_EDIT_STAGES
        else:
            definitions = DUBBING_STAGES
        upload = next(
            (
                item
                for item in (artifacts or [])
                if item.role == "upload" and item.state == "current"
            ),
            None,
        )
        filename = (
            str(
                (upload.metadata_json or {}).get("original_filename")
                or upload.relative_path
            ).lower()
            if upload
            else ""
        )
        return tuple(
            item
            for item in definitions
            if not (filename.endswith((".srt", ".vtt")) and item.key == "transcribe")
        )

    @staticmethod
    def _matches_active_media_edit_revision(
        artifact: Artifact,
        active_revision: MediaEditPlanRevision | None,
    ) -> bool:
        if artifact.role not in {"media_edit_media", "media_edit_subtitles"}:
            return True
        if active_revision is None:
            return False
        metadata = (
            artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
        )
        content_matches = bool(
            str(metadata.get("content_hash") or "") == active_revision.content_hash
        )
        explicit_revision_id = str(
            metadata.get("media_edit_revision_id") or ""
        )
        if explicit_revision_id:
            return bool(explicit_revision_id == active_revision.id and content_matches)
        if str(metadata.get("revision_id") or "") == active_revision.id:
            # Compatibility with render artifacts created before the dedicated
            # media-edit identity field existed.
            return content_matches
        # Subtitle materialization assigns the generic revision_id to its
        # DocumentRevision. Older in-flight artifacts still carry enough
        # immutable plan identity to recognize the exact edit revision.
        try:
            revision_number = int(metadata.get("revision"))
        except (TypeError, ValueError):
            return False
        return bool(
            str(metadata.get("plan_id") or "") == active_revision.plan_id
            and revision_number == active_revision.revision_number
            and content_matches
        )

    @staticmethod
    def _usable_input(
        definition: StageDefinition, artifact: Artifact, workflow_kind: str
    ) -> bool:
        if artifact.role != "upload":
            # The caller has already selected this artifact through the exact
            # outcome-resolved role list (which may be narrower or newer than
            # the definition's broad compatibility roles).
            return True
        filename = str(
            (artifact.metadata_json or {}).get("original_filename")
            or artifact.relative_path
        ).lower()
        extension = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
        if definition.key == "transcribe":
            return extension in {
                ".mp3",
                ".wav",
                ".flac",
                ".m4a",
                ".aac",
                ".ogg",
                ".opus",
                ".mp4",
                ".mkv",
                ".mov",
                ".avi",
                ".webm",
                ".m4v",
            }
        if definition.key in {
            "correct",
            "translate",
            "optimize_tts",
            "optimize_document",
            "preview",
        }:
            return extension == ".srt"
        if definition.key == "clean_source":
            return extension in {".txt", ".pdf", ".epub", ".docx", ".mobi"}
        if definition.key == "generate_audio":
            if workflow_kind == "audiobook":
                return extension in {
                    ".json",
                    ".txt",
                    ".md",
                    ".pdf",
                    ".epub",
                    ".docx",
                    ".mobi",
                }
            return extension == ".srt"
        return True

    def _valid_translation_source(
        self,
        definition: StageDefinition,
        artifact: Artifact | None,
        workflow_kind: str,
        session_id: str,
        attached_source_ids: set[str],
    ) -> bool:
        return bool(
            artifact is not None
            and artifact.state != "deleted"
            and artifact.role
            in {"media_edit_subtitles", "transcription", "correction", "upload"}
            and (
                artifact.session_id == session_id or artifact.id in attached_source_ids
            )
            and self._usable_input(definition, artifact, workflow_kind)
        )

    def snapshot(self, session_id: str) -> dict[str, Any]:
        context = WorkflowSnapshotContext(
            database=self.database,
            definitions=lambda record, artifacts: self.definitions(record, artifacts),
            matches_active_media_edit_revision=lambda artifact, active_revision: (
                self._matches_active_media_edit_revision(artifact, active_revision)
            ),
            usable_input=lambda definition, artifact, workflow_kind: self._usable_input(
                definition, artifact, workflow_kind
            ),
            valid_translation_source=lambda definition, artifact, workflow_kind, session_id, attached_source_ids: (
                self._valid_translation_source(
                    definition, artifact, workflow_kind, session_id, attached_source_ids
                )
            ),
            resolve_primary_source=lambda session, session_id: resolve_primary_source(
                session, session_id
            ),
            current_artifacts_by_role=lambda session, session_id, roles: (
                _latest_current_artifacts_by_role(session, session_id, roles)
            ),
            canonical_stage_key=lambda stage_key: canonical_stage_key(stage_key),
            stage_output_roles=lambda: STAGE_OUTPUT_ROLES,
            stage_histories=lambda session, session_id, stage_keys, *, limit: stage_histories(
                session, session_id, stage_keys, limit=limit
            ),
            history_preview_limit=lambda: WORKFLOW_HISTORY_PREVIEW_LIMIT,
            latest_jobs_by_kind=lambda session, session_id, kinds, *, include_ids: (
                _latest_jobs_by_kind(session, session_id, kinds, include_ids=include_ids)
            ),
            workflow_transformations=lambda session, session_id, outcome, database: (
                workflow_transformations(session, session_id, outcome, database)
            ),
            attached_source_artifact_ids=lambda session, session_id: _attached_source_artifact_ids(
                session, session_id
            ),
            correction_output_descends_from=lambda session, *, ancestor, output, session_id: (
                _correction_output_descends_from(
                    session, ancestor=ancestor, output=output, session_id=session_id
                )
            ),
            job_run_metrics=lambda job: _job_run_metrics(job),
            subtitle_source_status_in_session=lambda session, session_id, *, primary: (
                subtitle_source_status_in_session(session, session_id, primary=primary)
            ),
        )
        return build_workflow_snapshot(context, session_id)

    def resolve_stage(
        self,
        session_id: str,
        stage_key: str,
        settings: dict[str, Any] | None = None,
        *,
        continuation: bool = False,
    ) -> ResolvedWorkflowStage:
        """Capture coherent queue input; later edits affect only future runs."""
        with self.database.snapshot_session() as session:
            return self._resolve_stage_in_session(
                session, session_id, stage_key, settings, continuation=continuation,
            )

    def _resolve_stage_in_session(
        self,
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
        resolved, settings_hash = WorkspaceSettingsService(self.database).resolve_in_session(
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
        primary_source = resolve_primary_source(session, session_id)
        attached_sources = (
            [primary_source.artifact] if primary_source.artifact else []
        )
        attached_ids = {artifact.id for artifact in attached_sources}
        attached_source_ids = _attached_source_artifact_ids(session, session_id)
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
                for item in self.definitions(record, all_artifacts)
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
        transformations = workflow_transformations(session, session_id, outcome, self.database)
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
        selections = selected_artifacts(session, session_id, all_artifacts)
        by_role: dict[str, Artifact] = {}
        for selected in selections.values():
            if self._matches_active_media_edit_revision(
                selected, active_media_edit_revision
            ):
                by_role.setdefault(selected.role, selected)
        for attached in attached_sources:
            by_role.setdefault("upload", attached)
        for candidate in all_artifacts:
            if (
                candidate.state == "current"
                and self._matches_active_media_edit_revision(
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
                or not self._usable_input(
                    definition, requested, record.workflow_kind
                )
                or not self._matches_active_media_edit_revision(
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
                    and self._usable_input(
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
            deferred_export_assembly = export_requires_generation_assembly(
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

            subtitle_snapshot = WorkspaceSettingsService(self.database).get_in_session(
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
            payload["display_subtitle_snapshot"] = capture_display_subtitle_snapshot(
                session, session_id, flattened
            )
            export_source = resolve_media_source(session, session_id)
            # Converting an edited recording to voiceover retains its cut
            # timeline. Export must keep using the matching render.
            if record.workflow_kind == "media_edit" or (
                record.workflow_kind == "voiceover"
                and media_edit_plan is not None
                and normalize_export_mode(
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
                export_source = PrimarySourceResolution(
                    artifact=edited_media,
                    source_asset=None,
                    attachment=None,
                    profile=classify_source(
                        name=edited_name,
                        kind=edited_kind,
                        mime_type=edited_mime,
                    ),
                    name=edited_name,
                    kind=edited_kind,
                    mime_type=edited_mime,
                    resolution="derived_media_edit",
                )
            payload["export_contract"] = build_export_contract(
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
            resource_keys = self._resource_keys(session_id, stage_key, flattened)
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
                self.validate_generation_language_payload(session, payload)
            return ResolvedWorkflowStage(
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
        return ResolvedWorkflowStage(
            job_kind=job_kind,
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

    @staticmethod
    def _generation_language_for_source(
        session: Session,
        session_id: str,
        source: Artifact,
        settings: dict[str, Any],
    ) -> str:
        """Mirror the worker's source-aware language choice without loading media."""

        def usable(value: Any) -> str:
            language = str(value or "").strip()
            return "" if language.casefold() in {"", "auto", "und", "unknown"} else language

        metadata = source.metadata_json if isinstance(source.metadata_json, dict) else {}
        language = usable(metadata.get("language"))
        role = str(source.role or "")
        if not language and role == "tts_optimized":
            parent_id = str(metadata.get("source_artifact_id") or "")
            parent = session.get(Artifact, parent_id) if parent_id else None
            if parent is not None:
                parent_metadata = (
                    parent.metadata_json
                    if isinstance(parent.metadata_json, dict)
                    else {}
                )
                language = usable(parent_metadata.get("language"))
                role = str(parent.role or role)

        record = session.get(SessionRecord, session_id)
        if not language and role == "translation":
            language = usable(record.target_language if record else None)
        if not language and role in {
            "transcription",
            "correction",
            "upload",
            "prepared_text",
            "source",
        }:
            language = usable(record.source_language if record else None)
        if not language:
            language = usable(settings.get("language") or settings.get("target_language"))
        return language or "en"

    def validate_generation_language_payload(
        self, session: Session, payload: dict[str, Any]
    ) -> None:
        """Preflight the exact selected language/model before a workflow enqueue.

        A pinned speech revision is checked through the same freezer used by the
        synthesis worker, but all settings are copied and the caller only reads
        database state. Unpinned multi-voice requests must first be prepared and
        reviewed because an assignment can select a different provider/model.
        """
        if str(payload.get("target_stage") or "") != "generate_audio":
            return

        settings = deepcopy(payload.get("settings") or {})
        if not isinstance(settings, dict):
            settings = {}
        top_level_revision_id = str(
            payload.get("speech_plan_revision_id") or ""
        ).strip()
        settings_revision_id = str(
            settings.get("speech_plan_revision_id") or ""
        ).strip()
        if (
            top_level_revision_id
            and settings_revision_id
            and top_level_revision_id != settings_revision_id
        ):
            raise GenerationPreflightError(
                "The requested speech plan revision conflicts with the current selected revision."
            )
        revision_id = top_level_revision_id or settings_revision_id
        input_selected = bool(payload.get("_tts_language_preflight_input_selected"))
        if revision_id and not input_selected:
            raise GenerationPreflightError(
                "Select or prepare the generation input before using an explicit speech plan revision."
            )
        if not input_selected and not revision_id:
            return

        source_id = str(payload.get("source_artifact_id") or "")
        source = session.get(Artifact, source_id) if source_id else None
        if source is None or not input_selected:
            raise GenerationPreflightError(
                "Select or prepare the narration input before validating audio generation."
            )
        session_id = str(payload.get("session_id") or "")
        language = self._generation_language_for_source(
            session, session_id, source, settings
        )
        settings.update(language=language, target_language=language)

        resolved = payload.get("resolved_settings_snapshot")
        snapshot = deepcopy(resolved) if isinstance(resolved, dict) else {}
        safe_settings = deepcopy(settings)
        for key in (
            "audio_cpp_voice_ref",
            "audio_cpp_voice_ref_hash",
            "audio_cpp_reference_text",
        ):
            safe_settings.pop(key, None)
        snapshot["tts"] = {
            **dict(snapshot.get("tts") or {}),
            **safe_settings,
        }
        snapshot["audio"] = {
            **dict(snapshot.get("audio") or {}),
            **safe_settings,
        }
        snapshot["text"] = {
            **dict(snapshot.get("text") or {}),
            "llm_tts_optimization": bool(settings.get("llm_tts_optimization")),
            "apply_reviewed_pronunciations": settings.get(
                "apply_reviewed_pronunciations", True
            ),
            "use_existing_speech_plans": source.role == "tts_optimized",
        }
        snapshot["source_artifact_id"] = source.id

        if revision_id:
            plan = session.scalar(
                select(GenerationPlan).where(
                    GenerationPlan.session_id == session_id
                )
            )
            revision = session.get(GenerationPlanRevision, revision_id)
            if (
                plan is None
                or revision is None
                or revision.plan_id != plan.id
            ):
                raise GenerationPreflightError(
                    "The requested speech plan revision is not available in this session."
                )
            if plan.active_revision_id != revision_id:
                raise GenerationPreflightError(
                    "The requested speech plan revision is no longer current. Refresh and select the current revision."
                )
            revision_settings = (
                revision.settings_json
                if isinstance(revision.settings_json, dict)
                else {}
            )
            planned_source_id = str(
                revision_settings.get("_source_artifact_id") or ""
            )
            if planned_source_id and planned_source_id != source.id:
                raise GenerationPreflightError(
                    "The requested speech plan revision belongs to a different generation input. Select the matching input or prepare a new speech plan."
                )
            snapshot["speech_plan_revision_id"] = revision_id
            from .speech_plan_workspace import freeze_speech_snapshot

            try:
                freeze_speech_snapshot(
                    session,
                    revision_id,
                    snapshot,
                    explicit=True,
                )
            except ValueError as error:
                raise GenerationPreflightError(str(error)) from error
            return

        effective_tts = {
            **dict(snapshot.get("audio") or {}),
            **dict(snapshot.get("tts") or {}),
        }
        from .generation_rendering import is_strict_single_voice

        has_route_changing_voice = bool(effective_tts.get("casting_enabled"))
        plan = session.scalar(
            select(GenerationPlan).where(
                GenerationPlan.session_id == str(payload.get("session_id") or "")
            )
        )
        active_revision_id = str(plan.active_revision_id or "") if plan else ""
        if active_revision_id and not is_strict_single_voice(effective_tts):
            has_route_changing_voice = has_route_changing_voice or (
                session.scalar(
                    select(GenerationSegment.id)
                    .where(
                        GenerationSegment.plan_revision_id == active_revision_id,
                        GenerationSegment.removed.is_(False),
                        or_(
                            GenerationSegment.voice_id.is_not(None),
                            GenerationSegment.voice.is_not(None),
                        ),
                    )
                    .limit(1)
                )
                is not None
            )
        if has_route_changing_voice:
            raise GenerationPreflightError(
                "Prepare and review the speech plan before generation; an unpinned "
                "voice assignment may use a different TTS model."
            )

        from pandrator.logic.tts_language_preflight import validate_tts_language

        try:
            validate_tts_language(effective_tts)
        except ValueError as error:
            raise GenerationPreflightError(str(error)) from error

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

    def decide_video_tail(self, job_id: str, action: str) -> Job:
        """Stop, or continue the captured export after its duration warning.

        The decision and continuation are committed together, so repeated clicks
        cannot enqueue duplicate exports or pick up subsequently edited settings.
        """
        if action not in ("stop", "extend"):
            raise ValueError("Choose stop or extend for the video duration warning.")
        with self.database.immediate_session() as session:
            previous = session.get(Job, job_id)
            if previous is None:
                raise KeyError(job_id)
            if (
                previous.kind not in {"export.create", "export.variant"}
                or previous.status != "failed"
                or previous.error_code != "VideoTailExtensionRequired"
            ):
                raise ValueError("This export is not waiting for a video duration decision.")
            result = dict(previous.result_json or {})
            decision = result.get("video_tail_decision")
            if decision and decision != action:
                raise ValueError("This video duration warning has already been handled.")
            continued_id = result.get("continuation_job_id")
            if continued_id:
                continued = session.get(Job, continued_id)
                if continued is None:
                    raise ValueError("The continued export is no longer available.")
                session.expunge(continued)
                return continued
            result["video_tail_decision"] = action
            if action == "stop":
                previous.result_json = result
                session.flush()
                session.expunge(previous)
                return previous
            payload = deepcopy(previous.payload_json or {})
            payload.setdefault("settings", {})["video_tail_extension_policy"] = "extend"
            snapshot = payload.get("resolved_settings_snapshot")
            if isinstance(snapshot, dict):
                snapshot.setdefault("output", {})["video_tail_extension_policy"] = "extend"
            continued = self.jobs.enqueue_in_session(
                session,
                previous.kind,
                payload,
                session_id=previous.session_id,
                resource_keys=list(previous.resource_keys_json or []),
            )
            result["continuation_job_id"] = continued.id
            previous.result_json = result
            session.flush()
            session.expunge(continued)
            return continued

    @staticmethod
    def _resource_keys(
        session_id: str, stage_key: str, settings: dict[str, Any]
    ) -> list[str]:
        keys = [f"session:{session_id}"]
        if stage_key in {
            "correct",
            "translate",
            "optimize_tts",
            "optimize_document",
        } or (stage_key == "clean_source" and bool(settings.get("agentic", False))):
            keys.append("service:llm")
        if stage_key == "generate_audio":
            service = str(settings.get("service") or "tts").lower().replace(" ", "_")
            keys.append(f"service:tts:{service}")
        if stage_key == "transcribe":
            from .stt_resources import stt_resource_keys

            return keys + stt_resource_keys(settings)
        compute = str(
            settings.get("compute_backend") or settings.get("device") or "auto"
        ).lower()
        if compute in {"cuda", "vulkan", "metal", "gpu"}:
            keys.append(f"gpu:{compute}")
        return keys
