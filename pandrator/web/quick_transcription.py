"""Sessionless transcription with private, bounded, expiring file storage."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import threading
import time
import wave
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pandrator.logic.cancellable_process import ProcessCancelled, run_cancellable
from pandrator.logic.dubbing.crispasr import (
    MODELS,
    normalize_compute_backend,
    normalize_model_quantization,
    normalize_model_quantization_alias,
    validate_model_quantization,
)
from pandrator.logic.dubbing.stt_backends import normalize_stt_backend
from pandrator.logic.dubbing.stt_languages import validate_stt_language
from pandrator.logic.dubbing.stt_provider_profiles import CLOUD_STT_ENGINE_IDS
from pandrator.logic.dubbing.transcript_normalization import load_transcript
from pandrator.logic.dubbing.transcription import transcribe_source_file_with_metadata
from pandrator.runtime import DataPaths

from .auth import Principal
from .credentials import SecretRedactor, hydrate_stt_settings
from .database import Database
from .idempotency import IdempotencyService
from .jobs import JobQueue
from .maintenance_threads import MaintenanceThread
from .media_process import resolve_ffmpeg_executable
from .models import AppSetting, Job, QuickTranscription, new_id, utcnow
from .quick_transcription_schemas import (
    CHUNK_SIZE,
    INLINE_BYTES,
    TranscriptionCreate,
)
from .stt_resources import stt_resource_keys

TERMINAL = {"succeeded", "failed", "canceled", "interrupted"}
MIME_TYPES = {
    "txt": "text/plain",
    "srt": "application/x-subrip",
    "json": "application/json",
}
_SELECTION_KEY = "_quick_transcription_selection"
_STRICT_QUANTIZATION_KEY = "_strict_stt_model_quantization"
_SAFE_PROVENANCE_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_SAFE_LANGUAGE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SUPPORTED_MODEL_QUANTIZATIONS = frozenset(
    choice for model in MODELS.values() for choice in model.filenames
)


def _empty_selection() -> dict[str, Any]:
    return {
        "requested": {
            "engine": None,
            "language": None,
            "model_quantization": None,
            "compute_backend": None,
        },
        "resolved": {
            "engine": None,
            "language": None,
            "model": None,
            "model_quantization": None,
            "compute_backend": None,
        },
        "runtime": {
            "engine": None,
            "model": None,
            "model_quantization": None,
            "compute_backend": None,
            "language": None,
        },
        "normalization": {},
    }


def _safe_provenance_token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_PROVENANCE_TOKEN.fullmatch(candidate) else None


def _safe_language_token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_LANGUAGE_TOKEN.fullmatch(candidate) else None


def _quantization_normalization(raw_value: Any, canonical: str) -> dict[str, Any]:
    if raw_value == canonical:
        return {}
    original = _safe_provenance_token(raw_value)
    return {
        "model_quantization": {"from": original, "to": canonical},
    }


def _resolved_compute_backend(settings: dict[str, Any], engine: str) -> str:
    if engine in CLOUD_STT_ENGINE_IDS:
        return "remote"
    if engine == "qwen3":
        from pandrator.logic.dubbing.qwen_asr import _normalize_backend

        return _normalize_backend(settings)
    return normalize_compute_backend(settings.get("stt_compute_backend"))


def _validate_requested_quantization(value: str | None, engine: str) -> None:
    if value is None:
        return
    if engine in MODELS:
        try:
            validate_model_quantization(value, engine)
        except ValueError as error:
            raise TranscriptionError("validation_error", str(error), 400) from error
    elif engine == "auto":
        normalized = normalize_model_quantization_alias(value)
        if normalized not in _SUPPORTED_MODEL_QUANTIZATIONS:
            choices = ", ".join(sorted(_SUPPORTED_MODEL_QUANTIZATIONS))
            raise TranscriptionError(
                "validation_error",
                f"Unsupported model quantization {value!r} for auto; "
                f"valid choices are: {choices}.",
                400,
            )


def _selection_for_request(
    payload: TranscriptionCreate,
    settings: dict[str, Any],
    engine: str,
    *,
    legacy_quantization: Any,
    explicit_quantization: bool,
) -> dict[str, Any]:
    selection = _empty_selection()
    selection["requested"] = {
        "engine": payload.engine,
        "language": payload.language,
        "model_quantization": payload.model_quantization,
        "compute_backend": payload.compute_backend,
    }
    resolved = selection["resolved"]
    resolved["engine"] = engine
    resolved["language"] = _safe_language_token(
        settings.get("stt_language") or settings.get("whisper_language")
    ) or "auto"
    if engine in MODELS:
        quantization = (
            MODELS[engine].default_quantization
            if engine == "qwen3"
            else normalize_model_quantization(
                settings.get("stt_model_quantization"), engine
            )
        )
        if engine == "qwen3":
            resolved["model"] = _safe_provenance_token(
                settings.get("qwen_asr_model") or "qwen3_asr_0_6b"
            )
        else:
            resolved["model"] = MODELS[engine].filename_for(quantization)
        resolved["model_quantization"] = quantization
        if (
            not explicit_quantization
            and engine != "qwen3"
            and legacy_quantization is not None
        ):
            selection["normalization"] = _quantization_normalization(
                legacy_quantization, quantization
            )
    resolved["compute_backend"] = _resolved_compute_backend(settings, engine)
    return selection


def _selection_after_transcription(
    initial: dict[str, Any], settings: dict[str, Any], output: Any, native_payload: Any
) -> dict[str, Any]:
    selection = copy.deepcopy(initial) if isinstance(initial, dict) else _empty_selection()
    route = getattr(output, "routing", None)
    route = route if isinstance(route, dict) else {}
    engine_value = route.get("engine") or getattr(output, "engine", None)
    engine = _safe_provenance_token(engine_value)
    if engine:
        selection["resolved"]["engine"] = engine
    language_value = (
        route.get("resolved_language")
        or getattr(output, "resolved_language", None)
        or settings.get("stt_language")
        or settings.get("whisper_language")
    )
    language = _safe_language_token(language_value) or "auto"
    selection["resolved"]["language"] = language

    native_metadata = (
        native_payload.get("metadata") if isinstance(native_payload, dict) else None
    )
    native_metadata = native_metadata if isinstance(native_metadata, dict) else {}
    if engine in CLOUD_STT_ENGINE_IDS:
        model = _safe_provenance_token(native_metadata.get("model"))
    else:
        model = _safe_provenance_token(route.get("model"))
    selected_quantization = None
    if engine in MODELS:
        if engine == "qwen3":
            selected_quantization = MODELS[engine].default_quantization
        elif settings.get(_STRICT_QUANTIZATION_KEY):
            selected_quantization = validate_model_quantization(
                settings.get("stt_model_quantization"), engine
            )
        else:
            selected_quantization = normalize_model_quantization(
                settings.get("stt_model_quantization"), engine
            )
        if not model and engine == "qwen3":
            model = _safe_provenance_token(
                settings.get("qwen_asr_model") or "qwen3_asr_0_6b"
            )
        elif not model:
            model = MODELS[engine].filename_for(selected_quantization)
        selection["resolved"]["model_quantization"] = selected_quantization
        legacy_quantization = settings.get("stt_model_quantization")
        if (
            engine != "qwen3"
            and selection["requested"].get("model_quantization") is None
            and legacy_quantization is not None
        ):
            selection["normalization"] = _quantization_normalization(
                legacy_quantization, selected_quantization
            )
    selection["resolved"]["model"] = model

    if engine:
        selection["resolved"]["compute_backend"] = _resolved_compute_backend(
            settings, engine
        )

    runtime = selection["runtime"]
    header = native_payload.get("crispasr") if isinstance(native_payload, dict) else None
    header = header if isinstance(header, dict) else {}
    runtime["engine"] = _safe_provenance_token(
        getattr(output, "engine", None) or header.get("backend")
    )
    runtime["model"] = _safe_provenance_token(
        getattr(output, "model", None) or header.get("model")
    )
    runtime_quantization = getattr(output, "model_quantization", None) or header.get(
        "model_quantization"
    )
    runtime["model_quantization"] = (
        normalize_model_quantization_alias(runtime_quantization)
        if isinstance(runtime_quantization, str)
        and normalize_model_quantization_alias(runtime_quantization)
        in {choice for model_config in MODELS.values() for choice in model_config.filenames}
        else None
    )
    backend = _safe_provenance_token(getattr(output, "compute_backend", None))
    runtime["compute_backend"] = (
        backend if backend in {*("auto", "cpu", "cuda", "vulkan", "metal", "remote")} else None
    )
    native_language = None
    if isinstance(native_payload, dict):
        native_language = (
            native_payload.get("language")
            or header.get("language_detected")
            or header.get("language")
        )
    runtime["language"] = _safe_language_token(
        getattr(output, "resolved_language", None) or native_language
    )
    return selection


class TranscriptionError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _native_transcript_text(payload: Any) -> str:
    """Read recognizer text even when its segments have no native timing."""

    if not isinstance(payload, dict):
        return ""
    for key in ("segments", "transcription"):
        entries = payload.get(key)
        if not isinstance(entries, list):
            continue
        texts = [
            str(entry.get("text") or "").replace("\n", " ").strip()
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("text") or "").strip()
        ]
        if texts:
            return "\n".join(texts)
    text = payload.get("text")
    return str(text).strip() if isinstance(text, str) else ""


class QuickTranscriptionService:
    def __init__(self, database: Database, paths: DataPaths, jobs: JobQueue):
        self.database = database
        self.paths = paths
        self.jobs = jobs
        self.root = paths.root / "quick-transcriptions"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.idempotency = IdempotencyService(database, SecretRedactor(database, paths))
        self._stop = threading.Event()
        self._thread: MaintenanceThread | None = None

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
            # Snapshot ordinary non-secret configured defaults, then resolve credentials only in the worker.
            from .settings_policy import BUILTIN_DEFAULTS

            settings = copy.deepcopy(BUILTIN_DEFAULTS["stt"])
            defaults = session.get(AppSetting, "defaults.stt")
            if defaults and isinstance(defaults.value_json, dict):
                settings.update(copy.deepcopy(defaults.value_json))
            legacy_quantization = settings.get("stt_model_quantization")
            explicit_quantization = payload.model_quantization is not None
            for field, setting in {
                "language": "stt_language",
                "engine": "stt_engine",
                "model_quantization": "stt_model_quantization",
                "compute_backend": "stt_compute_backend",
                "qwen_asr_model": "qwen_asr_model",
                "transcription_vocal_isolation": "transcription_vocal_isolation",
            }.items():
                value = getattr(payload, field)
                if value is not None:
                    settings[setting] = value
            canonical_engine = normalize_stt_backend(
                settings.get("stt_engine") or settings.get("stt_backend")
            )
            _validate_requested_quantization(
                payload.model_quantization, canonical_engine
            )
            if explicit_quantization and canonical_engine in {*MODELS, "auto"}:
                settings[_STRICT_QUANTIZATION_KEY] = True
            if canonical_engine == "qwen3":
                if payload.compute_backend is not None:
                    settings["qwen_asr_backend"] = payload.compute_backend
                from pandrator.logic.dubbing.qwen_asr import (
                    QwenASRError,
                    normalize_qwen_asr_language,
                    validate_transcription_settings,
                )

                try:
                    requested_language = normalize_qwen_asr_language(
                        settings.get("stt_language")
                        or settings.get("whisper_language")
                    )
                    validated = validate_transcription_settings(
                        settings,
                        require_word_timestamps=(
                            payload.format != "txt" and requested_language != "auto"
                        ),
                    )
                except QwenASRError as error:
                    raise TranscriptionError(
                        "unsupported_language", str(error), 400
                    ) from error
                settings["stt_language"] = validated["language"]
            elif canonical_engine not in CLOUD_STT_ENGINE_IDS:
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
            selection = _selection_for_request(
                payload,
                settings,
                canonical_engine,
                legacy_quantization=legacy_quantization,
                explicit_quantization=explicit_quantization,
            )
            settings = self.idempotency.redactor.redact_value(settings)
            settings[_SELECTION_KEY] = selection

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
        available_formats = self._available_formats(record.format)
        if selected not in available_formats:
            raise TranscriptionError(
                "timing_not_requested",
                "Word timings were not requested, so an SRT result is unavailable.",
                409,
            )
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
            "available_formats": available_formats,
            "chunk_size": CHUNK_SIZE,
            "next_chunk_index": record.next_chunk_index,
            "uploaded_bytes": record.uploaded_bytes,
            "size_bytes": record.size_bytes,
            "selection": copy.deepcopy(
                record.settings_json.get(_SELECTION_KEY)
                if isinstance(record.settings_json, dict)
                and isinstance(record.settings_json.get(_SELECTION_KEY), dict)
                else _empty_selection()
            ),
            "result_available": status == "succeeded"
            and (self._directory(record.id) / f"result.{selected}").is_file(),
            "inline_result": False,
            "result_url": f"/api/v1/transcriptions/{record.id}/result?format={selected}",
        }
        if result["result_available"]:
            path = self._directory(record.id) / f"result.{selected}"
            if include_result and path.stat().st_size <= INLINE_BYTES:
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
            if format not in self._available_formats(record.format):
                raise TranscriptionError(
                    "timing_not_requested",
                    "Word timings were not requested, so an SRT result is unavailable.",
                    409,
                )
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

    @staticmethod
    def _available_formats(transcript_format: str) -> list[str]:
        # ``format`` is frozen with the uploaded request and therefore also
        # freezes whether precise word timing and SRT publication were asked for.
        return ["txt", "json"] if transcript_format == "txt" else ["txt", "srt", "json"]

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

        self._thread = MaintenanceThread(
            target=maintain, name="quick-transcription-cleanup", daemon=True
        )
        self._thread.start()

    def stop_maintenance(self, *, timeout: float | None = 2) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        return thread.finish(timeout=timeout)

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
            initial_selection = settings.pop(_SELECTION_KEY, None)
            source_suffix = record.source_suffix
            transcript_format = record.format
            require_word_timestamps = transcript_format != "txt"
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
                require_word_timestamps=require_word_timestamps,
                cancel_event=cancel_event,
                progress_callback=report,
            )
            native_payload = json.loads(
                Path(output.word_timestamps_path).read_text(encoding="utf-8-sig")
            )
            transcript = load_transcript(output.word_timestamps_path)
            canonical = transcript.to_dict()
            canonical["engine"] = output.engine
            canonical["compute_backend"] = output.compute_backend
            transcript_text = "\n".join(
                segment.text.replace("\n", " ").strip()
                for segment in transcript.segments
            )
            if not require_word_timestamps:
                transcript_text = _native_transcript_text(native_payload) or transcript_text
            canonical["text"] = transcript_text
            source_metadata = (
                native_payload.get("metadata")
                if isinstance(native_payload, dict)
                else None
            )
            if isinstance(source_metadata, dict):
                canonical.setdefault("metadata", {}).update(source_metadata)
            selection = _selection_after_transcription(
                initial_selection, settings, output, native_payload
            )
            canonical["selection"] = selection
            (scratch / "result.json").write_text(
                json.dumps(canonical, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (scratch / "result.txt").write_text(canonical["text"], encoding="utf-8")
            available_formats = self._available_formats(transcript_format)
            if require_word_timestamps:
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
                for format in available_formats:
                    (scratch / f"result.{format}").replace(
                        directory / f"result.{format}"
                    )
                saved_settings = copy.deepcopy(record.settings_json or {})
                saved_settings[_SELECTION_KEY] = selection
                record.settings_json = saved_settings
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
