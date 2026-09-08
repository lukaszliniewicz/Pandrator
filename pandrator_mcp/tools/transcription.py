"""MCP adapter for resumable, bounded transcription uploads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import stat
from io import BytesIO
from typing import Any, BinaryIO

from ..context import McpRuntime
from ..errors import PandratorMcpError
from ..results import ToolOutcome
from ..schemas.transcription import (
    Base64TranscriptionSource,
    CancelTranscriptionInput,
    DeleteTranscriptionInput,
    GetTranscriptionInput,
    GetTranscriptionResultInput,
    LocalFileTranscriptionSource,
    TranscribeInput,
    validate_transcription_filename,
)
from .e2e import _named_source_root, _open_contained_file, _relative_parts

MAX_LOCAL_BYTES = 256 * 1024 * 1024
MAX_CHUNK_BYTES = 8 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024


def _invalid(message: str, **details: Any) -> PandratorMcpError:
    return PandratorMcpError("validation_error", message, details=details)


def _integer(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool):
        raise _invalid(f"Pandrator returned an invalid {key}.")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise _invalid(f"Pandrator returned an invalid {key}.") from error


def _validated_filename(value: str) -> str:
    try:
        return validate_transcription_filename(value)
    except ValueError as error:
        raise _invalid(str(error)) from error


def _upload_and_start(
    application: Any,
    *,
    initial: dict[str, Any],
    handle: BinaryIO,
    size_bytes: int,
    arguments: TranscribeInput,
) -> dict[str, Any]:
    transcription_id = str(initial.get("id") or "").strip()
    if not transcription_id:
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned a transcription without an ID.",
        )
    status = str(initial.get("status") or "uploading").strip().lower()
    if status != "uploading":
        # Existing idempotent requests may already be queued or terminal. The
        # status endpoint is authoritative for the returned snapshot.
        return application.get_transcription(
            transcription_id,
            format=arguments.format,
            wait_seconds=arguments.wait_seconds,
        )

    chunk_size = _integer(initial, "chunk_size", MAX_CHUNK_BYTES)
    if not 1 <= chunk_size <= MAX_CHUNK_BYTES:
        raise _invalid("Pandrator returned an unsafe transcription chunk size.")
    chunk_count = (size_bytes + chunk_size - 1) // chunk_size
    next_index = _integer(initial, "next_chunk_index", 0)
    if not 0 <= next_index <= chunk_count:
        raise _invalid("Pandrator returned an invalid transcription chunk index.")

    while next_index < chunk_count:
        handle.seek(next_index * chunk_size)
        body = handle.read(chunk_size)
        if not body:
            raise PandratorMcpError(
                "source_changed",
                "The transcription source could not be read at the expected chunk.",
            )
        response = application.upload_transcription_chunk(
            transcription_id,
            next_index,
            body,
        )
        if not isinstance(response, dict):
            raise PandratorMcpError(
                "downstream_unavailable",
                "Pandrator returned an invalid transcription chunk response.",
            )
        returned_index = _integer(response, "next_chunk_index", next_index + 1)
        if returned_index <= next_index or returned_index > chunk_count:
            raise _invalid("Pandrator returned an invalid next transcription chunk index.")
        next_index = returned_index
        initial = response

    if str(initial.get("status") or "uploading").strip().lower() != "uploading":
        return application.get_transcription(
            transcription_id,
            format=arguments.format,
            wait_seconds=arguments.wait_seconds,
        )
    return application.start_transcription(
        transcription_id,
        wait_seconds=arguments.wait_seconds,
    )


def _transcribe_handle(
    runtime: McpRuntime,
    arguments: TranscribeInput,
    *,
    handle: BinaryIO,
    filename: str,
    size_bytes: int,
    before_stat: os.stat_result | None = None,
) -> ToolOutcome:
    if size_bytes > MAX_LOCAL_BYTES:
        raise _invalid(
            "The local transcription source exceeds the 256 MiB limit.",
            max_bytes=MAX_LOCAL_BYTES,
        )
    if size_bytes <= 0:
        raise _invalid("The transcription source must not be empty.")
    handle.seek(0)
    digest = hashlib.sha256()
    remaining = size_bytes
    while remaining:
        chunk = handle.read(min(remaining, HASH_CHUNK_BYTES))
        if not chunk:
            raise PandratorMcpError(
                "source_changed",
                "The transcription source ended before its declared size.",
            )
        if len(chunk) > remaining:
            raise PandratorMcpError(
                "source_changed",
                "The transcription source grew while it was being read.",
            )
        digest.update(chunk)
        remaining -= len(chunk)
    if handle.read(1):
        raise PandratorMcpError(
            "source_changed",
            "The transcription source grew while it was being read.",
        )
    if before_stat is not None:
        after_hash_stat = os.fstat(handle.fileno())
        if (
            before_stat.st_dev,
            before_stat.st_ino,
            before_stat.st_size,
            before_stat.st_mtime_ns,
        ) != (
            after_hash_stat.st_dev,
            after_hash_stat.st_ino,
            after_hash_stat.st_size,
            after_hash_stat.st_mtime_ns,
        ):
            raise PandratorMcpError(
                "source_changed",
                "The local transcription source changed while it was being read.",
            )
    handle.seek(0)
    application = runtime.require_application()
    initial = application.initialize_transcription(
        filename=filename,
        size_bytes=size_bytes,
        sha256=digest.hexdigest(),
        format=arguments.format,
        language=arguments.language,
        engine=arguments.engine,
        model_quantization=arguments.model_quantization,
        compute_backend=arguments.compute_backend,
        idempotency_key=arguments.idempotency_key,
    )
    if not isinstance(initial, dict):
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid transcription initialization response.",
        )
    result = _upload_and_start(
        application,
        initial=initial,
        handle=handle,
        size_bytes=size_bytes,
        arguments=arguments,
    )
    if before_stat is not None:
        after_stat = os.fstat(handle.fileno())
        if (
            before_stat.st_dev,
            before_stat.st_ino,
            before_stat.st_size,
            before_stat.st_mtime_ns,
        ) != (
            after_stat.st_dev,
            after_stat.st_ino,
            after_stat.st_size,
            after_stat.st_mtime_ns,
        ):
            raise PandratorMcpError(
                "source_changed",
                "The local transcription source changed while it was being uploaded.",
            )
    return ToolOutcome(result=result)


def _decode_base64(source: Base64TranscriptionSource) -> bytes:
    try:
        decoded = base64.b64decode(source.data, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _invalid("The base64 transcription source is not valid strict base64.") from error
    if len(decoded) > 8 * 1024 * 1024:
        raise _invalid(
            "The base64 transcription source exceeds the 8 MiB limit.",
            max_bytes=8 * 1024 * 1024,
        )
    return decoded


def transcribe(runtime: McpRuntime, arguments: TranscribeInput) -> ToolOutcome:
    """Upload a local or inline source, resume missing chunks, and start it."""

    source = arguments.source
    if isinstance(source, LocalFileTranscriptionSource):
        _, root = _named_source_root(runtime, source.root)
        parts = _relative_parts(source.path, allow_empty=False)
        filename = _validated_filename(parts[-1])
        descriptor = _open_contained_file(root, parts)
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                before_stat = os.fstat(handle.fileno())
                if not stat.S_ISREG(before_stat.st_mode):
                    raise _invalid("The local transcription source must be a regular file.")
                if before_stat.st_size > MAX_LOCAL_BYTES:
                    raise _invalid(
                        "The local transcription source exceeds the 256 MiB limit.",
                        max_bytes=MAX_LOCAL_BYTES,
                    )
                return _transcribe_handle(
                    runtime,
                    arguments,
                    handle=handle,
                    filename=filename,
                    size_bytes=before_stat.st_size,
                    before_stat=before_stat,
                )
        except OSError as error:
            raise PandratorMcpError(
                "source_unavailable",
                "The local transcription source could not be read.",
                retryable=True,
            ) from error

    filename = _validated_filename(source.filename)
    decoded = _decode_base64(source)
    return _transcribe_handle(
        runtime,
        arguments,
        handle=BytesIO(decoded),
        filename=filename,
        size_bytes=len(decoded),
    )


def get_transcription(
    runtime: McpRuntime,
    arguments: GetTranscriptionInput,
) -> ToolOutcome:
    return ToolOutcome(
        result=runtime.require_application().get_transcription(
            arguments.id,
            format=arguments.format,
            wait_seconds=arguments.wait_seconds,
        )
    )


def get_transcription_result(
    runtime: McpRuntime,
    arguments: GetTranscriptionResultInput,
) -> ToolOutcome:
    return ToolOutcome(
        result=runtime.require_application().get_transcription_result(
            arguments.id,
            format=arguments.format,
            offset=arguments.offset,
            limit=arguments.limit,
        )
    )


def cancel_transcription(
    runtime: McpRuntime,
    arguments: CancelTranscriptionInput,
) -> ToolOutcome:
    return ToolOutcome(
        result=runtime.require_application().cancel_transcription(arguments.id)
    )


def delete_transcription(
    runtime: McpRuntime,
    arguments: DeleteTranscriptionInput,
) -> ToolOutcome:
    return ToolOutcome(
        result=runtime.require_application().delete_transcription(arguments.id)
    )


__all__ = [
    "cancel_transcription",
    "delete_transcription",
    "get_transcription",
    "get_transcription_result",
    "transcribe",
]
