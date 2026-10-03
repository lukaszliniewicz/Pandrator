"""Source-aware workflow snapshots and prerequisite-safe stage queuing."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
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
    GenerationSegment,
    Job,
    MediaEditPlanRevision,
    SessionRecord,
    SessionSource,
    SourceAsset,
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
from .workflow_resolution import (
    WorkflowResolutionContext,
    resolve_workflow_stage_in_session,
)
from .workflow_resolution_types import ResolvedWorkflowStage as ResolvedWorkflowStage
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
        context = WorkflowResolutionContext(
            database=lambda: self.database,
            definitions=lambda record, artifacts: self.definitions(record, artifacts),
            matches_active_media_edit_revision=lambda artifact, active_revision: (
                self._matches_active_media_edit_revision(artifact, active_revision)
            ),
            usable_input=lambda definition, artifact, workflow_kind: self._usable_input(
                definition, artifact, workflow_kind
            ),
            resource_keys=lambda session_id, stage_key, settings: self._resource_keys(
                session_id, stage_key, settings
            ),
            validate_generation_language_payload=lambda session, payload: (
                self.validate_generation_language_payload(session, payload)
            ),
            selected_artifacts=lambda session, session_id, artifacts: selected_artifacts(
                session, session_id, artifacts
            ),
            resolve_primary_source=lambda session, session_id: resolve_primary_source(
                session, session_id
            ),
            attached_source_artifact_ids=lambda session, session_id: _attached_source_artifact_ids(
                session, session_id
            ),
            workflow_transformations=lambda session, session_id, outcome, database: (
                workflow_transformations(session, session_id, outcome, database)
            ),
            export_requires_generation_assembly=lambda *, workflow_kind, settings: (
                export_requires_generation_assembly(
                    workflow_kind=workflow_kind, settings=settings
                )
            ),
            capture_display_subtitle_snapshot=lambda session, session_id, settings: (
                capture_display_subtitle_snapshot(session, session_id, settings)
            ),
            resolve_media_source=lambda session, session_id: resolve_media_source(
                session, session_id
            ),
            normalize_export_mode=lambda value, *, workflow_kind: normalize_export_mode(
                value, workflow_kind=workflow_kind
            ),
            classify_source=lambda *, name, kind, mime_type: classify_source(
                name=name, kind=kind, mime_type=mime_type
            ),
            build_export_contract=lambda *, workflow_kind, settings, source: (
                build_export_contract(
                    workflow_kind=workflow_kind, settings=settings, source=source
                )
            ),
            primary_source_type=lambda: PrimarySourceResolution,
            resolved_stage_type=lambda: ResolvedWorkflowStage,
        )
        return resolve_workflow_stage_in_session(
            context, session, session_id, stage_key, settings, continuation=continuation
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
