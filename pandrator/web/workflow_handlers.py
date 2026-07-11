"""Worker adapters that run existing Pandrator engines without Qt."""

from __future__ import annotations

import hashlib
import json
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
            "voice.transcribe": self.transcribe_voice,
            "pdf.apply_edits": self.apply_pdf_edits,
        }

    def _session_dir(self, session_id: str) -> Path:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise ValueError(f"Session not found: {session_id}")
            path = self.paths.sessions / record.storage_key
        path.mkdir(parents=True, exist_ok=True)
        return path

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
                    speaker=item.speaker,
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
        from pandrator.logic.dubbing.srt_utils import parse_srt

        transient_session_id = str(payload.get("session_id") or "")
        result = self.transcribe(
            {
                **payload,
                "session_id": transient_session_id,
                "source_artifact_id": payload.get("sample_artifact_id"),
            },
            progress,
            cancel_event,
        )
        if not result or cancel_event.is_set():
            return result
        artifact, path = self.artifacts.resolve(result["artifact_id"])
        transcript = " ".join(segment.text.replace("\n", " ").strip() for segment in parse_srt(path.read_text(encoding="utf-8-sig")))
        return {**result, "transcript": transcript}

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
