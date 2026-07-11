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

    def definitions(self, record: SessionRecord, artifacts: list[Artifact] | None = None) -> tuple[StageDefinition, ...]:
        if record.workflow_kind == "audiobook":
            return AUDIOBOOK_STAGES
        upload = next((item for item in (artifacts or []) if item.role == "upload" and item.state == "current"), None)
        filename = str((upload.metadata_json or {}).get("original_filename") or upload.relative_path).lower() if upload else ""
        return tuple(item for item in DUBBING_STAGES if not (filename.endswith(".srt") and item.key == "transcribe"))

    @staticmethod
    def _usable_input(definition: StageDefinition, artifact: Artifact, workflow_kind: str) -> bool:
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
            return extension in ({".txt", ".json"} if workflow_kind == "audiobook" else {".srt"})
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
            latest_jobs = list(
                session.scalars(
                    select(Job).where(Job.session_id == session_id).order_by(Job.created_at.desc())
                ).all()
            )
            roles = {artifact.role: artifact for artifact in artifacts if artifact.state == "current"}
            latest_roles = {}
            for artifact_record in artifacts:
                latest_roles.setdefault(artifact_record.role, artifact_record)
            job_by_kind = {}
            for job in latest_jobs:
                job_by_kind.setdefault(job.kind, job)
            if "workflow.continue" in job_by_kind:
                job_by_kind["dubbing.generate_audio"] = job_by_kind["workflow.continue"]
                job_by_kind["audiobook.generate_audio"] = job_by_kind["workflow.continue"]
            stages = []
            for index, definition in enumerate(self.definitions(record, artifacts), start=1):
                artifact = latest_roles.get(definition.output_role or "")
                active = job_by_kind.get(definition.job_kind or "")
                prerequisite = next(
                    (
                        roles[role]
                        for role in definition.prerequisite_roles
                        if role in roles and self._usable_input(definition, roles[role], record.workflow_kind)
                    ),
                    None,
                )
                if active and active.status in {"queued", "running", "cancel_requested"}:
                    status = "running"
                elif active and active.status in {"failed", "interrupted"} and (artifact is None or active.created_at >= artifact.updated_at):
                    status = "failed"
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
                        "required": definition.key == "transcribe" and any(key in record.included_stages_json for key in ("correct", "translate", "generate_audio")),
                        "artifact": {"id": artifact.id, "role": artifact.role, "path": artifact.relative_path} if artifact else None,
                        "job_id": active.id if active else None,
                        "progress": active.progress if active and status in {"running", "failed"} else None,
                        "detail": active.error_message if active and status == "failed" else None,
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
            all_artifacts = list(session.scalars(select(Artifact).where(Artifact.session_id == session_id).order_by(Artifact.created_at.desc())).all())
            definition = next((item for item in self.definitions(record, all_artifacts) if item.key == stage_key), None)
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
            by_role = {artifact.role: artifact for artifact in artifacts}
            source = next(
                (
                    by_role[role]
                    for role in definition.prerequisite_roles
                    if role in by_role and self._usable_input(definition, by_role[role], record.workflow_kind)
                ),
                None,
            )
            if definition.prerequisite_roles and source is None:
                raise ValueError(f"Stage '{stage_key}' is missing a required input artifact.")
            payload = {
                "session_id": session_id,
                "source_artifact_id": source.id if source else None,
                "settings": settings or {},
            }
        if stage_key == "generate_audio":
            stage_settings = payload["settings"].pop("stage_settings", {})
            payload.update({"target_stage": stage_key, "stage_settings": stage_settings})
            return self.jobs.enqueue("workflow.continue", payload, session_id=session_id)
        return self.jobs.enqueue(definition.job_kind, payload, session_id=session_id)
