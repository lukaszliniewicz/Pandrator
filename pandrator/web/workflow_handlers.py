"""Worker adapters that run existing Pandrator engines without Qt."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .models import (
    Artifact,
    Document,
    DocumentRevision,
    Segment,
    SegmentLineage,
    SessionRecord,
    SourceRecord,
    TrainingRun,
    UsageEvent,
    Voice,
    VoiceSample,
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
            "dubbing.generate_audio": self.generate_dubbing_audio,
            "source.clean": self.clean_source,
            "text.prepare": self.prepare_text,
            "audiobook.generate_audio": self.generate_audiobook_audio,
            "export.create": self.export,
            "voice.transcribe": self.transcribe_voice,
            "voice.normalize_recording": self.normalize_voice_recording,
            "rvc.model.upload": self.upload_rvc_model,
            "rvc.convert": self.convert_with_rvc,
            "training.xtts": self.train_xtts,
            "pdf.apply_edits": self.apply_pdf_edits,
            "session.bundle.export": self.export_session_bundle,
            "session.bundle.import": self.import_session_bundle,
            "workflow.continue": self.continue_workflow,
            "source.download_url": self.download_source_url,
            "source.reuse": self.reuse_source,
        }

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
        artifact = self.artifacts.register(output, kind="source", role="upload", session_id=session_id, metadata={"original_filename": output.name, "source_url": url})
        with self.database.session() as session:
            session.add(SourceRecord(session_id=session_id, kind=output.suffix.lower().lstrip(".") or "url", display_name=output.name, artifact_id=artifact.id, content_hash=artifact.content_hash, metadata_json={"url": url}))
        progress(1.0, "Source download ready")
        return {"artifact_id": artifact.id, "filename": output.name}

    def reuse_source(self, payload, progress, cancel_event):
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
        if record.workflow_kind != "audiobook":
            with self.database.session() as session:
                upload = session.scalar(select(Artifact).where(Artifact.session_id == session_id, Artifact.role == "upload", Artifact.state == "current").order_by(Artifact.created_at.desc()))
            filename = str((upload.metadata_json or {}).get("original_filename") or upload.relative_path).lower() if upload else ""
            if filename.endswith(".srt"):
                definitions = tuple(item for item in definitions if item.key != "transcribe")
        target_index = next((index for index, item in enumerate(definitions) if item.key == target_key), None)
        if target_index is None:
            raise ValueError(f"Unknown continuation stage: {target_key}")
        included = set(record.included_stages_json or [])
        stage_settings = payload.get("stage_settings") if isinstance(payload.get("stage_settings"), dict) else {}
        direct_settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
        runnable = [
            item for index, item in enumerate(definitions)
            if index <= target_index and item.executable and item.job_kind and (item.key in included or item.key == target_key)
        ]
        produced: list[dict[str, Any]] = []
        handlers = self.handlers()
        for index, definition in enumerate(runnable):
            if cancel_event.is_set():
                return {"artifacts": produced}
            settings = stage_settings.get(definition.key) if isinstance(stage_settings.get(definition.key), dict) else {}
            if definition.key == target_key:
                settings = {**settings, **direct_settings}
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
            if existing is not None and definition.key != target_key and existing.settings_hash == expected_settings_hash:
                continue
            source = self._latest_stage_input(session_id, definition.prerequisite_roles)
            if definition.prerequisite_roles and source is None:
                raise ValueError(f"Stage '{definition.key}' is missing a required input artifact.")
            handler = handlers[definition.job_kind]
            start = index / max(1, len(runnable))
            width = 1 / max(1, len(runnable))
            result = handler(
                {"session_id": session_id, "source_artifact_id": source.id if source else None, "settings": settings},
                lambda value, detail=None: progress(min(0.99, start + float(value) * width), detail),
                cancel_event,
            )
            if result:
                produced.append({"stage": definition.key, **result})
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
        }
        requested = str(settings.get("model_name") or "").strip()
        for key in aliases[stage]:
            requested = requested or str(settings.get(key) or "").strip()
        if requested == "default":
            requested = ""
        llm_settings, resolved_model = build_llm_settings(self.database, self.paths, requested_model=requested)
        hydrated = {
            **settings,
            "llm_provider_configs": llm_settings.provider_configs,
            "llm_default_model": llm_settings.default_model,
            "request_timeout_seconds": llm_settings.request_timeout_seconds,
        }
        hydrated[aliases[stage][0]] = requested or resolved_model
        return hydrated

    def transcribe(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.transcription import transcribe_source_file

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        session_dir = self._session_dir(session_id)
        progress(0.05, "Preparing transcription")
        if cancel_event.is_set():
            return {}
        output_path = Path(
            transcribe_source_file(
                session_dir,
                source_path,
                dict(payload.get("settings") or {}),
                ffmpeg_executable=str(payload.get("ffmpeg_executable") or "ffmpeg"),
                pixi_executable=str(payload.get("pixi_executable") or ""),
                pixi_manifest=str(payload.get("pixi_manifest") or ""),
                parakeet_pixi_executable=str(payload.get("parakeet_pixi_executable") or ""),
                parakeet_pixi_manifest=str(payload.get("parakeet_pixi_manifest") or ""),
            )
        )
        progress(0.9, "Registering transcription")
        artifact = self.artifacts.register(
            output_path,
            kind="srt",
            role="transcription",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=dict(payload.get("settings") or {}),
        )
        self._store_srt_document(
            session_id,
            artifact,
            "transcription",
            language=str((payload.get("settings") or {}).get("original_language") or "") or None,
        )
        progress(1.0, "Transcription ready")
        return {"artifact_id": artifact.id, "path": artifact.relative_path}

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
            result = translate_srt_file_deepl_with_result(session_dir, source_path, settings)
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

    def transcribe_voice(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.transcription import transcribe_source_file
        from pandrator.logic.dubbing.srt_utils import parse_srt

        sample_artifact, sample_path = self._resolve_input(str(payload.get("sample_artifact_id") or ""))
        operation_dir = self.paths.voices / str(payload.get("voice_id") or "transcription")
        operation_dir.mkdir(parents=True, exist_ok=True)
        progress(0.05, "Preparing reference transcription")
        output_path = Path(
            transcribe_source_file(
                operation_dir,
                sample_path,
                dict(payload.get("settings") or {}),
                ffmpeg_executable=str(payload.get("ffmpeg_executable") or "ffmpeg"),
                pixi_executable=str(payload.get("pixi_executable") or ""),
                pixi_manifest=str(payload.get("pixi_manifest") or ""),
                parakeet_pixi_executable=str(payload.get("parakeet_pixi_executable") or ""),
                parakeet_pixi_manifest=str(payload.get("parakeet_pixi_manifest") or ""),
            )
        )
        if cancel_event.is_set():
            return {}
        artifact = self.artifacts.register(
            output_path,
            kind="srt",
            role="voice_transcription",
            parent_ids=[sample_artifact.id],
            settings=dict(payload.get("settings") or {}),
        )
        transcript = " ".join(segment.text.replace("\n", " ").strip() for segment in parse_srt(output_path.read_text(encoding="utf-8-sig")))
        progress(1.0, "Reference transcription ready for review")
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "sample_id": payload.get("sample_id"), "transcript": transcript}

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
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        settings = dict(payload.get("settings") or {})
        progress(0.05, "Extracting source text")
        extension = source_path.suffix.lower()
        if extension == ".txt":
            cleaned_text = source_path.read_text(encoding="utf-8-sig")
        elif extension == ".epub":
            cleaned_text = file_handler.extract_text_from_epub(
                str(source_path),
                remove_footnotes=bool(settings.get("remove_footnotes", False)),
                filter_citations=bool(settings.get("filter_citations", True)),
            )
        elif extension == ".pdf":
            document = source_cleaning.build_source_document(
                str(source_path),
                artifact_dir=str(self._session_dir(session_id) / "source_ingestion"),
                progress_callback=lambda message: progress(0.35, str(message)),
            )
            cleaned_text = source_cleaning.apply_cleaning_operations(document, []).cleaned_text
        elif extension in {".docx", ".mobi"}:
            extracted = self._session_dir(session_id) / f"{source_path.stem}_extracted.txt"
            if not file_handler.convert_doc_to_text(str(source_path), str(extracted)):
                raise RuntimeError(f"Could not extract text from {source_path.name}.")
            cleaned_text = extracted.read_text(encoding="utf-8-sig")
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
            cleaning_result = source_cleaning.apply_cleaning_operations(document, pipeline.all_operations)
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
                pipeline.all_operations,
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
        destination = self._session_dir(session_id) / f"{source_path.stem}_cleaned.txt"
        destination.write_text(cleaned_text, encoding="utf-8", newline="\n")
        artifact = self.artifacts.register(
            destination,
            kind="text",
            role="clean_text",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={"extraction": extraction, "report": report},
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
        progress(0.1, "Segmenting narration")
        prepared = preprocess_text(
            text,
            {
                "source_file": str(source_path),
                "language": str(settings.get("language") or "en"),
                "tts_service": str(settings.get("service") or settings.get("tts_service") or "XTTS"),
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
        progress(1.0, "Narration segments ready")
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "segments": len(prepared)}

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

    def _generate_audio(self, session_id: str, source_artifact: Artifact, source_path: Path, settings: dict[str, Any], progress, cancel_event, *, role: str) -> dict[str, Any]:
        from pydub import AudioSegment
        from pandrator.logic import tts_handler

        records = json.loads(source_path.read_text(encoding="utf-8-sig"))
        if not isinstance(records, list) or not records:
            raise ValueError("No narration segments were found.")
        combined = AudioSegment.empty()
        wav_dir = self._session_dir(session_id) / "sentence_wavs"
        wav_dir.mkdir(parents=True, exist_ok=True)
        for index, record in enumerate(records, start=1):
            if cancel_event.is_set():
                return {}
            text = str(record.get("text") or record.get("original_sentence") or "").strip()
            if not text:
                continue
            progress((index - 1) / len(records), f"Generating segment {index} of {len(records)}")
            audio = tts_handler.text_to_audio(text, settings, max_attempts=int(settings.get("max_attempts") or 3), **self._tts_urls(settings))
            if audio is None:
                raise RuntimeError(f"Speech generation failed at segment {index}.")
            sentence_path = wav_dir / f"sentence_{index:06d}.wav"
            exported = audio.export(sentence_path, format="wav")
            exported.close()
            combined += audio
        if not combined:
            raise RuntimeError("The speech service returned no audio.")
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
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "segments": len(records)}

    def generate_dubbing_audio(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.speech_blocks import generate_speech_blocks_file

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(str(payload.get("source_artifact_id") or ""))
        settings = dict(payload.get("settings") or {})
        if source_path.suffix.lower() != ".srt":
            raise ValueError("Dubbing audio requires a transcription, correction, or translation SRT artifact.")
        blocks_path = Path(generate_speech_blocks_file(str(self._session_dir(session_id)), str(source_path), target_language=str(settings.get("target_language") or "en")))
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
        return self._generate_audio(session_id, source_artifact, source_path, settings, progress, cancel_event, role="audiobook_audio")

    def export(self, payload, progress, cancel_event):
        """Create immutable, managed exports without requiring generated audio."""
        from pandrator.logic.dubbing.bilingual_ass import write_bilingual_ass
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
        progress(0.1, "Preparing export")
        produced: list[Artifact] = []

        if record.workflow_kind == "audiobook":
            audio = by_role.get("audiobook_audio")
            if audio is None:
                raise ValueError("Audiobook export requires generated audio.")
            _audio_record, audio_path = self._resolve_input(audio.id)
            destination = output_dir / f"{record.name}.wav"
            shutil.copy2(audio_path, destination)
            produced.append(self.artifacts.register(destination, kind="export", role="export", session_id=session_id, parent_ids=[audio.id], settings=settings))
        else:
            upload_media = next((item for item in reversed(current) if item.role == "upload" and Path(item.relative_path).suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}), None)
            translated = by_role.get("translation")
            source_subtitle = by_role.get("correction") or by_role.get("transcription") or next((item for item in current if item.role == "upload" and Path(item.relative_path).suffix.lower() == ".srt"), None)
            subtitle_mode = str(settings.get("subtitle_mode") or "none").lower()
            subtitle_selection = str(settings.get("subtitle_selection") or ("dual" if translated and source_subtitle else "translation")).lower()
            selected_subtitles = ([source_subtitle] if source_subtitle and subtitle_selection in {"source", "dual"} else []) + ([translated] if translated and subtitle_selection in {"translation", "dual"} else [])
            selected_subtitles = [item for item in selected_subtitles if item]
            dubbing_audio = by_role.get("dubbing_audio")
            audio_mode = str(settings.get("audio_mode") or "source").lower()

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
                destination = output_dir / f"{record.name}.mp4"
                if subtitle_mode == "soft" and selected_subtitles:
                    tracks = []
                    for item in selected_subtitles:
                        _subtitle, subtitle_path = self._resolve_input(item.id)
                        tracks.append({"path": str(subtitle_path), "language": str((item.metadata_json or {}).get("language") or ("und" if item.role != "translation" else settings.get("target_language") or "und")), "title": "Translation" if item.role == "translation" else "Source", "default": item.role == "translation"})
                    command = build_multi_soft_subtitle_command(str(working_video), tracks, str(destination))
                    subprocess.run(command, check=True, capture_output=True, text=True)
                elif subtitle_mode == "burned" and selected_subtitles:
                    subtitle_paths = [self._resolve_input(item.id)[1] for item in selected_subtitles]
                    burn_path = subtitle_paths[-1]
                    if len(subtitle_paths) == 2:
                        burn_path = Path(write_bilingual_ass(str(subtitle_paths[0]), str(subtitle_paths[1]), str(output_dir / "bilingual_subtitles.ass")))
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
                    destination = output_dir / item_path.name
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
