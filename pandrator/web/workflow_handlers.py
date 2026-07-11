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
    UsageEvent,
    Voice,
    VoiceSample,
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
            "pdf.apply_edits": self.apply_pdf_edits,
            "session.bundle.export": self.export_session_bundle,
            "session.bundle.import": self.import_session_bundle,
        }

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
        if not cost and not response_count:
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
                    cost_usd=cost,
                    cost_source=",".join(sources) or None,
                    raw_usage_json={"response_count": response_count},
                )
            )

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
        settings = dict(payload.get("settings") or {})
        progress(0.05, "Correcting subtitles")
        result = correct_srt_file_with_result(
            session_dir,
            source_path,
            settings,
            correction_instructions=str(payload.get("instructions") or ""),
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
            result = translate_srt_file_with_result(
                session_dir,
                source_path,
                settings,
                translation_instructions=str(payload.get("instructions") or ""),
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

    def clean_source(self, payload, progress, cancel_event):
        """Run deterministic extraction; agentic cleanup remains an explicit option."""
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
        if bool(settings.get("agentic", False)):
            raise ValueError(
                "Agentic source cleaning needs a configured provider and is not implied by deterministic extraction. "
                "Disable agentic cleaning or configure it in the stage settings."
            )
        destination = self._session_dir(session_id) / f"{source_path.stem}_cleaned.txt"
        destination.write_text(cleaned_text, encoding="utf-8", newline="\n")
        artifact = self.artifacts.register(
            destination,
            kind="text",
            role="clean_text",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={"extraction": "deterministic"},
        )
        progress(1.0, "Source text ready")
        return {"artifact_id": artifact.id, "path": artifact.relative_path, "characters": len(cleaned_text)}

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
