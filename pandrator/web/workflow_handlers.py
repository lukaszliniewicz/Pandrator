"""Worker adapters that run existing Pandrator engines without Qt."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .credentials import (
    TTS_SERVICE_ENVS,
    auxiliary_credential_key,
    database_reference,
    hydrate_tts_settings,
    resolve_secret_reference,
    tts_credential_key,
)
from .database import Database
from .models import (
    AgentRun,
    AgentStep,
    AppSetting,
    AppSettingHistory,
    Artifact,
    AudioTake,
    Document,
    DocumentRevision,
    GenerationRun,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    OutcomePlan,
    OutputAssembly,
    Segment,
    SegmentLineage,
    SessionRecord,
    SourceRecord,
    TrainingRun,
    TimedWord,
    UsageEvent,
    Voice,
    VoiceSample,
    new_id,
    utcnow,
)


def _hash_segments(segments) -> str:
    payload = [
        {"start_ms": segment.start_ms, "end_ms": segment.end_ms, "text": segment.text}
        for segment in segments
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class WorkflowHandlers:
    def __init__(self, database: Database, paths: DataPaths):
        self.database = database
        self.paths = paths
        self.artifacts = ArtifactService(database, paths)

    def handlers(self):
        return {
            "dubbing.transcribe": self.transcribe,
            "dubbing.correct": self.correct,
            "dubbing.translate": self.translate,
            "text.optimize_tts": self.optimize_tts,
            "dubbing.generate_audio": self.generate_dubbing_audio,
            "source.clean": self.clean_source,
            "text.prepare": self.prepare_text,
            "audiobook.generate_audio": self.generate_audiobook_audio,
            "export.create": self.export,
            "voice.transcribe": self.transcribe_voice,
            "voice.normalize_recording": self.normalize_voice_recording,
            "voice.publish": self.publish_voice,
            "rvc.model.upload": self.upload_rvc_model,
            "rvc.convert": self.convert_with_rvc,
            "training.xtts": self.train_xtts,
            "pdf.apply_edits": self.apply_pdf_edits,
            "session.bundle.export": self.export_session_bundle,
            "session.bundle.import": self.import_session_bundle,
            "workflow.continue": self.continue_workflow,
            "source.download_url": self.download_source_url,
            "source.reuse": self.reuse_source,
            "generation.run": self.run_generation,
            "generation.assemble": self.assemble_generation_output,
            "audio.waveform": self.generate_waveform,
            "tts.preview": self.preview_tts_voice,
        }

    def preview_tts_voice(self, payload, progress, cancel_event):
        """Generate a short managed preview without mutating a session plan."""
        from pandrator.logic import tts_handler

        text = str(payload.get("text") or "").strip()
        settings = hydrate_tts_settings(self.database, self.paths, dict(payload.get("settings") or {}))
        if not text:
            raise ValueError("Preview text is required.")
        if cancel_event.is_set():
            return {}
        progress(0.1, "Requesting voice preview")
        urls = self._tts_urls(settings)
        service_id = str(settings.get("preview_service_id") or "").lower()
        api_base = str(settings.get("preview_api_base") or "").strip()
        url_key = {
            "xtts": "xtts_base_url", "voxcpm": "voxcpm_base_url", "fishs2": "fishs2_base_url",
            "voxtral": "voxtral_base_url", "kokoro": "kokoro_base_url", "silero": "silero_base_url",
            "chatterbox": "chatterbox_base_url", "kobold_qwen": "kobold_qwen_base_url", "magpie": "magpie_base_url",
        }.get(service_id)
        if url_key and api_base:
            urls[url_key] = api_base
        audio = tts_handler.text_to_audio(text, settings, max_attempts=1, **urls)
        if audio is None:
            raise RuntimeError("The speech service did not return preview audio.")
        preview_identity = {
            "service_id": service_id,
            "model": str(settings.get("model") or ""),
            "voice": str(settings.get("voice") or ""),
            "language": str(settings.get("language") or ""),
        }
        preview_key = hashlib.sha256(
            json.dumps(preview_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        target_dir = self.paths.artifacts / "tts-previews"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{preview_key}.wav"
        exported = audio.export(target, format="wav")
        exported.close()
        artifact = self.artifacts.register(
            target,
            kind="audio",
            role="tts_voice_preview",
            settings=settings,
            metadata={
                **preview_identity,
                "service": settings.get("service"),
                "preview_text": text,
            },
        )
        progress(1.0, "Preview ready")
        return {"artifact_id": artifact.id, "duration_ms": len(audio)}

    @staticmethod
    def _validate_download_url(raw_url: str) -> str:
        import ipaddress
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(str(raw_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Source URL must use http or https.")
        for _family, _type, _proto, _canon, address in socket.getaddrinfo(parsed.hostname, parsed.port or 443):
            ip = ipaddress.ip_address(address[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError("Source URL resolves to a non-public network address.")
        return parsed.geturl()

    def download_source_url(self, payload, progress, cancel_event):
        import yt_dlp
        from .workspace import SourceLibraryService

        session_id = str(payload.get("session_id") or "")
        url = self._validate_download_url(str(payload.get("url") or ""))
        destination_dir = self._session_dir(session_id) / "sources"
        destination_dir.mkdir(parents=True, exist_ok=True)
        progress(0.05, "Inspecting source URL")
        options = {
            "outtmpl": str(destination_dir / "%(title).160B-%(id)s.%(ext)s"),
            "restrictfilenames": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            information = downloader.extract_info(url, download=True)
            output = Path(downloader.prepare_filename(information)).resolve()
        if cancel_event.is_set():
            return {}
        if destination_dir.resolve() not in output.parents or not output.is_file():
            raise RuntimeError("Downloaded source was not created in the managed session directory.")
        source_metadata = {
            "original_filename": output.name,
            "source_url": url,
            "downloader": "yt-dlp",
        }
        artifact = self.artifacts.register(
            output,
            kind="source",
            role="upload",
            session_id=session_id,
            metadata=source_metadata,
        )
        with self.database.session() as session:
            session.add(SourceRecord(session_id=session_id, kind=output.suffix.lower().lstrip(".") or "url", display_name=output.name, artifact_id=artifact.id, content_hash=artifact.content_hash, metadata_json={"url": url, "downloader": "yt-dlp"}))
        library = SourceLibraryService(self.database)
        asset = library.ensure_for_artifact(artifact.id, display_name=output.name, kind=output.suffix.lower().lstrip(".") or "url")
        library.attach(session_id, asset.id)
        progress(1.0, "Source download ready")
        return {"artifact_id": artifact.id, "filename": output.name}

    def reuse_source(self, payload, progress, cancel_event):
        from .workspace import SourceLibraryService
        session_id = str(payload.get("session_id") or "")
        source, source_path = self._resolve_input(str(payload.get("artifact_id") or ""))
        destination_dir = self._session_dir(session_id) / "sources"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{source.id}-{source_path.name}"
        progress(0.2, "Copying reusable source")
        shutil.copy2(source_path, destination)
        if cancel_event.is_set():
            destination.unlink(missing_ok=True)
            return {}
        artifact = self.artifacts.register(destination, kind="source", role="upload", session_id=session_id, parent_ids=[source.id], metadata={"original_filename": source_path.name, "reused_from": source.id})
        with self.database.session() as session:
            session.add(SourceRecord(session_id=session_id, kind=destination.suffix.lower().lstrip(".") or "file", display_name=source_path.name, artifact_id=artifact.id, content_hash=artifact.content_hash, metadata_json={"reused_from": source.id}))
        library = SourceLibraryService(self.database)
        asset = library.ensure_for_artifact(artifact.id, display_name=source_path.name, kind=destination.suffix.lower().lstrip(".") or "file")
        library.attach(session_id, asset.id)
        progress(1.0, "Reusable source ready")
        return {"artifact_id": artifact.id, "filename": source_path.name}

    def _latest_stage_input(self, session_id: str, prerequisite_roles: tuple[str, ...]) -> Artifact | None:
        with self.database.session() as session:
            candidates = list(
                session.scalars(
                    select(Artifact).where(
                        Artifact.session_id == session_id,
                        Artifact.state == "current",
                        Artifact.role.in_(prerequisite_roles),
                    ).order_by(Artifact.created_at.desc())
                ).all()
            )
            by_role = {item.role: item for item in candidates}
            result = next((by_role[role] for role in prerequisite_roles if role in by_role), None)
            if result is not None:
                session.expunge(result)
            return result

    def continue_workflow(self, payload, progress, cancel_event):
        """Run only missing/stale included prerequisites, then the requested outcome stage."""
        from .workflows import AUDIOBOOK_STAGES, DUBBING_STAGES

        session_id = str(payload.get("session_id") or "")
        target_key = str(payload.get("target_stage") or "generate_audio")
        record = self._session_record(session_id)
        definitions = AUDIOBOOK_STAGES if record.workflow_kind == "audiobook" else DUBBING_STAGES
        is_srt_source = False
        if record.workflow_kind != "audiobook":
            with self.database.session() as session:
                upload = session.scalar(select(Artifact).where(Artifact.session_id == session_id, Artifact.role == "upload", Artifact.state == "current").order_by(Artifact.created_at.desc()))
            filename = str((upload.metadata_json or {}).get("original_filename") or upload.relative_path).lower() if upload else ""
            is_srt_source = filename.endswith(".srt")
            if is_srt_source:
                definitions = tuple(item for item in definitions if item.key != "transcribe")
        target_index = next((index for index, item in enumerate(definitions) if item.key == target_key), None)
        if target_index is None:
            raise ValueError(f"Unknown continuation stage: {target_key}")
        included = set(record.included_stages_json or [])
        with self.database.session() as session:
            outcome = session.scalar(select(OutcomePlan).where(OutcomePlan.session_id == session_id))
            outcome_value = dict(outcome.value_json or {}) if outcome else {}
        input_choices = outcome_value.get("inputs") if isinstance(outcome_value.get("inputs"), dict) else {}
        transformations = outcome_value.get("transformations") if isinstance(outcome_value.get("transformations"), dict) else {}
        stage_settings = payload.get("stage_settings") if isinstance(payload.get("stage_settings"), dict) else {}
        direct_settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
        required = {target_key}
        if record.workflow_kind == "audiobook" and target_key in {"generate_audio", "export"}:
            required.update({"clean_source", "prepare_text"})
        elif target_key in {"generate_audio", "export"}:
            if not is_srt_source:
                required.add("transcribe")
            translation_parent = str(input_choices.get("translation") or "correction")
            generation_parent = str(input_choices.get("generation") or "translation")
            translation_required = bool(transformations.get("translation")) or generation_parent == "translation"
            if bool(transformations.get("correction")) or generation_parent == "correction" or (translation_required and translation_parent == "correction"):
                required.add("correct")
            if translation_required:
                required.add("translate")
        if bool(transformations.get("llm_tts_document_optimization")) and target_key in {"generate_audio", "export"}:
            required.add("optimize_document")
        runnable = [
            item for index, item in enumerate(definitions)
            if index <= target_index and item.executable and item.job_kind and (item.key in included or item.key in required)
        ]
        produced: list[dict[str, Any]] = []
        handlers = self.handlers()
        stage_weights = {
            "clean_source": 0.12,
            "transcribe": 0.18,
            "correct": 0.10,
            "translate": 0.10,
            "optimize_document": 0.10,
            "prepare_text": 0.05,
            # Speech synthesis is normally the dominant part of this action.
            "generate_audio": 0.65,
            "export": 0.10,
        }
        weights = [stage_weights.get(item.key, 0.08) for item in runnable]
        weight_total = sum(weights) or 1.0
        completed_weight = 0.0
        for index, definition in enumerate(runnable):
            if cancel_event.is_set():
                return {"artifacts": produced}
            settings = stage_settings.get(definition.key) if isinstance(stage_settings.get(definition.key), dict) else {}
            if definition.key == target_key:
                settings = {**settings, **direct_settings}
            if definition.key == "generate_audio":
                settings["llm_tts_optimization"] = bool(transformations.get("llm_tts_optimization"))
            expected_settings_hash = hashlib.sha256(
                json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            ).hexdigest()
            with self.database.session() as session:
                existing = session.scalar(
                    select(Artifact).where(
                        Artifact.session_id == session_id,
                        Artifact.role == definition.output_role,
                        Artifact.state == "current",
                    ).order_by(Artifact.created_at.desc())
                ) if definition.output_role else None
            if (
                existing is not None
                and definition.key != target_key
                and (
                    existing.settings_hash == expected_settings_hash
                    or str((existing.metadata_json or {}).get("requested_settings_hash") or "") == expected_settings_hash
                )
            ):
                continue
            input_roles = definition.prerequisite_roles
            if definition.key == "translate":
                translation_parent = str(input_choices.get("translation") or "correction")
                input_roles = ("correction",) if translation_parent == "correction" else ("transcription", "upload")
            elif definition.key in {"optimize_document", "generate_audio"}:
                if definition.key == "generate_audio" and bool(transformations.get("llm_tts_document_optimization")):
                    input_roles = ("tts_optimized",)
                elif record.workflow_kind == "audiobook":
                    input_roles = ("prepared_text",)
                else:
                    generation_parent = str(input_choices.get("generation") or "translation")
                    input_roles = {
                        "translation": ("translation",),
                        "correction": ("correction",),
                        "source": ("transcription", "upload"),
                    }.get(generation_parent, definition.prerequisite_roles)
            source = self._latest_stage_input(session_id, input_roles)
            if definition.prerequisite_roles and source is None:
                raise ValueError(f"Stage '{definition.key}' is missing a required input artifact.")
            handler = handlers[definition.job_kind]
            width = weights[index] / weight_total
            start = completed_weight / weight_total
            stage_progress = lambda value, detail=None, start=start, width=width: progress(
                min(0.99, start + max(0.0, min(1.0, float(value))) * width),
                detail,
            )
            handler_payload = {
                "session_id": session_id,
                "source_artifact_id": source.id if source else None,
                "settings": settings,
            }
            if definition.key == "generate_audio":
                result = self._run_reviewable_generation(
                    handler_payload,
                    stage_progress,
                    cancel_event,
                    resolved_snapshot=payload.get("resolved_settings_snapshot"),
                    settings_hash=str(payload.get("settings_hash") or "") or None,
                    job_id=str(payload.get("_job_id") or "") or None,
                )
            else:
                result = handler(handler_payload, stage_progress, cancel_event)
            if result:
                produced.append({"stage": definition.key, **result})
            completed_weight += weights[index]
            if definition.key == "generate_audio" and str((result or {}).get("status") or "") in {"paused", "canceled"}:
                return {"artifacts": produced, "target_stage": target_key}
        progress(1.0, "Workflow continuation finished")
        return {"artifacts": produced, "target_stage": target_key}

    def _session_dir(self, session_id: str) -> Path:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise ValueError(f"Session not found: {session_id}")
            path = self.paths.sessions / record.storage_key
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _session_record(self, session_id: str) -> SessionRecord:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise ValueError(f"Session not found: {session_id}")
            session.expunge(record)
            return record

    def _resolve_input(self, artifact_id: str) -> tuple[Artifact, Path]:
        artifact, path = self.artifacts.resolve(artifact_id)
        if not path.is_file():
            raise FileNotFoundError(path)
        return artifact, path

    def _store_srt_document(
        self,
        session_id: str,
        artifact: Artifact,
        stage: str,
        *,
        language: str | None = None,
        parent_artifact: Artifact | None = None,
    ) -> tuple[str, str]:
        from pandrator.logic.dubbing.srt_utils import parse_srt

        _record, path = self.artifacts.resolve(artifact.id)
        segments = parse_srt(path.read_text(encoding="utf-8-sig"))
        with self.database.session() as session:
            document = Document(session_id=session_id, stage=stage, language=language)
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id,
                revision_number=1,
                content_hash=_hash_segments(segments),
            )
            session.add(revision)
            session.flush()
            child_records: list[Segment] = []
            for ordinal, item in enumerate(segments):
                child = Segment(
                    revision_id=revision.id,
                    ordinal=ordinal,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    text=item.text,
                    speaker=getattr(item, "speaker", None),
                )
                session.add(child)
                child_records.append(child)
            session.flush()
            document.active_revision_id = revision.id

            if parent_artifact:
                parent_revision_id = str((parent_artifact.metadata_json or {}).get("revision_id") or "")
                if parent_revision_id:
                    parents = list(
                        session.scalars(
                            select(Segment).where(Segment.revision_id == parent_revision_id).order_by(Segment.ordinal)
                        ).all()
                    )
                    for child in child_records:
                        overlaps = [
                            parent
                            for parent in parents
                            if child.start_ms is not None
                            and child.end_ms is not None
                            and parent.start_ms is not None
                            and parent.end_ms is not None
                            and min(child.end_ms, parent.end_ms) > max(child.start_ms, parent.start_ms)
                        ]
                        for sequence, parent in enumerate(overlaps):
                            session.add(
                                SegmentLineage(
                                    parent_segment_id=parent.id,
                                    child_segment_id=child.id,
                                    relation="temporal_overlap",
                                    sequence=sequence,
                                )
                            )

            managed = session.get(Artifact, artifact.id)
            managed.metadata_json = {
                **(managed.metadata_json or {}),
                "document_id": document.id,
                "revision_id": revision.id,
                "stage": stage,
                "language": language,
            }
            return document.id, revision.id

    def _store_timed_words(self, revision_id: str, metadata_path: Path) -> int:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        transcription = payload.get("transcription") if isinstance(payload, dict) else []
        words: list[dict[str, Any]] = []
        for group in transcription if isinstance(transcription, list) else []:
            if not isinstance(group, dict):
                continue
            speaker = str(group.get("speaker") or "") or None
            for word in group.get("words") if isinstance(group.get("words"), list) else []:
                if not isinstance(word, dict) or not isinstance(word.get("offsets"), dict):
                    continue
                offsets = word["offsets"]
                try:
                    start_ms = int(offsets["from"])
                    end_ms = int(offsets["to"])
                except (KeyError, TypeError, ValueError):
                    continue
                text = str(word.get("text") or word.get("word") or "").strip()
                if text and end_ms > start_ms >= 0:
                    words.append({"text": text, "start_ms": start_ms, "end_ms": end_ms, "speaker": str(word.get("speaker") or "") or speaker, "confidence": word.get("confidence") or word.get("probability"), "metadata": {key: value for key, value in word.items() if key not in {"text", "word", "offsets", "speaker", "confidence", "probability"}}})
        with self.database.session() as session:
            segments = list(session.scalars(select(Segment).where(Segment.revision_id == revision_id).order_by(Segment.ordinal)).all())
            for ordinal, word in enumerate(words):
                owner = next((segment for segment in segments if segment.start_ms is not None and segment.end_ms is not None and min(segment.end_ms, word["end_ms"]) > max(segment.start_ms, word["start_ms"])), None)
                session.add(TimedWord(revision_id=revision_id, segment_id=owner.id if owner else None, ordinal=ordinal, text=word["text"], start_ms=word["start_ms"], end_ms=word["end_ms"], speaker=word["speaker"], confidence=float(word["confidence"]) if word["confidence"] is not None else None, metadata_json=word["metadata"]))
        return len(words)

    def _record_usage(self, session_id: str, stage: str, settings: dict[str, Any], result) -> None:
        cost = float(getattr(result, "cost", 0.0) or 0.0)
        response_count = int(getattr(result, "response_count", 0) or 0)
        raw_usage = getattr(result, "usage", {})
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        if not cost and not response_count and not usage:
            return
        model = str(settings.get(f"{stage}_model") or settings.get("default_model") or "default")
        provider = model.split("/", 1)[0] if "/" in model else "default"
        sources = tuple(getattr(result, "cost_sources", ()) or ())
        with self.database.session() as session:
            session.add(
                UsageEvent(
                    session_id=session_id,
                    stage=stage,
                    provider_key=provider,
                    model_id=model,
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    cached_input_tokens=int(usage.get("cached_prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                    cost_usd=cost,
                    cost_source=",".join(sources) or None,
                    raw_usage_json={"response_count": response_count, **usage},
                )
            )

    def _with_database_llm_settings(self, settings: dict[str, Any], stage: str) -> dict[str, Any]:
        from .provider_settings import build_llm_settings

        aliases = {
            "correction": ("correction_model", "correct_model"),
            "translation": ("translation_model", "translate_model"),
            "tts_optimization": ("tts_optimization_model", "llm_model"),
        }
        requested = str(settings.get("model_name") or "").strip()
        for key in aliases[stage]:
            requested = requested or str(settings.get(key) or "").strip()
        if requested == "default":
            requested = ""
        llm_settings, resolved_model = build_llm_settings(
            self.database,
            self.paths,
            requested_model=requested,
            request_timeout_seconds=int(settings.get("request_timeout_seconds") or 600),
        )
        hydrated = {
            **settings,
            "llm_provider_configs": llm_settings.provider_configs,
            "llm_default_model": llm_settings.default_model,
            "request_timeout_seconds": llm_settings.request_timeout_seconds,
        }
        hydrated[aliases[stage][0]] = requested or resolved_model
        return hydrated

    def transcribe(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.transcription import transcribe_source_file_with_metadata

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        session_dir = self._session_dir(session_id)
        progress(0.05, "Preparing transcription")
        if cancel_event.is_set():
            return {}
        transcription_result = transcribe_source_file_with_metadata(
            session_dir,
            source_path,
            dict(payload.get("settings") or {}),
            ffmpeg_executable=str(payload.get("ffmpeg_executable") or "ffmpeg"),
            crispasr_executable=str(payload.get("crispasr_executable") or ""),
        )
        output_path = Path(transcription_result.srt_path)
        progress(0.9, "Registering transcription")
        artifact = self.artifacts.register(
            output_path,
            kind="srt",
            role="transcription",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=dict(payload.get("settings") or {}),
        )
        timing_artifact = self.artifacts.register(
            Path(transcription_result.word_timestamps_path),
            kind="json",
            role="word_timestamps",
            session_id=session_id,
            parent_ids=[source_artifact.id, artifact.id],
            settings={
                **dict(payload.get("settings") or {}),
                "stt_engine": transcription_result.engine,
                "stt_compute_backend": transcription_result.compute_backend,
            },
        )
        _document_id, revision_id = self._store_srt_document(
            session_id,
            artifact,
            "transcription",
            language=str((payload.get("settings") or {}).get("original_language") or "") or None,
        )
        word_count = self._store_timed_words(revision_id, Path(transcription_result.word_timestamps_path))
        progress(1.0, "Transcription ready")
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "word_timestamps_artifact_id": timing_artifact.id,
            "word_timestamps_path": timing_artifact.relative_path,
            "word_count": word_count,
            "revision_id": revision_id,
        }

    def correct(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.llm_correction import correct_srt_file_with_result

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        session_dir = self._session_dir(session_id)
        settings = self._with_database_llm_settings(dict(payload.get("settings") or {}), "correction")
        progress(0.05, "Correcting subtitles")
        result = correct_srt_file_with_result(
            session_dir,
            source_path,
            settings,
            correction_instructions=str(payload.get("instructions") or settings.get("instructions") or ""),
        )
        if cancel_event.is_set():
            return {}
        artifact = self.artifacts.register(
            Path(result.output_path),
            kind="srt",
            role="correction",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
        )
        self._store_srt_document(
            session_id,
            artifact,
            "correction",
            language=str(settings.get("original_language") or "") or None,
            parent_artifact=source_artifact,
        )
        self._record_usage(session_id, "correction", settings, result)
        progress(1.0, "Correction ready")
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "cost": result.cost}

    def translate(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.llm_translation import (
            translate_srt_file_deepl_with_result,
            translate_srt_file_with_result,
        )

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        session_dir = self._session_dir(session_id)
        settings = dict(payload.get("settings") or {})
        progress(0.05, "Translating subtitles")
        if str(settings.get("translation_backend") or "llm").lower() == "deepl":
            credential = resolve_secret_reference(
                self.database,
                self.paths,
                database_reference(auxiliary_credential_key("deepl")),
                fallback_environment_variable="DEEPL_API_KEY",
            )
            result = translate_srt_file_deepl_with_result(
                session_dir,
                source_path,
                settings,
                auth_key=credential.resolved_value(),
            )
        else:
            settings = self._with_database_llm_settings(settings, "translation")
            result = translate_srt_file_with_result(
                session_dir,
                source_path,
                settings,
                translation_instructions=str(payload.get("instructions") or settings.get("instructions") or ""),
            )
        if cancel_event.is_set():
            return {}
        artifact = self.artifacts.register(
            Path(result.output_path),
            kind="srt",
            role="translation",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
        )
        self._store_srt_document(
            session_id,
            artifact,
            "translation",
            language=str(settings.get("target_language") or "") or None,
            parent_artifact=source_artifact,
        )
        self._record_usage(session_id, "translation", settings, result)
        progress(1.0, "Translation ready")
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "cost": result.cost}

    def optimize_tts(self, payload, progress, cancel_event):
        """Create a separate, previewable text revision optimized only for speech."""
        from dataclasses import replace
        from types import SimpleNamespace

        from pandrator.logic.dubbing.srt_utils import compose_srt, parse_srt

        from .tts_optimization import optimize_texts

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        requested_settings = dict(payload.get("settings") or {})
        requested_settings_hash = hashlib.sha256(
            json.dumps(requested_settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        settings = self._with_database_llm_settings(requested_settings, "tts_optimization")
        settings["llm_tts_batch_size"] = max(
            1,
            int(settings.get("llm_tts_document_batch_size") or settings.get("llm_tts_batch_size") or 8),
        )
        llm_settings = SimpleNamespace(
            provider_configs=settings["llm_provider_configs"],
            default_model=settings["llm_default_model"],
            request_timeout_seconds=settings["request_timeout_seconds"],
        )
        model_name = str(settings.get("tts_optimization_model") or settings["llm_default_model"])
        suffix = source_path.suffix.lower()
        progress(0.02, "Preparing speech optimization preview")
        if suffix == ".srt":
            segments = parse_srt(source_path.read_text(encoding="utf-8-sig"))
            optimized, usage = optimize_texts(
                [segment.text for segment in segments], settings, llm_settings, model_name, cancel_event, progress
            )
            if cancel_event.is_set():
                return {}
            segments = [replace(segment, text=text) for segment, text in zip(segments, optimized)]
            destination = self._session_dir(session_id) / f"tts-optimized-{new_id()}.srt"
            destination.write_text(compose_srt(segments), encoding="utf-8")
            kind = "srt"
        elif suffix == ".json":
            rows = json.loads(source_path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError("Speech optimization JSON input must contain a list of generation units.")
            source_texts = [
                str(row.get("processed_sentence") or row.get("original_sentence") or row.get("text") or "")
                if isinstance(row, dict) else str(row)
                for row in rows
            ]
            optimized, usage = optimize_texts(source_texts, settings, llm_settings, model_name, cancel_event, progress)
            if cancel_event.is_set():
                return {}
            for row, text in zip(rows, optimized):
                if isinstance(row, dict):
                    row["source_text"] = str(row.get("source_text") or row.get("text") or row.get("processed_sentence") or row.get("original_sentence") or "")
                    row["tts_optimized_sentence"] = text
                    row["processed_sentence"] = text
                    row["text"] = text
            destination = self._session_dir(session_id) / f"tts-optimized-{new_id()}.json"
            destination.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            kind = "json"
        else:
            source_text = source_path.read_text(encoding="utf-8-sig")
            optimized, usage = optimize_texts([source_text], settings, llm_settings, model_name, cancel_event, progress)
            if cancel_event.is_set():
                return {}
            destination = self._session_dir(session_id) / f"tts-optimized-{new_id()}.txt"
            destination.write_text(optimized[0], encoding="utf-8")
            kind = "text"
        artifact = self.artifacts.register(
            destination,
            kind=kind,
            role="tts_optimized",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={"source_artifact_id": source_artifact.id, "model": model_name, "mode": "whole_document", "batch_size": settings["llm_tts_batch_size"], "requested_settings_hash": requested_settings_hash},
        )
        if suffix == ".srt":
            self._store_srt_document(
                session_id,
                artifact,
                "tts_optimization",
                language=str((source_artifact.metadata_json or {}).get("language") or "") or None,
                parent_artifact=source_artifact,
            )
        self._record_usage(session_id, "tts_optimization", settings, usage)
        progress(1.0, "Speech optimization preview ready")
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "cost": usage.cost}

    def transcribe_voice(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.transcription import transcribe_source_file_with_metadata
        from pandrator.logic.dubbing.srt_utils import parse_srt

        sample_artifact, sample_path = self._resolve_input(str(payload.get("sample_artifact_id") or ""))
        operation_dir = self.paths.voices / str(payload.get("voice_id") or "transcription")
        operation_dir.mkdir(parents=True, exist_ok=True)
        progress(0.05, "Preparing reference transcription")
        transcription_result = transcribe_source_file_with_metadata(
            operation_dir,
            sample_path,
            dict(payload.get("settings") or {}),
            ffmpeg_executable=str(payload.get("ffmpeg_executable") or "ffmpeg"),
            crispasr_executable=str(payload.get("crispasr_executable") or ""),
        )
        output_path = Path(transcription_result.srt_path)
        if cancel_event.is_set():
            return {}
        artifact = self.artifacts.register(
            output_path,
            kind="srt",
            role="voice_transcription",
            parent_ids=[sample_artifact.id],
            settings=dict(payload.get("settings") or {}),
        )
        timing_artifact = self.artifacts.register(
            Path(transcription_result.word_timestamps_path),
            kind="json",
            role="voice_word_timestamps",
            parent_ids=[sample_artifact.id, artifact.id],
            settings=dict(payload.get("settings") or {}),
        )
        transcript = " ".join(segment.text.replace("\n", " ").strip() for segment in parse_srt(output_path.read_text(encoding="utf-8-sig")))
        progress(1.0, "Reference transcription ready for review")
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "word_timestamps_artifact_id": timing_artifact.id,
            "sample_id": payload.get("sample_id"),
            "transcript": transcript,
        }

    def normalize_voice_recording(self, payload, progress, cancel_event):
        voice_id = str(payload.get("voice_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        with self.database.session() as session:
            if session.get(Voice, voice_id) is None:
                raise ValueError("Voice not found.")
        voice_dir = self.paths.voices / voice_id
        voice_dir.mkdir(parents=True, exist_ok=True)
        destination = voice_dir / f"sample-{source_artifact.id}.wav"
        progress(0.1, "Normalizing recording")
        command = [str(payload.get("ffmpeg_executable") or "ffmpeg"), "-y", "-i", str(source_path), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(destination)]
        subprocess.run(command, check=True, capture_output=True, text=True)
        if cancel_event.is_set():
            destination.unlink(missing_ok=True)
            return {}
        artifact = self.artifacts.register(destination, kind="audio", role="voice_sample", parent_ids=[source_artifact.id])
        with self.database.session() as session:
            sample = VoiceSample(voice_id=voice_id, artifact_id=artifact.id)
            session.add(sample)
            session.flush()
            sample_id = sample.id
        progress(1.0, "Voice sample ready")
        return {"sample_id": sample_id, "artifact_id": artifact.id, "path": artifact.relative_path}

    def publish_voice(self, payload, progress, cancel_event):
        """Upload the newest managed sample and persist the provider's voice ID."""
        from pandrator.logic import tts_handler

        voice_id = str(payload.get("voice_id") or "")
        service_id = str(payload.get("service_id") or "").strip()
        service_name = str(payload.get("service") or service_id).strip()
        base_url = str(payload.get("base_url") or "").strip()
        with self.database.session() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice not found.")
            samples = list(
                session.scalars(
                    select(VoiceSample)
                    .where(VoiceSample.voice_id == voice_id)
                    .order_by(VoiceSample.created_at.desc())
                ).all()
            )
            if not samples:
                raise ValueError("Add a voice sample before uploading this voice.")
            sample = samples[0]
            provider_records = dict((voice.metadata_json or {}).get("providers") or {})
            existing = dict(provider_records.get(service_id) or {})
            requested_provider_voice_id = str(existing.get("voice_id") or voice.name).strip()
            voice_name = voice.name

        _artifact, sample_path = self._resolve_input(sample.artifact_id)
        if sample_path.suffix.lower() != ".wav":
            raise ValueError("Provider voice uploads require a normalized WAV sample.")
        if cancel_event.is_set():
            return {}
        progress(0.1, f"Uploading {voice_name} to {service_name}")
        with self.database.session() as session:
            connections = session.get(AppSetting, "services.tts")
            defaults = session.get(AppSetting, "defaults.tts")
            connection_value = dict(connections.value_json or {}) if connections and isinstance(connections.value_json, dict) else {}
            default_value = dict(defaults.value_json or {}) if defaults and isinstance(defaults.value_json, dict) else {}
        service_config = tts_handler.get_service_config({**default_value, **connection_value}, service_id) or {}
        normalized_service_id = str(service_config.get("id") or service_id).strip().lower().replace("-", "_")
        credential = resolve_secret_reference(
            self.database,
            self.paths,
            service_config.get("secret_ref") or database_reference(tts_credential_key(normalized_service_id)),
            fallback_environment_variable=str(service_config.get("api_key_env") or TTS_SERVICE_ENVS.get(normalized_service_id, "")),
        )
        provider_voice_id = tts_handler.upload_speaker_voice(
            str(sample_path),
            base_url=base_url,
            service=service_name,
            prompt_text=sample.transcript if sample.transcript_reviewed else None,
            voice_id=requested_provider_voice_id,
            api_key=credential.resolved_value(),
        )
        if cancel_event.is_set():
            return {}
        with self.database.session() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice was removed while it was being uploaded.")
            metadata = deepcopy(voice.metadata_json or {})
            providers = dict(metadata.get("providers") or {})
            providers[service_id] = {
                "voice_id": provider_voice_id,
                "sample_id": sample.id,
                "status": "ready",
                "updated_at": utcnow().isoformat(),
            }
            metadata["providers"] = providers
            voice.metadata_json = metadata
            voice.revision += 1
            voice.updated_at = utcnow()
        progress(1.0, f"{voice_name} is ready in {service_name}")
        return {
            "voice_id": voice_id,
            "service_id": service_id,
            "provider_voice_id": provider_voice_id,
        }

    def upload_rvc_model(self, payload, progress, cancel_event):
        from pandrator.logic import rvc_handler

        pth_artifact, pth_path = self._resolve_input(str(payload.get("pth_artifact_id") or ""))
        index_artifact, index_path = self._resolve_input(str(payload.get("index_artifact_id") or ""))
        if pth_path.suffix.lower() != ".pth":
            raise ValueError("The RVC weights artifact must be a .pth file.")
        if index_path.suffix.lower() not in {".index", ".idx"}:
            raise ValueError("The RVC index artifact must be an .index or .idx file.")
        if not rvc_handler.is_rvc_available():
            raise RuntimeError("The RVC service is not available.")
        progress(0.15, "Installing RVC model")
        model_root = self.paths.models / "rvc"
        model_root.mkdir(parents=True, exist_ok=True)
        model_name = rvc_handler.upload_rvc_model(str(pth_path), str(index_path), str(model_root))
        if cancel_event.is_set():
            return {}
        manifest = model_root / model_name / "pandrator-model.json"
        manifest.write_text(
            json.dumps(
                {
                    "kind": "rvc",
                    "model_name": model_name,
                    "weights_artifact_id": pth_artifact.id,
                    "index_artifact_id": index_artifact.id,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifact = self.artifacts.register(
            manifest,
            kind="model",
            role="rvc_model",
            parent_ids=[pth_artifact.id, index_artifact.id],
            metadata={"model_name": model_name},
        )
        progress(1.0, "RVC model ready")
        return {"model_name": model_name, "artifact_id": artifact.id, "path": artifact.relative_path}

    def convert_with_rvc(self, payload, progress, cancel_event):
        from pydub import AudioSegment
        from pandrator.logic import rvc_handler

        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        session_id = str(payload.get("session_id") or "") or source_artifact.session_id
        settings = dict(payload.get("settings") or {})
        if not str(settings.get("rvc_model") or "").strip():
            raise ValueError("Select an RVC model before conversion.")
        if not rvc_handler.is_rvc_available():
            raise RuntimeError("The RVC service is not available.")
        progress(0.1, "Loading source audio")
        audio = AudioSegment.from_file(source_path)
        if cancel_event.is_set():
            return {}
        progress(0.3, "Converting voice with RVC")
        converted = rvc_handler.process_with_rvc(audio, {**settings, "raise_on_error": True})
        destination_dir = self._session_dir(session_id) if session_id else self.paths.artifacts / "rvc"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{source_path.stem}-rvc-{hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:10]}.wav"
        converted.export(destination, format="wav")
        artifact = self.artifacts.register(
            destination,
            kind="audio",
            role="rvc_audio",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={"rvc_model": settings["rvc_model"]},
        )
        progress(1.0, "RVC audio ready")
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "model_name": settings["rvc_model"]}

    def train_xtts(self, payload, progress, cancel_event):
        from pandrator.logic import xtts_trainer_handler

        training_id = str(payload.get("training_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        source_text_path = ""
        source_text_id = str(payload.get("source_text_artifact_id") or "")
        if source_text_id:
            _text_artifact, text_path = self._resolve_input(source_text_id)
            source_text_path = str(text_path)
        settings = dict(payload.get("settings") or {})
        model_name = str(payload.get("model_name") or settings.get("model_name") or "").strip()
        if not model_name:
            raise ValueError("An XTTS model name is required.")
        with self.database.session() as session:
            training = session.get(TrainingRun, training_id)
            if training is None:
                raise ValueError("Training record not found.")
            training.status = "running"
            training.updated_at = utcnow()
        progress(0.02, "Validating XTTS trainer")
        try:
            success, message = xtts_trainer_handler.start_training(
                {
                    **settings,
                    "model_name": model_name,
                    "source_audio_path": str(source_path),
                    "source_text_path": source_text_path,
                },
                output_callback=lambda line: progress(0.5, line[-500:]),
                status_callback=lambda line: progress(0.25, line[-500:]),
                stop_event=cancel_event,
            )
            if cancel_event.is_set():
                with self.database.session() as session:
                    training = session.get(TrainingRun, training_id)
                    training.status = "canceled"
                return {}
            if not success:
                raise RuntimeError(message)
            manifest_dir = self.paths.models / "xtts" / model_name
            manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest = manifest_dir / "pandrator-training.json"
            manifest.write_text(
                json.dumps({"kind": "xtts", "model_name": model_name, "message": message}, indent=2) + "\n",
                encoding="utf-8",
            )
            artifact = self.artifacts.register(
                manifest,
                kind="model",
                role="xtts_model",
                parent_ids=[source_artifact.id] + ([source_text_id] if source_text_id else []),
                settings=settings,
                metadata={"model_name": model_name},
            )
            with self.database.session() as session:
                training = session.get(TrainingRun, training_id)
                training.status = "succeeded"
                training.output_artifact_id = artifact.id
                training.updated_at = utcnow()
                defaults = session.get(AppSetting, "services.tts")
                value = dict(defaults.value_json or {}) if defaults and isinstance(defaults.value_json, dict) else {}
                providers = [dict(item) for item in value.get("provider_configs", []) if isinstance(item, dict)]
                xtts = next((item for item in providers if str(item.get("id") or "").lower() == "xtts"), None)
                if xtts is None:
                    xtts = {"id": "xtts", "name": "XTTS", "models": []}
                    providers.append(xtts)
                xtts["models"] = list(dict.fromkeys([*(xtts.get("models") or []), model_name]))
                if defaults is None:
                    session.add(AppSetting(key="services.tts", value_json={**value, "provider_configs": providers}, revision=1))
                else:
                    session.add(AppSettingHistory(key=defaults.key, value_json=defaults.value_json, revision=defaults.revision))
                    defaults.value_json = {**value, "provider_configs": providers}
                    defaults.revision += 1
                    defaults.updated_at = utcnow()
            progress(1.0, "XTTS model ready")
            return {"training_id": training_id, "artifact_id": artifact.id, "model_name": model_name, "message": message}
        except Exception as error:
            with self.database.session() as session:
                training = session.get(TrainingRun, training_id)
                if training is not None:
                    training.status = "failed"
                    training.error_message = str(error)
                    training.updated_at = utcnow()
            raise

    def clean_source(self, payload, progress, cancel_event):
        """Run deterministic extraction and the optional auditable agentic pipeline."""
        from pandrator.logic import file_handler
        from pandrator.logic import source_cleaning

        session_id = str(payload.get("session_id") or "")
        agent_run_id = str(payload.get("agent_run_id") or "")
        if agent_run_id:
            with self.database.session() as session:
                run = session.get(AgentRun, agent_run_id)
                if run is not None:
                    run.status = "running"
                    run.updated_at = utcnow()
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        settings = dict(payload.get("settings") or {})
        pdf_config = source_cleaning.PDFIngestionConfig(
            ocr_mode=str(settings.get("pdf_ocr_mode") or "auto"),
            ocr_language=str(settings.get("pdf_ocr_language") or "auto"),
            ocr_dpi=int(settings.get("pdf_ocr_dpi") or 200),
        )
        deterministic_operations: list[dict[str, Any]] = []
        baseline_text = ""
        progress(0.05, "Extracting source text")
        extension = source_path.suffix.lower()
        if extension == ".txt":
            cleaned_text = source_path.read_text(encoding="utf-8-sig")
            baseline_text = cleaned_text
        elif extension == ".epub":
            cleaned_text = file_handler.extract_text_from_epub(
                str(source_path),
                remove_footnotes=bool(settings.get("remove_footnotes", False)),
                filter_citations=bool(settings.get("filter_citations", True)),
            )
            baseline_text = cleaned_text
        elif extension == ".pdf":
            document = source_cleaning.build_source_document(
                str(source_path),
                pdf_config=pdf_config,
                artifact_dir=str(self._session_dir(session_id) / "source_ingestion"),
                progress_callback=lambda message: progress(0.35, str(message)),
            )
            deterministic_operations = source_cleaning.propose_deterministic_operations(
                document,
                remove_footnotes=bool(settings.get("remove_footnotes", False)),
                remove_toc=bool(settings.get("pdf_remove_toc", True)),
                remove_repeated_marginals=bool(settings.get("pdf_remove_repeated_marginals", True)),
            )
            baseline_text = document.plain_text()
            cleaned_text = source_cleaning.apply_cleaning_operations(document, deterministic_operations).cleaned_text
        elif extension in {".docx", ".mobi"}:
            extracted = self._session_dir(session_id) / f"{source_path.stem}_extracted.txt"
            if not file_handler.convert_doc_to_text(str(source_path), str(extracted)):
                raise RuntimeError(f"Could not extract text from {source_path.name}.")
            cleaned_text = extracted.read_text(encoding="utf-8-sig")
            baseline_text = cleaned_text
        else:
            raise ValueError(f"Unsupported document type: {extension or 'unknown'}")
        if cancel_event.is_set():
            return {}
        extraction = "deterministic"
        report: dict[str, Any] = {}
        if bool(settings.get("agentic", False)):
            from .provider_settings import build_llm_settings

            progress(0.4, "Building source-cleaning index")
            if extension in {".epub", ".pdf"}:
                document = source_cleaning.build_source_document(
                    str(source_path),
                    pdf_config=pdf_config if extension == ".pdf" else None,
                    extracted_text=cleaned_text if extension == ".epub" else None,
                    artifact_dir=str(self._session_dir(session_id) / "source_ingestion"),
                    progress_callback=lambda message: progress(0.45, str(message)),
                )
            else:
                from pandrator.logic.source_cleaning.pdf_text_adapter import build_source_document_from_text

                document = build_source_document_from_text(
                    cleaned_text,
                    source_path=str(source_path),
                    filename=source_path.name,
                )
            llm_settings, model_name = build_llm_settings(
                self.database,
                self.paths,
                requested_model=str(settings.get("model_name") or settings.get("default_model") or ""),
                request_timeout_seconds=int(settings.get("request_timeout_seconds") or 600),
            )
            total_iterations = max(1, int(settings.get("max_iterations") or 53))
            phase_iterations = settings.get("phase_max_iterations")
            pipeline = source_cleaning.run_cleaning_pipeline(
                document,
                llm_settings=llm_settings,
                config=source_cleaning.SourceCleaningPipelineConfig(
                    model_name=model_name,
                    remove_footnotes=bool(settings.get("remove_footnotes", False)),
                    filter_citations=bool(settings.get("filter_citations", True)),
                    total_max_iterations=total_iterations,
                    phase_max_iterations=phase_iterations if isinstance(phase_iterations, dict) else None,
                    phase_names=settings.get("phase_names") if isinstance(settings.get("phase_names"), list) else None,
                ),
                progress_callback=lambda message: progress(0.45, str(message)),
                stop_event=cancel_event,
            )
            if cancel_event.is_set():
                return {}
            all_operations = [*deterministic_operations, *pipeline.all_operations]
            cleaning_result = source_cleaning.apply_cleaning_operations(document, all_operations)
            validation = source_cleaning.validate_cleaning_result(
                document,
                cleaning_result,
                remove_footnotes=bool(settings.get("remove_footnotes", False)),
            )
            cleaned_text = cleaning_result.cleaned_text
            report = {
                **cleaning_result.report,
                "pipeline": pipeline.to_dict(),
                "validation": validation.to_dict(),
                "warnings": pipeline.warnings + validation.warnings + cleaning_result.warnings,
            }
            audit_dir = self._session_dir(session_id) / "source_cleaning"
            source_cleaning.write_cleaning_artifacts(
                document,
                all_operations,
                cleaning_result,
                str(audit_dir),
            )
            usage = pipeline.llm_usage
            models = list(usage.get("models") or [])
            details = usage.get("token_details") if isinstance(usage.get("token_details"), dict) else {}
            with self.database.session() as session:
                session.add(
                    UsageEvent(
                        session_id=session_id,
                        stage="source_cleaning",
                        provider_key=(models[0].split("/", 1)[0] if models else model_name.split("/", 1)[0]),
                        model_id=(models[0] if models else model_name),
                        input_tokens=int(usage.get("prompt_tokens") or 0),
                        cached_input_tokens=int(details.get("cached_tokens") or 0),
                        output_tokens=int(usage.get("completion_tokens") or 0),
                        cost_usd=float(usage["cost_usd"]) if usage.get("cost_usd") is not None else None,
                        cost_source=",".join(usage.get("cost_sources") or []) or None,
                        raw_usage_json=usage,
                    )
                )
            extraction = "agentic"
        comparison_dir = self._session_dir(session_id) / "source_cleaning"
        comparison_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = comparison_dir / f"extracted-{new_id()}.txt"
        baseline_path.write_text(baseline_text, encoding="utf-8", newline="\n")
        baseline_artifact = self.artifacts.register(
            baseline_path,
            kind="text",
            role="extracted_text",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            metadata={"comparison_source": True, "source_filename": source_path.name},
        )
        destination = self._session_dir(session_id) / f"{source_path.stem}_cleaned.txt"
        destination.write_text(cleaned_text, encoding="utf-8", newline="\n")
        artifact = self.artifacts.register(
            destination,
            kind="text",
            role="clean_text",
            session_id=session_id,
            parent_ids=[source_artifact.id, baseline_artifact.id],
            settings=settings,
            metadata={"extraction": extraction, "report": report},
        )
        if agent_run_id:
            pipeline_report = report.get("pipeline") if isinstance(report.get("pipeline"), dict) else {}
            phases = pipeline_report.get("phases") if isinstance(pipeline_report.get("phases"), list) else []
            with self.database.session() as session:
                run = session.get(AgentRun, agent_run_id)
                if run is not None:
                    run.status = "completed"
                    run.result_artifact_id = artifact.id
                    run.updated_at = utcnow()
                    for ordinal, phase in enumerate(phases):
                        safe_phase = phase if isinstance(phase, dict) else {"name": str(phase)}
                        operations = safe_phase.get("operations") if isinstance(safe_phase.get("operations"), list) else []
                        warnings = safe_phase.get("warnings") if isinstance(safe_phase.get("warnings"), list) else []
                        operation_types = sorted(
                            {
                                str(item.get("type") or item.get("operation") or "edit")
                                for item in operations
                                if isinstance(item, dict)
                            }
                        )
                        session.add(
                            AgentStep(
                                agent_run_id=agent_run_id,
                                ordinal=ordinal,
                                phase=str(safe_phase.get("name") or safe_phase.get("phase") or f"Phase {ordinal + 1}"),
                                status=str(safe_phase.get("status") or "completed"),
                                summary=str(safe_phase.get("summary") or f"{len(operations)} proposed operation(s), {len(warnings)} warning(s)."),
                                input_json={"operation_count": len(operations)},
                                output_json={"warnings": warnings, "operation_types": operation_types},
                            )
                        )
        progress(1.0, "Source text ready")
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "characters": len(cleaned_text), "report": report}

    def prepare_text(self, payload, progress, cancel_event):
        from pandrator.logic.text_preprocessor import preprocess_text

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        settings = dict(payload.get("settings") or {})
        if source_path.suffix.lower() not in {".txt", ".md"}:
            raise ValueError("Prepare narration requires a cleaned text artifact.")
        text = source_path.read_text(encoding="utf-8-sig")
        record = self._session_record(session_id)
        source_language = str(record.source_language or "auto")
        if source_language == "auto":
            source_language = "en"
        progress(0.1, "Segmenting narration")
        prepared = preprocess_text(
            text,
            {
                "source_file": str(source_path),
                "language": source_language,
                # Segmentation is intentionally provider-independent.  This
                # selects the shared multilingual sentence tokenizer only.
                "tts_service": "XTTS",
                "max_sentence_length": int(settings.get("max_sentence_length") or 160),
                "enable_sentence_splitting": bool(settings.get("enable_sentence_splitting", True)),
                "enable_sentence_appending": bool(settings.get("enable_sentence_appending", True)),
                "enable_nemo_normalization": bool(settings.get("enable_nemo_normalization", True)),
                "remove_diacritics": bool(settings.get("remove_diacritics", False)),
                "remove_quotation_marks": bool(settings.get("remove_quotation_marks", False)),
                "normalize_all_caps": bool(settings.get("normalize_all_caps", False)),
            },
        )
        if cancel_event.is_set():
            return {}
        destination = self._session_dir(session_id) / "prepared_narration.json"
        destination.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        artifact = self.artifacts.register(
            destination,
            kind="json",
            role="prepared_text",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={"segment_count": len(prepared)},
        )
        generation_revision_id, _segment_ids = self._store_generation_plan(
            session_id,
            prepared,
            settings=settings,
            source_revision_id=str((source_artifact.metadata_json or {}).get("revision_id") or "") or None,
        )
        progress(1.0, "Narration segments ready")
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "segments": len(prepared), "generation_plan_revision_id": generation_revision_id}

    def _store_generation_plan(
        self,
        session_id: str,
        records: list[dict[str, Any]],
        *,
        settings: dict[str, Any],
        source_revision_id: str | None = None,
    ) -> tuple[str, list[str]]:
        clean = [item for item in records if str(item.get("text") or item.get("original_sentence") or "").strip()]
        digest = hashlib.sha256(json.dumps({"records": clean, "settings": settings}, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        with self.database.session() as session:
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
            if plan is None:
                plan = GenerationPlan(session_id=session_id)
                session.add(plan)
                session.flush()
            maximum = session.scalar(select(func.max(GenerationPlanRevision.revision_number)).where(GenerationPlanRevision.plan_id == plan.id)) or 0
            revision = GenerationPlanRevision(plan_id=plan.id, source_revision_id=source_revision_id, revision_number=int(maximum) + 1, settings_json=settings, content_hash=digest)
            session.add(revision)
            session.flush()
            segment_ids = []
            for ordinal, record in enumerate(clean):
                explicit_silence = record.get("silence_after_ms")
                if explicit_silence is None:
                    is_paragraph = str(record.get("paragraph") or "").lower() == "yes"
                    explicit_silence = (
                        settings.get("paragraph_silence_ms", settings.get("silence_for_paragraphs", 700))
                        if is_paragraph
                        else settings.get("sentence_silence_ms", settings.get("silence_between_sentences", 250))
                    )
                segment = GenerationSegment(
                    plan_revision_id=revision.id,
                    ordinal=ordinal,
                    source_segment_ids_json=list(record.get("source_segment_ids") or []),
                    node_kind=str(record.get("node_kind") or ("chapter_marker" if str(record.get("chapter") or "").lower() == "yes" else "paragraph")),
                    paragraph_break_after=bool(record.get("paragraph_break_after", str(record.get("paragraph") or "").lower() == "yes")),
                    text=str(record.get("text") or record.get("original_sentence") or "").strip(),
                    language=str(record.get("language") or settings.get("language") or settings.get("target_language") or "") or None,
                    silence_after_ms=max(0, int(explicit_silence or 0)),
                    marked=bool(record.get("marked", False)),
                )
                session.add(segment)
                session.flush()
                segment_ids.append(segment.id)
            plan.active_revision_id = revision.id
            plan.updated_at = utcnow()
            return revision.id, segment_ids

    @staticmethod
    def _tts_urls(settings: dict[str, Any]) -> dict[str, str]:
        return {
            key: str(settings.get(setting_key) or default)
            for key, setting_key, default in (
                ("xtts_base_url", "xtts_base_url", "http://127.0.0.1:8020"),
                ("voxcpm_base_url", "voxcpm_base_url", "http://127.0.0.1:8020"),
                ("fishs2_base_url", "fishs2_base_url", "http://127.0.0.1:8020"),
                ("voxtral_base_url", "voxtral_base_url", "http://127.0.0.1:8000"),
                ("kokoro_base_url", "kokoro_base_url", "http://127.0.0.1:8880"),
                ("silero_base_url", "silero_base_url", "http://127.0.0.1:8001"),
                ("chatterbox_base_url", "chatterbox_base_url", "http://127.0.0.1:8040"),
                ("kobold_qwen_base_url", "kobold_qwen_base_url", "http://127.0.0.1:8042"),
                ("magpie_base_url", "magpie_base_url", "http://127.0.0.1:8030"),
            )
        }

    @staticmethod
    def _optimization_text_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _optimize_generation_texts(
        self,
        session_id: str,
        segment_ids: list[str],
        texts: list[str],
        settings: dict[str, Any],
        cancel_event,
        progress,
    ) -> tuple[list[str], str]:
        """Resolve reviewed or newly batched inline optimization for generation."""
        if not bool(settings.get("llm_tts_optimization")):
            return list(texts), ""

        from types import SimpleNamespace

        from .tts_optimization import optimize_texts

        resolved = self._with_database_llm_settings(dict(settings), "tts_optimization")
        llm_settings = SimpleNamespace(
            provider_configs=resolved["llm_provider_configs"],
            default_model=resolved["llm_default_model"],
            request_timeout_seconds=resolved["request_timeout_seconds"],
        )
        model_name = str(resolved.get("tts_optimization_model") or resolved["llm_default_model"])
        output = list(texts)
        pending_texts: list[str] = []
        pending_positions: list[int] = []
        with self.database.session() as session:
            for position, (segment_id, text) in enumerate(zip(segment_ids, texts)):
                segment = session.get(GenerationSegment, segment_id)
                source_hash = self._optimization_text_hash(text)
                if (
                    segment is not None
                    and segment.optimized_text
                    and segment.optimization_source_hash == source_hash
                    and segment.optimization_status in {"optimized", "reviewed"}
                ):
                    output[position] = segment.optimized_text
                    continue
                if segment is not None:
                    segment.optimization_status = "running"
                    segment.optimization_reviewed = False
                    segment.optimization_model = model_name
                    segment.updated_at = utcnow()
                pending_positions.append(position)
                pending_texts.append(text)

        if not pending_texts:
            return output, model_name

        def persist_batch(items: list[tuple[int, str]]) -> None:
            with self.database.session() as session:
                for local_index, revised in items:
                    position = pending_positions[local_index]
                    output[position] = revised
                    segment = session.get(GenerationSegment, segment_ids[position])
                    if segment is None:
                        continue
                    segment.optimized_text = revised
                    segment.optimization_status = "optimized"
                    segment.optimization_source_hash = self._optimization_text_hash(texts[position])
                    segment.optimization_reviewed = False
                    segment.optimization_model = model_name
                    segment.updated_at = utcnow()

        try:
            optimized, usage = optimize_texts(
                pending_texts,
                resolved,
                llm_settings,
                model_name,
                cancel_event,
                progress,
                on_batch=persist_batch,
            )
        except Exception:
            with self.database.session() as session:
                for position in pending_positions:
                    segment = session.get(GenerationSegment, segment_ids[position])
                    if segment is not None and segment.optimization_status == "running":
                        segment.optimization_status = "failed"
                        segment.updated_at = utcnow()
            raise
        for local_index, revised in enumerate(optimized):
            output[pending_positions[local_index]] = revised
        self._record_usage(session_id, "tts_optimization", resolved, usage)
        return output, model_name

    def _generate_audio(self, session_id: str, source_artifact: Artifact, source_path: Path, settings: dict[str, Any], progress, cancel_event, *, role: str) -> dict[str, Any]:
        from pydub import AudioSegment
        from pandrator.logic import tts_handler

        settings = hydrate_tts_settings(self.database, self.paths, settings)

        if source_path.suffix.lower() != ".json":
            raise ValueError("Audio generation requires segmented narration. Run Segment narration first.")
        try:
            records = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as error:
            raise ValueError("The segmented narration artifact is invalid JSON. Run Segment narration again.") from error
        if not isinstance(records, list) or not records:
            raise ValueError("No narration segments were found.")
        records = [record for record in records if str(record.get("text") or record.get("original_sentence") or "").strip()]
        if not records:
            raise ValueError("No non-empty narration segments were found.")
        audio_parts: list[tuple[AudioSegment, int]] = []
        revision_id = ""
        generation_segment_ids: list[str] = []
        if source_artifact.role == "prepared_text":
            with self.database.session() as session:
                plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
                if plan and plan.active_revision_id:
                    segments = list(session.scalars(select(GenerationSegment).where(GenerationSegment.plan_revision_id == plan.active_revision_id, GenerationSegment.removed.is_(False)).order_by(GenerationSegment.ordinal)).all())
                    if segments:
                        revision_id = plan.active_revision_id
                        generation_segment_ids = [segment.id for segment in segments]
                        records = [
                            {
                                "text": segment.text,
                                "language": segment.language,
                                "node_kind": segment.node_kind,
                                "paragraph_break_after": segment.paragraph_break_after,
                                "silence_after_ms": segment.silence_after_ms,
                                "source_segment_ids": segment.source_segment_ids_json,
                            }
                            for segment in segments
                        ]
        if not revision_id:
            revision_id, generation_segment_ids = self._store_generation_plan(
                session_id,
                records,
                settings=settings,
                source_revision_id=str((source_artifact.metadata_json or {}).get("revision_id") or "") or None,
            )
        source_texts = [str(record.get("text") or record.get("original_sentence") or "").strip() for record in records]
        optimization_share = 0.25 if bool(settings.get("llm_tts_optimization")) else 0.0
        optimized_texts, optimization_model = self._optimize_generation_texts(
            session_id,
            generation_segment_ids,
            source_texts,
            settings,
            cancel_event,
            lambda value, detail=None: progress(float(value) * optimization_share, detail),
        )
        for index, (record, generation_segment_id) in enumerate(zip(records, generation_segment_ids), start=1):
            if cancel_event.is_set():
                return {}
            text = str(record.get("text") or record.get("original_sentence") or "").strip()
            if not text:
                continue
            synthesis_share = 1.0 - optimization_share
            progress(optimization_share + ((index - 1) / len(records)) * synthesis_share, f"Generating segment {index} of {len(records)}")
            synthesized_text = optimized_texts[index - 1]
            audio = tts_handler.text_to_audio(synthesized_text, settings, max_attempts=int(settings.get("max_attempts") or 3), **self._tts_urls(settings))
            if audio is None:
                raise RuntimeError(f"Speech generation failed at segment {index}.")
            take_dir = self._session_dir(session_id) / "generation" / revision_id / generation_segment_id
            take_dir.mkdir(parents=True, exist_ok=True)
            sentence_path = take_dir / f"tts-{new_id()}.wav"
            exported = audio.export(sentence_path, format="wav")
            exported.close()
            take_artifact = self.artifacts.register(
                sentence_path,
                kind="audio",
                role="generation_take",
                session_id=session_id,
                parent_ids=[source_artifact.id],
                settings=settings,
                metadata={
                    "generation_segment_id": generation_segment_id,
                    "kind": "tts",
                    "source_text": text,
                    "synthesized_text": synthesized_text,
                    "llm_optimized": synthesized_text != text,
                    "llm_model": optimization_model or None,
                },
            )
            with self.database.session() as session:
                segment = session.get(GenerationSegment, generation_segment_id)
                segment.status = "completed"
                session.add(AudioTake(generation_segment_id=generation_segment_id, artifact_id=take_artifact.id, kind="tts", status="completed", settings_hash=take_artifact.settings_hash, duration_ms=len(audio), is_active=True))
            silence_after = record.get("silence_after_ms")
            if silence_after is None:
                silence_after = (
                    settings.get("paragraph_silence_ms", settings.get("silence_for_paragraphs", 700))
                    if str(record.get("paragraph") or "").lower() == "yes"
                    else settings.get("sentence_silence_ms", settings.get("silence_between_sentences", 250))
                )
            audio_parts.append((audio, max(0, int(silence_after or 0))))
            progress(optimization_share + (index / len(records)) * synthesis_share, f"Generated segment {index} of {len(records)}")
        if not audio_parts:
            raise RuntimeError("The speech service returned no audio.")
        from .audio_assembly import compose_audio

        combined = compose_audio(audio_parts, settings)
        destination = self._session_dir(session_id) / ("dubbing_audio.wav" if role == "dubbing_audio" else "audiobook_audio.wav")
        exported = combined.export(destination, format="wav")
        exported.close()
        artifact = self.artifacts.register(
            destination,
            kind="audio",
            role=role,
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={"segment_count": len(records), "service": settings.get("service") or settings.get("tts_service") or "XTTS"},
        )
        progress(1.0, "Audio ready")
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "segments": len(records), "generation_plan_revision_id": revision_id}

    def generate_dubbing_audio(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.speech_blocks import generate_speech_blocks_file

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        settings = dict(payload.get("settings") or {})
        if source_path.suffix.lower() != ".srt":
            raise ValueError("Dubbing audio requires a transcription, correction, or translation SRT artifact.")
        blocks_path = Path(
            generate_speech_blocks_file(
                str(self._session_dir(session_id)),
                str(source_path),
                target_language=str(settings.get("target_language") or "en"),
                min_chars=int(settings.get("speech_block_min_chars") or 10),
                max_chars=int(settings.get("speech_block_max_chars") or 160),
                merge_threshold=int(
                    settings.get("speech_block_merge_threshold")
                    if settings.get("speech_block_merge_threshold") is not None
                    else settings.get("subtitle_merge_threshold", 250)
                ),
            )
        )
        blocks_artifact = self.artifacts.register(
            blocks_path,
            kind="json",
            role="speech_blocks",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
        )
        return self._generate_audio(session_id, blocks_artifact, blocks_path, settings, progress, cancel_event, role="dubbing_audio")

    def generate_audiobook_audio(self, payload, progress, cancel_event):
        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        settings = dict(payload.get("settings") or {})
        if source_artifact.role not in {"prepared_text", "tts_optimized"}:
            raise ValueError("Audiobook generation requires a current Segment narration artifact or its reviewed speech-optimized revision.")
        return self._generate_audio(session_id, source_artifact, source_path, settings, progress, cancel_event, role="audiobook_audio")

    def _run_reviewable_generation(
        self,
        payload: dict[str, Any],
        progress,
        cancel_event,
        *,
        resolved_snapshot: Any = None,
        settings_hash: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Create/resolve the segment plan and generate takes without assembly.

        The workflow card and the generation drawer must describe the same
        operation.  The former compatibility path generated a combined WAV in
        the workflow job, leaving no GenerationRun for the drawer to observe.
        This boundary deliberately stops after immutable per-segment takes;
        output assembly remains an explicit review action.
        """
        from pandrator.logic.dubbing.speech_blocks import generate_speech_blocks_file

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        settings = dict(payload.get("settings") or {})
        progress(0.0, "Preparing generation segments")

        plan_revision_id: str | None = None
        if source_path.suffix.lower() == ".srt":
            blocks_path = Path(
                generate_speech_blocks_file(
                    str(self._session_dir(session_id)),
                    str(source_path),
                    target_language=str(settings.get("target_language") or settings.get("language") or "en"),
                    min_chars=int(settings.get("speech_block_min_chars") or 10),
                    max_chars=int(settings.get("speech_block_max_chars") or 160),
                    merge_threshold=int(
                        settings.get("speech_block_merge_threshold")
                        if settings.get("speech_block_merge_threshold") is not None
                        else settings.get("subtitle_merge_threshold", 250)
                    ),
                )
            )
            records = json.loads(blocks_path.read_text(encoding="utf-8-sig"))
            if not isinstance(records, list) or not records:
                raise ValueError("No dubbing speech blocks were produced.")
            self.artifacts.register(
                blocks_path,
                kind="json",
                role="speech_blocks",
                session_id=session_id,
                parent_ids=[source_artifact.id],
                settings=settings,
            )
            plan_revision_id, _ = self._store_generation_plan(
                session_id,
                records,
                settings=settings,
                source_revision_id=str((source_artifact.metadata_json or {}).get("revision_id") or "") or None,
            )
        elif source_path.suffix.lower() == ".json":
            # Segment narration already creates a plan. Preserve any edits the
            # user made in the drawer. A separately reviewed optimization
            # artifact, however, is a new source and therefore a new plan.
            with self.database.session() as session:
                plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
                if source_artifact.role == "prepared_text" and plan is not None:
                    plan_revision_id = plan.active_revision_id
            if not plan_revision_id:
                records = json.loads(source_path.read_text(encoding="utf-8-sig"))
                if not isinstance(records, list) or not records:
                    raise ValueError("No narration segments were found.")
                plan_revision_id, _ = self._store_generation_plan(
                    session_id,
                    records,
                    settings=settings,
                    source_revision_id=str((source_artifact.metadata_json or {}).get("revision_id") or "") or None,
                )
        else:
            raise ValueError("Audio generation requires subtitle cues or segmented narration.")

        if not plan_revision_id:
            raise ValueError("Create generation segments before starting audio generation.")

        snapshot = deepcopy(resolved_snapshot) if isinstance(resolved_snapshot, dict) else {}
        # The resolved sections are the immutable source of truth. Merge the
        # flattened stage values as compatibility aliases so direct Run Now
        # choices (service, model, voice, and language) cannot be lost.
        snapshot["tts"] = {**dict(snapshot.get("tts") or {}), **settings}
        snapshot["audio"] = {**dict(snapshot.get("audio") or {}), **settings}
        snapshot["text"] = {
            **dict(snapshot.get("text") or {}),
            "llm_tts_optimization": bool(settings.get("llm_tts_optimization")),
        }
        frozen_hash = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        with self.database.session() as session:
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=plan_revision_id,
                job_id=job_id,
                status="queued",
                settings_snapshot_json=snapshot,
                settings_hash=frozen_hash or settings_hash,
            )
            session.add(run)
            session.flush()
            run_id = run.id

        progress(0.03, "Generation segments ready")
        try:
            return self.run_generation(
                {"generation_run_id": run_id, "segment_ids": [], "operation": "generate"},
                lambda value, detail=None: progress(0.03 + max(0.0, min(1.0, float(value))) * 0.97, detail),
                cancel_event,
            )
        except Exception:
            with self.database.session() as session:
                failed = session.get(GenerationRun, run_id)
                if failed is not None:
                    failed.status = "failed"
                    failed.updated_at = utcnow()
            raise

    def run_generation(self, payload, progress, cancel_event):
        """Generate immutable per-segment takes with safe pause and resume boundaries."""
        from pydub import AudioSegment
        from pandrator.logic import rvc_handler, tts_handler

        run_id = str(payload.get("generation_run_id") or "")
        selected_ids = {str(value) for value in (payload.get("segment_ids") or []) if str(value)}
        operation = str(payload.get("operation") or "generate")
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            run.status = "running"
            run.updated_at = utcnow()
            settings_snapshot = dict(run.settings_snapshot_json or {})
            session_id = run.session_id
            plan_revision_id = run.plan_revision_id

        statement = select(GenerationSegment).where(
            GenerationSegment.plan_revision_id == plan_revision_id,
            GenerationSegment.removed.is_(False),
        ).order_by(GenerationSegment.ordinal)
        if selected_ids:
            statement = statement.where(GenerationSegment.id.in_(selected_ids))
        with self.database.session() as session:
            selected_segments = list(session.scalars(statement).all())
            segment_ids = [item.id for item in selected_segments]
            source_texts = [item.text for item in selected_segments]
        if not segment_ids:
            with self.database.session() as session:
                run = session.get(GenerationRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.updated_at = utcnow()
            raise ValueError("No generation segments match this request.")

        from .workspace import adapt_runtime_settings, mark_output_assemblies_stale

        tts_settings = {
            **adapt_runtime_settings("tts", dict(settings_snapshot.get("tts") or {})),
            **adapt_runtime_settings("audio", dict(settings_snapshot.get("audio") or {})),
        }
        tts_settings = hydrate_tts_settings(self.database, self.paths, tts_settings)
        text_settings = adapt_runtime_settings("text", dict(settings_snapshot.get("text") or {}))
        optimization_model = ""
        optimized_by_id: dict[str, str] = {}
        optimization_share = 0.2 if operation != "rvc" and bool(text_settings.get("llm_tts_optimization")) else 0.0
        if operation != "rvc":
            try:
                optimized, optimization_model = self._optimize_generation_texts(
                    session_id,
                    segment_ids,
                    source_texts,
                    {**text_settings, **tts_settings},
                    cancel_event,
                    lambda value, detail=None: progress(float(value) * optimization_share, detail),
                )
                optimized_by_id = dict(zip(segment_ids, optimized))
            except Exception:
                with self.database.session() as session:
                    run = session.get(GenerationRun, run_id)
                    if run is not None:
                        run.status = "failed"
                        run.updated_at = utcnow()
                raise
        rvc_settings = adapt_runtime_settings("rvc", dict(settings_snapshot.get("rvc") or {}))
        generated = 0
        skipped = 0
        for index, segment_id in enumerate(segment_ids):
            with self.database.session() as session:
                run = session.get(GenerationRun, run_id)
                segment = session.get(GenerationSegment, segment_id)
                if run.cancel_requested or cancel_event.is_set():
                    run.status = "canceled"
                    run.updated_at = utcnow()
                    return {"generation_run_id": run_id, "status": "canceled", "generated": generated}
                if run.pause_requested:
                    run.status = "paused"
                    run.updated_at = utcnow()
                    return {"generation_run_id": run_id, "status": "paused", "generated": generated}
                if operation == "resume" and segment.status == "completed":
                    skipped += 1
                    progress(
                        optimization_share + ((index + 1) / len(segment_ids)) * (1.0 - optimization_share),
                        f"Kept completed segment {index + 1} of {len(segment_ids)}",
                    )
                    continue
                segment.status = "running"
                segment.updated_at = utcnow()
                text = segment.text
            progress(
                optimization_share + (index / len(segment_ids)) * (1.0 - optimization_share),
                f"Generating segment {index + 1} of {len(segment_ids)}",
            )
            try:
                synthesized_text = text
                if operation == "rvc":
                    with self.database.session() as session:
                        source_take = session.scalar(select(AudioTake).where(AudioTake.generation_segment_id == segment_id, AudioTake.is_active.is_(True), AudioTake.status == "completed").order_by(AudioTake.created_at.desc()))
                        if source_take is None or source_take.artifact_id is None:
                            raise ValueError("The selected segment has no active audio take for RVC.")
                        source_artifact = session.get(Artifact, source_take.artifact_id)
                        source_take_id = source_take.id
                    source_path = self.paths.managed_path(source_artifact.relative_path)
                    source_audio = AudioSegment.from_file(source_path)
                    audio = rvc_handler.process_with_rvc(source_audio, rvc_settings)
                    take_kind = "rvc"
                    parent_take_id = source_take_id
                    take_settings = rvc_settings
                else:
                    synthesized_text = optimized_by_id.get(segment_id, text)
                    audio = tts_handler.text_to_audio(synthesized_text, tts_settings, max_attempts=int(tts_settings.get("max_attempts") or 3), **self._tts_urls(tts_settings))
                    if audio is None:
                        raise RuntimeError("The speech service returned no audio.")
                    take_kind = "tts"
                    parent_take_id = None
                    take_settings = tts_settings
                take_dir = self._session_dir(session_id) / "generation" / plan_revision_id / segment_id
                take_dir.mkdir(parents=True, exist_ok=True)
                take_path = take_dir / f"{take_kind}-{new_id()}.wav"
                exported = audio.export(take_path, format="wav")
                exported.close()
                artifact = self.artifacts.register(
                    take_path,
                    kind="audio",
                    role="generation_take",
                    session_id=session_id,
                    parent_ids=[source_artifact.id] if operation == "rvc" else [],
                    settings=take_settings,
                    metadata={
                        "generation_segment_id": segment_id,
                        "kind": take_kind,
                        "source_text": text,
                        "synthesized_text": synthesized_text,
                        "llm_optimized": operation != "rvc" and synthesized_text != text,
                        "llm_model": optimization_model or None,
                    },
                )
                with self.database.session() as session:
                    segment = session.get(GenerationSegment, segment_id)
                    for previous in session.scalars(select(AudioTake).where(AudioTake.generation_segment_id == segment_id, AudioTake.is_active.is_(True))).all():
                        previous.is_active = False
                        previous.revision += 1
                    session.add(
                        AudioTake(
                            generation_segment_id=segment_id,
                            artifact_id=artifact.id,
                            parent_take_id=parent_take_id,
                            kind=take_kind,
                            status="completed",
                            settings_hash=artifact.settings_hash,
                            duration_ms=len(audio),
                            is_active=True,
                        )
                    )
                    segment.status = "completed"
                    segment.updated_at = utcnow()
                    mark_output_assemblies_stale(session, session_id)
                generated += 1
                progress(
                    optimization_share + ((index + 1) / len(segment_ids)) * (1.0 - optimization_share),
                    f"Generated segment {index + 1} of {len(segment_ids)}",
                )
            except Exception:
                with self.database.session() as session:
                    segment = session.get(GenerationSegment, segment_id)
                    if segment is not None:
                        segment.status = "failed"
                        segment.updated_at = utcnow()
                    run = session.get(GenerationRun, run_id)
                    if run is not None:
                        run.status = "failed"
                        run.updated_at = utcnow()
                raise

        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            run.status = "completed"
            run.updated_at = utcnow()
        progress(1.0, "Generation run complete")
        return {"generation_run_id": run_id, "status": "completed", "generated": generated, "skipped": skipped}

    def assemble_generation_output(self, payload, progress, cancel_event):
        """Assemble the current selected takes in plan order into an immutable artifact."""
        from pydub import AudioSegment

        from .audio_assembly import compose_audio, export_audio

        assembly_id = str(payload.get("output_assembly_id") or "")
        with self.database.session() as session:
            assembly = session.get(OutputAssembly, assembly_id)
            if assembly is None:
                raise KeyError(assembly_id)
            assembly.status = "running"
            assembly.error_message = None
            assembly.updated_at = utcnow()
            session_id = assembly.session_id
            settings_container = dict(assembly.settings_json or {})
            plan_revision_id = str(settings_container.get("plan_revision_id") or "")
            resolved = settings_container.get("resolved") if isinstance(settings_container.get("resolved"), dict) else {}
            audio_settings = dict(resolved.get("audio") or {})
            output_settings = dict(resolved.get("output") or {})

        try:
            with self.database.session() as session:
                segments = list(
                    session.scalars(
                        select(GenerationSegment)
                        .where(
                            GenerationSegment.plan_revision_id == plan_revision_id,
                            GenerationSegment.removed.is_(False),
                        )
                        .order_by(GenerationSegment.ordinal)
                    ).all()
                )
                selected: list[tuple[GenerationSegment, AudioTake, Artifact]] = []
                for segment in segments:
                    take = session.scalar(
                        select(AudioTake)
                        .where(
                            AudioTake.generation_segment_id == segment.id,
                            AudioTake.is_active.is_(True),
                        )
                        .order_by(AudioTake.created_at.desc())
                    )
                    if take is None or take.status != "completed" or not take.artifact_id:
                        raise ValueError(f"Segment {segment.ordinal + 1} has no current completed audio take.")
                    artifact = session.get(Artifact, take.artifact_id)
                    if artifact is None or artifact.state != "current":
                        raise ValueError(f"Segment {segment.ordinal + 1} references an unavailable audio artifact.")
                    session.expunge(segment)
                    session.expunge(take)
                    session.expunge(artifact)
                    selected.append((segment, take, artifact))
            if not selected:
                raise ValueError("No active generation segments are available for assembly.")

            parts: list[tuple[AudioSegment, int]] = []
            manifest: list[dict[str, Any]] = []
            chapter_markers: list[tuple[float, str]] = []
            timeline_ms = 0
            parent_ids: list[str] = []
            for index, (segment, take, artifact) in enumerate(selected):
                if cancel_event.is_set():
                    with self.database.session() as session:
                        assembly = session.get(OutputAssembly, assembly_id)
                        if assembly is not None:
                            assembly.status = "canceled"
                            assembly.error_message = None
                            assembly.updated_at = utcnow()
                    return {}
                progress(index / len(selected), f"Assembling segment {index + 1} of {len(selected)}")
                path = self.paths.managed_path(artifact.relative_path)
                if not path.is_file():
                    raise ValueError(f"Audio take file is missing for segment {segment.ordinal + 1}.")
                audio = AudioSegment.from_file(path)
                if segment.node_kind == "chapter_marker":
                    chapter_markers.append((timeline_ms / 1000, segment.text))
                parts.append((audio, segment.silence_after_ms))
                parent_ids.append(artifact.id)
                manifest.append(
                    {
                        "segment_id": segment.id,
                        "segment_revision": segment.revision,
                        "node_kind": segment.node_kind,
                        "take_id": take.id,
                        "take_revision": take.revision,
                        "artifact_id": artifact.id,
                        "kind": take.kind,
                        "duration_ms": len(audio),
                        "silence_after_ms": segment.silence_after_ms if index < len(selected) - 1 else 0,
                    }
                )
                timeline_ms += len(audio)
                if index < len(selected) - 1:
                    timeline_ms += max(0, int(segment.silence_after_ms or 0))
            combined = compose_audio(parts, audio_settings)
            output_format = str(output_settings.get("format") or "wav").lower()
            bitrate = str(output_settings.get("bitrate") or "192k")
            destination = self._session_dir(session_id) / "assemblies" / f"assembly-{assembly_id}.{output_format}"
            export_audio(combined, destination, output_format, bitrate)
            session_record = self._session_record(session_id)
            metadata = {
                "title": str(output_settings.get("title") or session_record.name),
                "artist": str(output_settings.get("artist") or ""),
                "album": str(output_settings.get("album") or ""),
                "genre": str(output_settings.get("genre") or ""),
                "language": str(output_settings.get("language") or ""),
            }
            cover_artifact_id = str(output_settings.get("cover_artifact_id") or "").strip()
            cover_path = None
            if cover_artifact_id:
                cover_artifact, candidate = self._resolve_input(cover_artifact_id)
                if cover_artifact.state != "current" or not candidate.is_file() or not str(cover_artifact.mime_type or "").startswith("image/"):
                    raise ValueError("The selected cover artifact is not an available image.")
                cover_path = candidate
                parent_ids.append(cover_artifact.id)
            from pandrator.logic.audio_processor import _add_chapters_to_m4b, _save_metadata_and_cover

            _save_metadata_and_cover(
                str(destination),
                output_format,
                metadata,
                str(cover_path) if cover_path else None,
                raise_on_error=True,
            )
            if output_format == "m4b" and chapter_markers:
                _add_chapters_to_m4b(
                    str(destination),
                    chapter_markers,
                    total_duration_sec=len(combined) / 1000,
                    raise_on_error=True,
                )
                _save_metadata_and_cover(
                    str(destination),
                    output_format,
                    metadata,
                    str(cover_path) if cover_path else None,
                    raise_on_error=True,
                )
            artifact = self.artifacts.register(
                destination,
                kind="audio",
                role="assembled_audio",
                session_id=session_id,
                parent_ids=parent_ids,
                settings={"audio": audio_settings, "output": output_settings, "takes": manifest},
                metadata={
                    "output_assembly_id": assembly_id,
                    "duration_ms": len(combined),
                    "segment_count": len(selected),
                    "format": output_format,
                    "bitrate": bitrate,
                    "metadata": metadata,
                    "cover_artifact_id": cover_artifact_id or None,
                    "chapters": [{"start_ms": int(start * 1000), "title": title} for start, title in chapter_markers],
                    "takes": manifest,
                },
            )
            with self.database.session() as session:
                assembly = session.get(OutputAssembly, assembly_id)
                assembly.artifact_id = artifact.id
                assembly.status = "completed"
                assembly.error_message = None
                assembly.settings_json = {**dict(assembly.settings_json or {}), "takes": manifest, "duration_ms": len(combined)}
                assembly.updated_at = utcnow()
            progress(1.0, "Output assembly ready")
            return {
                "output_assembly_id": assembly_id,
                "artifact_id": artifact.id,
                "duration_ms": len(combined),
                "segment_count": len(selected),
                "format": output_format,
            }
        except Exception as error:
            with self.database.session() as session:
                assembly = session.get(OutputAssembly, assembly_id)
                if assembly is not None:
                    assembly.status = "failed"
                    assembly.error_message = str(error)
                    assembly.updated_at = utcnow()
            raise

    def generate_waveform(self, payload, progress, cancel_event):
        """Create a compact, reusable peak artifact for browser review."""
        from pydub import AudioSegment

        source, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        max_points = max(128, min(5000, int(payload.get("max_points") or 1600)))
        progress(0.1, "Decoding audio for waveform")
        audio = AudioSegment.from_file(source_path)
        samples = audio.get_array_of_samples()
        channels = max(1, int(audio.channels or 1))
        frames = max(1, len(samples) // channels)
        frame_step = max(1, frames // max_points)
        ceiling = float(1 << (8 * audio.sample_width - 1))
        peaks: list[float] = []
        for start in range(0, frames, frame_step):
            if cancel_event.is_set():
                return {}
            end = min(frames, start + frame_step)
            value = 0
            for frame in range(start, end):
                offset = frame * channels
                value = max(value, *(abs(int(samples[offset + channel])) for channel in range(channels)))
            peaks.append(round(min(1.0, value / ceiling), 5))
            if len(peaks) % 200 == 0:
                progress(min(0.9, end / frames), "Calculating waveform peaks")
        destination_dir = self._session_dir(source.session_id) if source.session_id else self.paths.artifacts / "waveforms"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"waveform-{source.id}-{max_points}.json"
        destination.write_text(json.dumps({"duration_ms": len(audio), "channels": channels, "points": peaks}, separators=(",", ":")) + "\n", encoding="utf-8")
        artifact = self.artifacts.register(
            destination,
            kind="json",
            role="waveform_peaks",
            session_id=source.session_id,
            parent_ids=[source.id],
            settings={"max_points": max_points},
            metadata={"source_artifact_id": source.id, "duration_ms": len(audio)},
        )
        progress(1.0, "Waveform ready")
        return {"artifact_id": artifact.id, "source_artifact_id": source.id, "point_count": len(peaks)}

    def export(self, payload, progress, cancel_event):
        """Create immutable, managed exports without requiring generated audio."""
        from werkzeug.utils import secure_filename

        from pandrator.logic.dubbing.bilingual_ass import write_bilingual_ass
        from pandrator.logic.dubbing.subtitle_finalization import finalize_srt_file
        from pandrator.logic.dubbing.video_muxing import build_add_subtitles_command, build_multi_soft_subtitle_command, build_replace_video_audio_command

        session_id = str(payload.get("session_id") or "")
        settings = dict(payload.get("settings") or {})
        record = self._session_record(session_id)
        with self.database.session() as session:
            current = list(session.scalars(select(Artifact).where(Artifact.session_id == session_id, Artifact.state == "current")).all())
            for item in current:
                session.expunge(item)
        by_role = {item.role: item for item in current}
        output_dir = self._session_dir(session_id) / "exports"
        output_dir.mkdir(parents=True, exist_ok=True)
        export_name = secure_filename(record.name) or record.storage_key
        progress(0.1, "Preparing export")
        produced: list[Artifact] = []

        if record.workflow_kind == "audiobook":
            audio = by_role.get("assembled_audio") or by_role.get("audiobook_audio")
            if audio is None:
                raise ValueError("Audiobook export requires generated audio.")
            _audio_record, audio_path = self._resolve_input(audio.id)
            destination = output_dir / f"{export_name}{audio_path.suffix.lower()}"
            shutil.copy2(audio_path, destination)
            produced.append(self.artifacts.register(destination, kind="export", role="export", session_id=session_id, parent_ids=[audio.id], settings=settings))
        else:
            upload_media = next((item for item in reversed(current) if item.role == "upload" and Path(item.relative_path).suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}), None)
            translated = by_role.get("translation")
            source_subtitle = by_role.get("correction") or by_role.get("transcription") or next((item for item in current if item.role == "upload" and Path(item.relative_path).suffix.lower() == ".srt"), None)
            subtitle_mode = str(settings.get("subtitle_mode") or "none").lower()
            subtitle_selection = str(settings.get("subtitle_selection") or ("dual" if translated and source_subtitle else "translation")).lower()
            selected_subtitles = ([source_subtitle] if source_subtitle and subtitle_selection in {"source", "dual"} else []) + ([translated] if translated and subtitle_selection in {"translation", "dual"} else [])
            # For media, "none" means no subtitle track. For SRT/audio-only
            # sessions there is no mux target, so the selected SRT remains a
            # valid standalone export (and preserves the existing contract).
            selected_subtitles = [item for item in selected_subtitles if item] if subtitle_mode != "none" or upload_media is None else []
            finalized_subtitles: list[Artifact] = []
            for item in selected_subtitles:
                _subtitle_record, subtitle_path = self._resolve_input(item.id)
                track_name = "translation" if item.role == "translation" else "source"
                finalized_path = output_dir / f"{record.storage_key}_{track_name}_final.srt"
                finalize_srt_file(subtitle_path, finalized_path, settings)
                finalized = self.artifacts.register(
                    finalized_path,
                    kind="srt",
                    role=f"final_subtitle_{track_name}",
                    session_id=session_id,
                    parent_ids=[item.id],
                    settings=settings,
                    metadata={
                        "language": str((item.metadata_json or {}).get("language") or (settings.get("target_language") if track_name == "translation" else settings.get("original_language")) or "und"),
                        "source_role": item.role,
                    },
                )
                finalized_subtitles.append(finalized)
                produced.append(finalized)
            selected_subtitles = finalized_subtitles
            dubbing_audio = by_role.get("assembled_audio") or by_role.get("dubbing_audio")
            audio_mode = str(settings.get("audio_mode") or "source").lower()
            audio_mode = {"preserve": "source", "dubbing_only": "dubbed"}.get(audio_mode, audio_mode)

            def is_translation_track(item: Artifact) -> bool:
                return str((item.metadata_json or {}).get("source_role") or item.role) == "translation"

            if upload_media:
                _media_record, media_path = self._resolve_input(upload_media.id)
                working_video = media_path
                audio_parent_ids: list[str] = [upload_media.id]
                if dubbing_audio and audio_mode in {"dubbed", "mixed"}:
                    _audio_record, audio_path = self._resolve_input(dubbing_audio.id)
                    audio_video = output_dir / f".{record.storage_key}-audio.mp4"
                    if audio_mode == "dubbed":
                        command = build_replace_video_audio_command(str(media_path), str(audio_path), str(audio_video))
                    else:
                        command = ["ffmpeg", "-y", "-i", str(media_path), "-i", str(audio_path), "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=2[a]", "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", str(audio_video)]
                    subprocess.run(command, check=True, capture_output=True, text=True)
                    working_video = audio_video
                    audio_parent_ids.append(dubbing_audio.id)
                destination = output_dir / f"{export_name}.mp4"
                if subtitle_mode == "soft" and selected_subtitles:
                    tracks = []
                    for item in selected_subtitles:
                        _subtitle, subtitle_path = self._resolve_input(item.id)
                        translation_track = is_translation_track(item)
                        tracks.append({"path": str(subtitle_path), "language": str((item.metadata_json or {}).get("language") or (settings.get("target_language") if translation_track else "und") or "und"), "title": "Translation" if translation_track else "Source", "default": translation_track})
                    command = build_multi_soft_subtitle_command(str(working_video), tracks, str(destination))
                    subprocess.run(command, check=True, capture_output=True, text=True)
                elif subtitle_mode == "burned" and selected_subtitles:
                    subtitle_paths = [self._resolve_input(item.id)[1] for item in selected_subtitles]
                    burn_path = subtitle_paths[-1]
                    if len(subtitle_paths) == 2:
                        burn_path = Path(write_bilingual_ass(str(subtitle_paths[0]), str(subtitle_paths[1]), str(output_dir / "bilingual_subtitles.ass")))
                        self.artifacts.register(
                            burn_path,
                            kind="ass",
                            role="bilingual_subtitle_overlay",
                            session_id=session_id,
                            parent_ids=[item.id for item in selected_subtitles],
                            settings=settings,
                        )
                    command = build_add_subtitles_command(str(working_video), str(burn_path), str(destination), subtitle_mode="burned", subtitle_language=str(settings.get("target_language") or "und"))
                    subprocess.run(command, check=True, capture_output=True, text=True)
                else:
                    shutil.copy2(working_video, destination)
                produced.append(self.artifacts.register(destination, kind="export", role="export", session_id=session_id, parent_ids=audio_parent_ids + [item.id for item in selected_subtitles], settings=settings))
                if working_video != media_path and working_video.exists():
                    working_video.unlink()
            else:
                for item in selected_subtitles + ([dubbing_audio] if dubbing_audio and audio_mode != "source" else []):
                    if item is None:
                        continue
                    _artifact, item_path = self._resolve_input(item.id)
                    destination = (
                        output_dir / f"{export_name}{item_path.suffix.lower()}"
                        if item.role in {"assembled_audio", "dubbing_audio"}
                        else output_dir / item_path.name
                    )
                    if item_path.resolve() == destination.resolve():
                        continue
                    shutil.copy2(item_path, destination)
                    produced.append(self.artifacts.register(destination, kind="export", role=f"export_{item.role}", session_id=session_id, parent_ids=[item.id], settings=settings))
                if not produced:
                    raise ValueError("No subtitle or audio artifact is available to export.")
        progress(1.0, "Export ready")
        return {"artifact_ids": [item.id for item in produced], "paths": [item.relative_path for item in produced]}

    def apply_pdf_edits(self, payload, progress, cancel_event):
        from .pdf_editor import PdfEditPlan, apply_pdf_edit_plan

        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        if source_path.suffix.lower() != ".pdf":
            raise ValueError("PDF edit jobs require a PDF source artifact.")
        session_id = str(payload.get("session_id") or source_artifact.session_id or "") or None
        output_dir = self._session_dir(session_id) if session_id else self.paths.artifacts
        output_path = output_dir / f"{source_path.stem}_edited.pdf"
        suffix = 2
        while output_path.exists():
            output_path = output_dir / f"{source_path.stem}_edited_{suffix}.pdf"
            suffix += 1
        progress(0.1, "Validating PDF edit plan")
        plan = PdfEditPlan.from_value(dict(payload.get("plan") or {}))
        if cancel_event.is_set():
            return {}
        destination, manifest, provenance = apply_pdf_edit_plan(
            source_path,
            output_path,
            plan,
            parent_artifact_id=source_artifact.id,
        )
        progress(0.85, "Registering edited PDF")
        output_artifact = self.artifacts.register(
            destination,
            kind="pdf",
            role="pdf_edited",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            metadata={"provenance_manifest": self.paths.relative_managed_path(manifest)},
        )
        manifest_artifact = self.artifacts.register(
            manifest,
            kind="json",
            role="provenance",
            session_id=session_id,
            parent_ids=[source_artifact.id, output_artifact.id],
        )
        progress(1.0, "Edited PDF ready")
        return {
            "artifact_id": output_artifact.id,
            "manifest_artifact_id": manifest_artifact.id,
            "page_count": provenance["output"]["page_count"],
        }

    def export_session_bundle(self, payload, progress, cancel_event):
        from .bundles import SessionBundleService

        session_id = str(payload.get("session_id") or "")
        record = self._session_record(session_id)
        destination = self._session_dir(session_id) / "exports" / f"{record.storage_key}.pandrator-session"
        progress(0.05, "Collecting session records")
        result = SessionBundleService(self.database, self.paths).export_bundle(
            session_id,
            destination,
            include_sources=bool(payload.get("include_sources", True)),
        )
        if cancel_event.is_set():
            return {}
        artifact = self.artifacts.register(destination, kind="bundle", role="session_bundle", session_id=session_id)
        progress(1.0, "Session bundle ready")
        return {**result, "artifact_id": artifact.id}

    def import_session_bundle(self, payload, progress, cancel_event):
        from .bundles import SessionBundleService

        _artifact, source = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        progress(0.05, "Validating session bundle")
        if cancel_event.is_set():
            return {}
        result = SessionBundleService(self.database, self.paths).import_bundle(source, name=payload.get("name"))
        progress(1.0, "Session imported")
        return result
