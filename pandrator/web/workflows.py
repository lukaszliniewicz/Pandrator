"""Source-aware workflow snapshots and prerequisite-safe stage queuing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from .database import Database
from .jobs import JobQueue
from .models import Artifact, Job, OutcomePlan, SessionRecord


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
    StageDefinition("optimize_tts", "Optimize for speech", "Optionally rewrite pronunciation-sensitive text for TTS without changing subtitle timing.", prerequisite_roles=("translation", "correction", "transcription", "upload"), output_role="tts_optimized", job_kind="text.optimize_tts"),
    StageDefinition("preview", "Preview", "Compare source, correction, and translation with recorded lineage.", executable=False, prerequisite_roles=("translation", "correction", "transcription", "upload")),
    StageDefinition("generate_audio", "Generate audio", "Synthesize missing or stale included prerequisites, then create speech.", prerequisite_roles=("tts_optimized", "translation", "correction", "transcription", "upload"), output_role="dubbing_audio", job_kind="dubbing.generate_audio"),
    StageDefinition("export", "Export", "Package audio, subtitle tracks, or a rendered video.", prerequisite_roles=("dubbing_audio", "translation", "correction", "transcription", "upload"), output_role="export", job_kind="export.create"),
)

AUDIOBOOK_STAGES = (
    StageDefinition("clean_source", "Clean source", "Review deterministic extraction and optional agentic cleanup.", prerequisite_roles=("upload",), output_role="clean_text", job_kind="source.clean"),
    StageDefinition("prepare_text", "Prepare narration", "Segment and optimize text for the selected speech service.", prerequisite_roles=("clean_text", "upload"), output_role="prepared_text", job_kind="text.prepare"),
    StageDefinition("optimize_tts", "Optimize for speech", "Optionally rewrite pronunciation-sensitive narration and keep a before/after artifact.", prerequisite_roles=("prepared_text", "clean_text", "upload"), output_role="tts_optimized", job_kind="text.optimize_tts"),
    StageDefinition("generate_audio", "Generate audio", "Generate or resume narration from prepared segments.", prerequisite_roles=("tts_optimized", "prepared_text", "clean_text", "upload"), output_role="audiobook_audio", job_kind="audiobook.generate_audio"),
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
        if definition.key in {"correct", "translate", "optimize_tts"}:
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
        # Resolve persisted defaults before the run is enqueued.  The resulting
        # snapshot is immutable job input: later settings edits affect only
        # future runs, and Run Now values still take highest precedence.
        from .workspace import WorkspaceSettingsService, adapt_runtime_settings

        section_map: dict[str, tuple[str, ...]] = {
            "transcribe": ("stt", "subtitles"),
            "correct": ("correction", "subtitles"),
            "translate": ("translation", "subtitles"),
            "optimize_tts": ("text",),
            "clean_source": ("source_cleaning", "text"),
            "prepare_text": ("text", "tts", "audio"),
            "generate_audio": ("text", "tts", "audio", "rvc", "output"),
            "export": ("output", "audio", "subtitles"),
        }
        pipeline_sections = {
            section
            for key in ("transcribe", "correct", "translate", "clean_source", "prepare_text", "optimize_tts", "generate_audio")
            for section in section_map[key]
        }
        requested_sections = sorted(pipeline_sections) if stage_key == "generate_audio" else list(section_map.get(stage_key, ()))
        run_values = dict(settings or {})
        provided_stage_settings = run_values.pop("stage_settings", {})
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
        resolved_stage_settings: dict[str, dict[str, Any]] = {}
        for key, sections in section_map.items():
            stage_value: dict[str, Any] = {}
            for section in sections:
                stage_value.update(adapt_runtime_settings(section, resolved.get(section, {})))
            supplied = provided_stage_settings.get(key, {}) if isinstance(provided_stage_settings, dict) else {}
            resolved_stage_settings[key] = {**stage_value, **(supplied if isinstance(supplied, dict) else {})}

        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            all_artifacts = list(session.scalars(select(Artifact).where(Artifact.session_id == session_id).order_by(Artifact.created_at.desc())).all())
            definition = next((item for item in self.definitions(record, all_artifacts) if item.key == stage_key), None)
            if definition is None or not definition.executable or not definition.job_kind:
                raise ValueError(f"Stage '{stage_key}' cannot be run directly.")
            prerequisite_roles = definition.prerequisite_roles
            outcome = session.scalar(select(OutcomePlan).where(OutcomePlan.session_id == session_id))
            inputs = (outcome.value_json or {}).get("inputs", {}) if outcome and isinstance(outcome.value_json, dict) else {}
            if stage_key == "translate" and str(inputs.get("translation") or "correction") != "correction":
                prerequisite_roles = ("transcription", "upload")
            elif stage_key == "optimize_tts" and record.workflow_kind != "audiobook":
                prerequisite_roles = {
                    "translation": ("translation",),
                    "correction": ("correction",),
                    "source": ("transcription", "upload"),
                }.get(str(inputs.get("generation") or "translation"), prerequisite_roles)
            elif stage_key == "generate_audio":
                transformations = (outcome.value_json or {}).get("transformations", {}) if outcome and isinstance(outcome.value_json, dict) else {}
                if bool(transformations.get("llm_tts_optimization")):
                    prerequisite_roles = ("tts_optimized",)
                else:
                    prerequisite_roles = {
                        "translation": ("translation",),
                        "correction": ("correction",),
                        "source": ("transcription", "upload"),
                    }.get(str(inputs.get("generation") or "translation"), prerequisite_roles)
            artifacts = list(
                session.scalars(
                    select(Artifact).where(
                        Artifact.session_id == session_id,
                        Artifact.state == "current",
                        Artifact.role.in_(prerequisite_roles),
                    ).order_by(Artifact.created_at.desc())
                ).all()
            )
            by_role = {artifact.role: artifact for artifact in artifacts}
            source = next(
                (
                    by_role[role]
                    for role in prerequisite_roles
                    if role in by_role and self._usable_input(definition, by_role[role], record.workflow_kind)
                ),
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
        if stage_key == "generate_audio":
            payload.update({"target_stage": stage_key, "stage_settings": resolved_stage_settings})
            return self.jobs.enqueue("workflow.continue", payload, session_id=session_id, resource_keys=self._resource_keys(session_id, stage_key, flattened))
        return self.jobs.enqueue(definition.job_kind, payload, session_id=session_id, resource_keys=self._resource_keys(session_id, stage_key, flattened))

    @staticmethod
    def _resource_keys(session_id: str, stage_key: str, settings: dict[str, Any]) -> list[str]:
        keys = [f"session:{session_id}"]
        if stage_key in {"correct", "translate", "optimize_tts", "clean_source"}:
            keys.append("service:llm")
        if stage_key in {"generate_audio", "prepare_text"}:
            service = str(settings.get("service") or "tts").lower().replace(" ", "_")
            keys.append(f"service:tts:{service}")
        if stage_key == "transcribe":
            keys.append("service:stt")
        compute = str(settings.get("compute_backend") or settings.get("device") or "auto").lower()
        if compute in {"cuda", "vulkan", "metal", "gpu"}:
            keys.append(f"gpu:{compute}")
        return keys
