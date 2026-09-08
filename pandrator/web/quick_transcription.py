"""Sessionless transcription with private, bounded, expiring file storage."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import threading
import time
import wave
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pandrator.logic.cancellable_process import ProcessCancelled, run_cancellable
from pandrator.logic.dubbing.transcript_normalization import load_transcript
from pandrator.logic.dubbing.stt_backends import normalize_stt_backend
from pandrator.logic.dubbing.stt_languages import validate_stt_language
from pandrator.logic.dubbing.stt_provider_profiles import CLOUD_STT_ENGINE_IDS
from pandrator.logic.dubbing.transcription import transcribe_source_file_with_metadata
from pandrator.runtime import DataPaths

from .auth import Principal
from .credentials import SecretRedactor, hydrate_stt_settings
from .database import Database
from .idempotency import IdempotencyService
from .jobs import JobQueue
from .media_process import resolve_ffmpeg_executable
from .models import AppSetting, Job, QuickTranscription, new_id, utcnow
from .quick_transcription_schemas import CHUNK_SIZE, INLINE_BYTES, TranscriptionCreate
from .stt_resources import stt_resource_keys

TERMINAL = {"succeeded", "failed", "canceled", "interrupted"}
MIME_TYPES = {
    "txt": "text/plain",
    "srt": "application/x-subrip",
    "json": "application/json",
}


class TranscriptionError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class QuickTranscriptionService:
    def __init__(self, database: Database, paths: DataPaths, jobs: JobQueue):
        self.database = database
        self.paths = paths
        self.jobs = jobs
        self.root = paths.root / "quick-transcriptions"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.idempotency = IdempotencyService(database, SecretRedactor(database, paths))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _directory(self, identifier: str) -> Path:
        # Identifiers come from a database record, never a caller's path.
        return self.root / identifier

    def hidden_job_ids(self, subject: str, identifiers) -> set[str]:
        """Filter only the current aggregate page, without exposing private IDs."""
        candidates = [identifier for identifier in identifiers if identifier]
        if not candidates:
            return set()
        with self.database.session() as session:
            return {
                identifier
                for identifier in session.scalars(
                    select(QuickTranscription.job_id).where(
                        QuickTranscription.job_id.in_(candidates),
                        QuickTranscription.owner_subject != subject,
                    )
                ).all()
                if identifier is not None
            }

    def _owned(self, session, identifier: str, subject: str, *, allow_gone=False):
        record = session.get(QuickTranscription, identifier)
        if record is None or record.owner_subject != subject:
            raise TranscriptionError("not_found", "Transcription not found.", 404)
        if not allow_gone and (
            record.state in {"deleted", "expired", "deleting"}
            or _aware(record.expires_at) <= utcnow()
        ):
            raise TranscriptionError(
                "transcription_expired",
                "This temporary transcription has expired or was deleted.",
                410,
            )
        return record

    def create(
        self, principal: Principal, payload: TranscriptionCreate, key: str
    ) -> dict[str, Any]:
        self.cleanup()
        with self.database.immediate_session() as session:
            reservation = self.idempotency.begin(
                session,
                principal=principal,
                operation_id="createQuickTranscription",
                idempotency_key=key,
                payload=payload.model_dump(mode="json"),
            )
            if reservation.response is not None:
                identifier = reservation.response[0]["id"]
                record = self._owned(session, identifier, principal.subject)
                return self._snapshot(session, record, include_result=False)
            active = session.scalars(
                select(QuickTranscription).where(
                    QuickTranscription.owner_subject == principal.subject,
                    QuickTranscription.state.not_in(["deleted", "expired"]),
                )
            ).all()
            if len(active) >= 20:
                raise TranscriptionError(
                    "transcription_limit",
                    "Delete an earlier temporary transcription before starting another.",
                    429,
                )
            # Snapshot ordinary non-secret configured defaults, then resolve credentials only in the worker.
            from .workspace import BUILTIN_DEFAULTS

            settings = copy.deepcopy(BUILTIN_DEFAULTS["stt"])
            defaults = session.get(AppSetting, "defaults.stt")
            if defaults and isinstance(defaults.value_json, dict):
                settings.update(copy.deepcopy(defaults.value_json))
            for field, setting in {
                "language": "stt_language",
                "engine": "stt_engine",
                "model_quantization": "stt_model_quantization",
                "compute_backend": "stt_compute_backend",
            }.items():
                value = getattr(payload, field)
                if value is not None:
                    settings[setting] = value
            canonical_engine = normalize_stt_backend(
                settings.get("stt_engine") or settings.get("stt_backend")
            )
            if canonical_engine not in CLOUD_STT_ENGINE_IDS:
                try:
                    settings["stt_language"] = validate_stt_language(
                        canonical_engine,
                        settings.get("stt_language")
                        or settings.get("whisper_language"),
                    )
                except ValueError as error:
                    raise TranscriptionError(
                        "unsupported_language", str(error), 400
                    ) from error
            settings = self.idempotency.redactor.redact_value(settings)
            record = QuickTranscription(
                id=new_id(),
                owner_subject=principal.subject,
                source_suffix=Path(payload.filename).suffix.lower(),
                size_bytes=payload.size_bytes,
                sha256=payload.sha256.lower(),
                format=payload.format,
                settings_json=settings,
                expires_at=utcnow() + timedelta(hours=1),
            )
            session.add(record)
            session.flush()
            result = self._snapshot(session, record, include_result=False)
            # Store only the opaque identity in idempotency history, never result text or input filenames.
            self.idempotency.complete(
                session,
                reservation,
                response={"id": record.id},
                status_code=201,
                resource_kind="quick_transcription",
                resource_id=record.id,
            )
            return result

    def upload_chunk(self, identifier: str, subject: str, index: int, data: bytes):
        if index < 0 or not data or len(data) > CHUNK_SIZE:
            raise TranscriptionError(
                "invalid_chunk", "Chunks must contain 1–8 MiB of audio data."
            )
        with self.database.immediate_session() as session:
            record = self._owned(session, identifier, subject)
            if record.state != "uploading":
                raise TranscriptionError(
                    "upload_closed",
                    "This transcription has already been submitted.",
                    409,
                )
            directory = self._directory(record.id)
            directory.mkdir(mode=0o700, exist_ok=True)
            path = directory / f"chunk-{index}"
            if index < record.next_chunk_index:
                if (
                    not path.is_file()
                    or hashlib.sha256(path.read_bytes()).digest()
                    != hashlib.sha256(data).digest()
                ):
                    raise TranscriptionError(
                        "chunk_conflict",
                        "This chunk index already contains different bytes.",
                        409,
                    )
                return self._snapshot(session, record, include_result=False)
            if index != record.next_chunk_index:
                raise TranscriptionError(
                    "chunk_out_of_order", "Upload the next expected chunk.", 409
                )
            expected = min(CHUNK_SIZE, record.size_bytes - record.uploaded_bytes)
            if len(data) != expected:
                raise TranscriptionError(
                    "invalid_chunk_size",
                    "Chunk size does not match the declared upload.",
                )
            temporary = path.with_suffix(".partial")
            temporary.write_bytes(data)
            temporary.replace(path)
            record.uploaded_bytes += len(data)
            record.next_chunk_index += 1
            return self._snapshot(session, record, include_result=False)

    def start(self, identifier: str, subject: str):
        with self.database.immediate_session() as session:
            record = self._owned(session, identifier, subject)
            if record.job_id:
                return self._snapshot(session, record, include_result=False)
            if (
                record.state != "uploading"
                or record.uploaded_bytes != record.size_bytes
            ):
                raise TranscriptionError(
                    "upload_incomplete",
                    "Upload the complete source before transcription.",
                    409,
                )
            directory = self._directory(record.id)
            source = directory / f"source{record.source_suffix}"
            digest = hashlib.sha256()
            with source.open("wb") as output:
                for index in range(record.next_chunk_index):
                    with (directory / f"chunk-{index}").open("rb") as chunk:
                        while data := chunk.read(1024 * 1024):
                            digest.update(data)
                            output.write(data)
            if digest.hexdigest() != record.sha256:
                source.unlink(missing_ok=True)
                raise TranscriptionError(
                    "source_hash_mismatch",
                    "The uploaded audio does not match its SHA-256 checksum.",
                    409,
                )
            job = self.jobs.enqueue_in_session(
                session,
                "transcription.quick",
                {"transcription_id": record.id},
                resource_keys=stt_resource_keys(record.settings_json),
                max_attempts=1,
            )
            record.job_id = job.id
            record.state = "submitted"
            record.expires_at = utcnow() + timedelta(hours=24)
            # Keep chunks until commit, so an interrupted submit can safely retry.
            return self._snapshot(session, record, include_result=False)

    def _snapshot(self, session, record, *, format=None, include_result=True):
        job = session.get(Job, record.job_id) if record.job_id else None
        selected = format or record.format
        if selected not in MIME_TYPES:
            raise TranscriptionError("invalid_format", "Choose txt, srt, or json.")
        status = job.status if job else record.state
        if record.state in {"deleted", "expired", "deleting"}:
            status = record.state
        result = {
            "id": record.id,
            "job_id": record.job_id,
            "status": status,
            "progress": job.progress if job else 0,
            "progress_detail": job.progress_detail if job else None,
            "expires_at": _aware(record.expires_at).isoformat(),
            "format": selected,
            "chunk_size": CHUNK_SIZE,
            "next_chunk_index": record.next_chunk_index,
            "uploaded_bytes": record.uploaded_bytes,
            "size_bytes": record.size_bytes,
            "result_available": status == "succeeded",
            "inline_result": False,
            "result_url": f"/api/v1/transcriptions/{record.id}/result?format={selected}",
        }
        if status == "succeeded":
            path = self._directory(record.id) / f"result.{selected}"
            if (
                path.is_file()
                and include_result
                and path.stat().st_size <= INLINE_BYTES
            ):
                content = path.read_text(encoding="utf-8")
                result["result"] = {
                    "format": selected,
                    "mime_type": MIME_TYPES[selected],
                    "content": json.loads(content) if selected == "json" else content,
                    "size_bytes": path.stat().st_size,
                }
                result["inline_result"] = True
        if status in {"failed", "interrupted"}:
            result["error"] = {
                "code": "transcription_failed",
                "message": "Transcription failed. Check that the media is readable and the selected STT service is available.",
            }
        return result

    def get(
        self, identifier: str, subject: str, *, format=None, wait_seconds: float = 0
    ):
        deadline = time.monotonic() + min(30, max(0, wait_seconds))
        while True:
            with self.database.session() as session:
                record = self._owned(session, identifier, subject)
                result = self._snapshot(session, record, format=format)
            if (
                result["status"] in TERMINAL | {"uploading"}
                or time.monotonic() >= deadline
            ):
                return result
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))

    def result(self, identifier: str, subject: str, format: str):
        if format not in MIME_TYPES:
            raise TranscriptionError("invalid_format", "Choose txt, srt, or json.")
        # Read under the same short lock used by deletion to avoid a read/delete race.
        with self.database.immediate_session() as session:
            record = self._owned(session, identifier, subject)
            job = session.get(Job, record.job_id) if record.job_id else None
            if not job or job.status != "succeeded":
                raise TranscriptionError(
                    "result_not_ready", "The transcript is not ready yet.", 409
                )
            path = self._directory(record.id) / f"result.{format}"
            if not path.is_file():
                raise TranscriptionError(
                    "result_unavailable",
                    "The temporary transcript is no longer available.",
                    410,
                )
            return path.read_text(encoding="utf-8")

    def cancel(self, identifier: str, subject: str, *, delete=False):
        with self.database.immediate_session() as session:
            record = self._owned(session, identifier, subject, allow_gone=True)
            job = session.get(Job, record.job_id) if record.job_id else None
            if job and job.status not in TERMINAL:
                job = self.jobs.request_cancel_in_session(session, job.id)
            if delete:
                record.state = "deleting"
            elif not job:
                record.state = "canceled"
                record.expires_at = utcnow() + timedelta(hours=1)
            if not job or job.status in TERMINAL:
                self._remove_inputs(record.id)
                if delete:
                    self._erase(session, record, "deleted")
            return self._snapshot(session, record, include_result=False)

    def _remove_inputs(self, identifier: str):
        directory = self._directory(identifier)
        if directory.is_dir():
            for child in directory.iterdir():
                if not child.name.startswith("result."):
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink(missing_ok=True)

    def _erase(self, session, record, state):
        directory = self._directory(record.id)
        if directory.exists():
            shutil.rmtree(directory)
        record.state = state
        record.settings_json = {}
        record.sha256 = ""
        record.source_suffix = ""
        record.expires_at = utcnow()
        if record.job_id:
            job = session.get(Job, record.job_id)
            if job:
                job.result_json = None
                job.error_message = None

    def cleanup(self):
        with self.database.immediate_session() as session:
            records = session.scalars(
                select(QuickTranscription).where(
                    QuickTranscription.state.not_in(["deleted", "expired"])
                )
            ).all()
            for record in records:
                job = session.get(Job, record.job_id) if record.job_id else None
                expired = _aware(record.expires_at) <= utcnow()
                if record.state == "deleting" or expired:
                    if job and job.status not in TERMINAL:
                        self.jobs.request_cancel_in_session(session, job.id)
                        record.state = "deleting"
                    else:
                        self._erase(
                            session, record, "expired" if expired else "deleted"
                        )
                elif job and job.status in TERMINAL:
                    self._remove_inputs(record.id)
                    if record.state == "submitted":
                        record.state = "finished"
                        record.expires_at = utcnow() + timedelta(hours=1)
            # Crash-before-commit scratch directories have no record; active records are never swept.
            known = set(session.scalars(select(QuickTranscription.id)).all())
            for directory in self.root.iterdir():
                if directory.is_dir() and directory.name not in known:
                    shutil.rmtree(directory)

    def start_maintenance(self):
        if self._thread is not None:
            return

        def maintain():
            while not self._stop.is_set():
                try:
                    self.cleanup()
                except Exception:
                    # Retry on the next tick; never put audio or transcript data in maintenance logs.
                    pass
                self._stop.wait(30)

        self._thread = threading.Thread(
            target=maintain, name="quick-transcription-cleanup", daemon=True
        )
        self._thread.start()

    def stop_maintenance(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def run(self, payload, progress, cancel_event):
        identifier = str(payload["transcription_id"])
        with self.database.session() as session:
            record = session.get(QuickTranscription, identifier)
            if (
                not record
                or record.job_id != payload.get("_job_id")
                or record.state != "submitted"
            ):
                raise ProcessCancelled("This transcription is no longer active.")
            settings = copy.deepcopy(record.settings_json)
            source_suffix = record.source_suffix
        directory = self._directory(identifier)
        # Each worker lease writes separately; a stale lease cannot overwrite a replacement's output.
        scratch = directory / f"attempt-{payload.get('_lease_generation', 0)}"
        scratch.mkdir(mode=0o700, exist_ok=True)
        try:
            progress(0.05, "Preparing audio")
            normalized = scratch / "audio.wav"
            run_cancellable(
                [
                    resolve_ffmpeg_executable(),
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    "mov,matroska,webm,wav,mp3,ogg,flac,aac,aiff,asf,avi,mpeg,mpegts",
                    "-i",
                    str(directory / f"source{source_suffix}"),
                    "-t",
                    "7201",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(normalized),
                ],
                cancel_event=cancel_event,
                check=True,
                capture_output=True,
            )
            with wave.open(str(normalized), "rb") as audio:
                if audio.getnframes() / audio.getframerate() > 7200:
                    raise TranscriptionError(
                        "media_too_long",
                        "Quick transcription supports media up to two hours.",
                    )
            settings = hydrate_stt_settings(self.database, self.paths, settings)

            def report(value, _detail=None):
                progress(0.15 + 0.8 * value, "Transcribing audio")

            output = transcribe_source_file_with_metadata(
                scratch,
                normalized,
                settings,
                source_is_normalized=True,
                cancel_event=cancel_event,
                progress_callback=report,
            )
            transcript = load_transcript(output.word_timestamps_path)
            canonical = transcript.to_dict()
            canonical["engine"] = output.engine
            canonical["compute_backend"] = output.compute_backend
            canonical["text"] = "\n".join(
                segment.text.replace("\n", " ").strip()
                for segment in transcript.segments
            )
            (scratch / "result.json").write_text(
                json.dumps(canonical, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (scratch / "result.txt").write_text(canonical["text"], encoding="utf-8")
            shutil.copyfile(output.srt_path, scratch / "result.srt")
            with self.database.immediate_session() as session:
                record = session.get(QuickTranscription, identifier)
                job = session.get(Job, payload["_job_id"])
                if (
                    cancel_event.is_set()
                    or not record
                    or record.state != "submitted"
                    or not job
                    or job.status != "running"
                    or job.lease_generation != payload.get("_lease_generation")
                ):
                    raise ProcessCancelled("Transcription was canceled.")
                for format in MIME_TYPES:
                    (scratch / f"result.{format}").replace(
                        directory / f"result.{format}"
                    )
                record.expires_at = utcnow() + timedelta(hours=1)
                record.state = "finished"
            progress(1, "Transcript ready")
            return {}  # Transcript content is deliberately absent from generic job/event history.
        except ProcessCancelled:
            raise
        except Exception:
            raise RuntimeError(
                "Transcription failed. Check the media and configured STT service."
            ) from None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            # Do not delete a source still in use by a newer worker lease.
            with self.database.immediate_session() as session:
                job = session.get(Job, payload.get("_job_id"))
                current = job and job.lease_generation == payload.get(
                    "_lease_generation"
                )
                if current:
                    self._remove_inputs(identifier)
