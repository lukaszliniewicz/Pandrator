"""Source-aware workflow snapshots and prerequisite-safe stage queuing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from .database import Database
from .jobs import JobQueue
from .models import Artifact, Job, SessionRecord


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
    StageDefinition("preview", "Preview", "Compare source, correction, and translation with recorded lineage.", executable=False, prerequisite_roles=("translation", "correction", "transcription", "upload")),
    StageDefinition("generate_audio", "Generate audio", "Synthesize missing or stale included prerequisites, then create speech.", prerequisite_roles=("translation", "correction", "transcription", "upload"), output_role="dubbing_audio", job_kind="dubbing.generate_audio"),
    StageDefinition("export", "Export", "Package audio, subtitle tracks, or a rendered video.", prerequisite_roles=("dubbing_audio", "translation", "correction", "transcription", "upload"), output_role="export", job_kind="export.create"),
)

AUDIOBOOK_STAGES = (
    StageDefinition("clean_source", "Clean source", "Review deterministic extraction and optional agentic cleanup.", prerequisite_roles=("upload",), output_role="clean_text", job_kind="source.clean"),
    StageDefinition("prepare_text", "Prepare narration", "Segment and optimize text for the selected speech service.", prerequisite_roles=("clean_text", "upload"), output_role="prepared_text", job_kind="text.prepare"),
    StageDefinition("generate_audio", "Generate audio", "Generate or resume narration from prepared segments.", prerequisite_roles=("prepared_text", "clean_text", "upload"), output_role="audiobook_audio", job_kind="audiobook.generate_audio"),
    StageDefinition("export", "Export", "Assemble the selected audio format, metadata, and cover.", prerequisite_roles=("audiobook_audio",), output_role="export", job_kind="export.create"),
)


class WorkflowService:
    def __init__(self, database: Database, jobs: JobQueue):
        self.database = database
        self.jobs = jobs

    def definitions(self, record: SessionRecord) -> tuple[StageDefinition, ...]:
        return AUDIOBOOK_STAGES if record.workflow_kind == "audiobook" else DUBBING_STAGES

    @staticmethod
    def _usable_input(definition: StageDefinition, artifact: Artifact) -> bool:
        if artifact.role != "upload":
            return artifact.role in definition.prerequisite_roles
        filename = str((artifact.metadata_json or {}).get("original_filename") or artifact.relative_path).lower()
        extension = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
        if definition.key == "transcribe":
            return extension in {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
        if definition.key in {"correct", "translate"}:
            return extension == ".srt"
        if definition.key == "clean_source":
            return extension in {".txt", ".pdf", ".epub", ".docx", ".mobi"}
        if definition.key == "generate_audio":
            return extension in {".srt", ".txt", ".pdf", ".epub", ".docx", ".mobi"}
        return True

    def snapshot(self, session_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            artifacts = list(
                session.scalars(
                    select(Artifact).where(Artifact.session_id == session_id).order_by(Artifact.created_at.desc())
                ).all()
            )
            active_jobs = list(
                session.scalars(
                    select(Job).where(Job.session_id == session_id, Job.status.in_(["queued", "running", "cancel_requested"]))
                ).all()
            )
            roles = {artifact.role: artifact for artifact in artifacts if artifact.state == "current"}
            job_by_kind = {job.kind: job for job in active_jobs}
            stages = []
            for index, definition in enumerate(self.definitions(record), start=1):
                artifact = roles.get(definition.output_role or "")
                active = job_by_kind.get(definition.job_kind or "")
                prerequisite = next(
                    (
                        roles[role]
                        for role in definition.prerequisite_roles
                        if role in roles and self._usable_input(definition, roles[role])
                    ),
                    None,
                )
                if active:
                    status = "running"
                elif artifact:
                    status = "stale" if artifact.state == "stale" else "completed"
                elif definition.prerequisite_roles and prerequisite is None:
                    status = "unavailable"
                else:
                    status = "ready"
                stages.append(
                    {
                        "number": index,
                        "key": definition.key,
                        "title": definition.title,
                        "explanation": definition.explanation,
                        "status": status,
                        "executable": definition.executable,
                        "included": definition.key in record.included_stages_json,
                        "artifact": {"id": artifact.id, "role": artifact.role, "path": artifact.relative_path} if artifact else None,
                        "job_id": active.id if active else None,
                    }
                )
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
                    for artifact in artifacts
                    if artifact.role == "upload" and artifact.state == "current"
                ],
            }

    def run_stage(self, session_id: str, stage_key: str, settings: dict[str, Any] | None = None) -> Job:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            definition = next((item for item in self.definitions(record) if item.key == stage_key), None)
            if definition is None or not definition.executable or not definition.job_kind:
                raise ValueError(f"Stage '{stage_key}' cannot be run directly.")
            artifacts = list(
                session.scalars(
                    select(Artifact).where(
                        Artifact.session_id == session_id,
                        Artifact.state == "current",
                        Artifact.role.in_(definition.prerequisite_roles),
                    ).order_by(Artifact.created_at.desc())
                ).all()
            )
            source = next((artifact for artifact in artifacts if self._usable_input(definition, artifact)), None)
            if definition.prerequisite_roles and source is None:
                raise ValueError(f"Stage '{stage_key}' is missing a required input artifact.")
            payload = {
                "session_id": session_id,
                "source_artifact_id": source.id if source else None,
                "settings": settings or {},
            }
        return self.jobs.enqueue(definition.job_kind, payload, session_id=session_id)
