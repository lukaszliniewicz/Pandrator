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
    StageDefinition("optimize_document", "Optimize subtitles before generation", "Optionally create a separate, reviewable speech-optimized revision before audio generation. This is useful when the LLM and TTS must not share limited GPU memory.", prerequisite_roles=("translation", "correction", "transcription", "upload"), output_role="tts_optimized", job_kind="text.optimize_tts"),
    StageDefinition("optimize_tts", "Optimize each segment for speech", "Optional LLM rewriting runs immediately before each segment is synthesized. It does not change the subtitle artifact or timing.", executable=False, prerequisite_roles=("translation", "correction", "transcription", "upload")),
    StageDefinition("preview", "Preview", "Compare source, correction, and translation with recorded lineage.", executable=False, prerequisite_roles=("translation", "correction", "transcription", "upload")),
    StageDefinition("generate_audio", "Generate audio", "Synthesize missing or stale prerequisites, optionally optimizing each segment immediately before speech generation.", prerequisite_roles=("translation", "correction", "transcription", "upload"), output_role="dubbing_audio", job_kind="dubbing.generate_audio"),
    StageDefinition("export", "Export", "Package audio, subtitle tracks, or a rendered video.", prerequisite_roles=("dubbing_audio", "translation", "correction", "transcription", "upload"), output_role="export", job_kind="export.create"),
)

AUDIOBOOK_STAGES = (
    StageDefinition("clean_source", "Clean source", "Review deterministic extraction and optional agentic cleanup.", prerequisite_roles=("upload",), output_role="clean_text", job_kind="source.clean"),
    StageDefinition("prepare_text", "Segment narration", "Create editable generation segments from the cleaned document. This controls text boundaries and pauses, not the TTS model.", prerequisite_roles=("clean_text",), output_role="prepared_text", job_kind="text.prepare"),
    StageDefinition("optimize_document", "Optimize narration before generation", "Optionally create a separate before-and-after narration revision for review before any audio is generated.", prerequisite_roles=("prepared_text",), output_role="tts_optimized", job_kind="text.optimize_tts"),
    StageDefinition("optimize_tts", "Optimize each segment for speech", "Optional LLM rewriting runs immediately before each narration segment is synthesized. The source segment remains unchanged for review.", executable=False, prerequisite_roles=("prepared_text",)),
    StageDefinition("generate_audio", "Generate audio", "Run missing document preparation, then generate or resume narration from editable segments.", prerequisite_roles=("prepared_text", "clean_text", "upload"), output_role="audiobook_audio", job_kind="audiobook.generate_audio"),
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
            # The caller has already selected this artifact through the exact
            # outcome-resolved role list (which may be narrower or newer than
            # the definition's broad compatibility roles).
            return True
        filename = str((artifact.metadata_json or {}).get("original_filename") or artifact.relative_path).lower()
        extension = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
        if definition.key == "transcribe":
            return extension in {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
        if definition.key in {"correct", "translate", "optimize_tts", "optimize_document"}:
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
            outcome = session.scalar(select(OutcomePlan).where(OutcomePlan.session_id == session_id))
            transformations = (outcome.value_json or {}).get("transformations", {}) if outcome and isinstance(outcome.value_json, dict) else {}
            optimization_enabled = bool(transformations.get("llm_tts_optimization"))
            document_optimization_enabled = bool(transformations.get("llm_tts_document_optimization"))
            input_choices = (outcome.value_json or {}).get("inputs", {}) if outcome and isinstance(outcome.value_json, dict) else {}
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
                prerequisite_roles = definition.prerequisite_roles
                if definition.key == "translate" and str(input_choices.get("translation") or "correction") != "correction":
                    prerequisite_roles = ("transcription", "upload")
                elif definition.key in {"optimize_document", "generate_audio"}:
                    if definition.key == "generate_audio" and document_optimization_enabled:
                        prerequisite_roles = ("tts_optimized",)
                    elif record.workflow_kind == "audiobook":
                        prerequisite_roles = ("prepared_text",)
                    else:
                        prerequisite_roles = {
                            "translation": ("translation",),
                            "correction": ("correction",),
                            "source": ("transcription", "upload"),
                        }.get(str(input_choices.get("generation") or "translation"), definition.prerequisite_roles)
                prerequisite = next(
                    (
                        roles[role]
                        for role in prerequisite_roles
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
                elif prerequisite_roles and prerequisite is None:
                    status = "unavailable"
                else:
                    status = "ready"
                if definition.key == "optimize_tts" and prerequisite is not None:
                    status = "completed" if optimization_enabled else "ready"
                stage_enabled = optimization_enabled if definition.key == "optimize_tts" else document_optimization_enabled if definition.key == "optimize_document" else None
                stages.append(
                    {
                        "number": index,
                        "key": definition.key,
                        "title": definition.title,
                        "explanation": definition.explanation,
                        "status": status,
                        "executable": definition.executable,
                        "toggle": definition.key in {"optimize_tts", "optimize_document"},
                        "toggle_only": definition.key == "optimize_tts",
                        "enabled": stage_enabled,
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
            # The primary automatic-generation action is allowed to enqueue
            # before its exact derived input exists: workflow.continue creates
            # those missing prerequisites in order. Individual stage controls
            # remain locked by snapshot.status == unavailable.
            if source is None and stage_key == "generate_audio":
                source = next(
                    (
                        artifact
                        for artifact in all_artifacts
                        if artifact.state == "current" and artifact.role == "upload"
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
            resource_keys = self._resource_keys(session_id, stage_key, flattened)
            transformations = (outcome.value_json or {}).get("transformations", {}) if outcome and isinstance(outcome.value_json, dict) else {}
            if any(bool(transformations.get(key)) for key in ("correction", "translation", "llm_tts_optimization", "llm_tts_document_optimization")):
                resource_keys.append("service:llm")
            upload = next((artifact for artifact in all_artifacts if artifact.state == "current" and artifact.role == "upload"), None)
            if upload is not None and record.workflow_kind != "audiobook":
                filename = str((upload.metadata_json or {}).get("original_filename") or upload.relative_path).lower()
                if not filename.endswith(".srt") and not any(artifact.state == "current" and artifact.role == "transcription" for artifact in all_artifacts):
                    resource_keys.append("service:stt")
            return self.jobs.enqueue("workflow.continue", payload, session_id=session_id, resource_keys=list(dict.fromkeys(resource_keys)))
        return self.jobs.enqueue(definition.job_kind, payload, session_id=session_id, resource_keys=self._resource_keys(session_id, stage_key, flattened))

    @staticmethod
    def _resource_keys(session_id: str, stage_key: str, settings: dict[str, Any]) -> list[str]:
        keys = [f"session:{session_id}"]
        if stage_key in {"correct", "translate", "optimize_tts", "optimize_document", "clean_source"}:
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
