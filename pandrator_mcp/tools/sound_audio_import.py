"""Approved-root audio import handling for the sound and voice MCP tools."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..clients.application_sounds import MAX_AUDIO_IMPORT_BYTES
from ..context import McpRuntime
from ..errors import PandratorMcpError
from ..results import ToolOutcome
from ..schemas.sounds import ImportAudioReferenceInput
from .e2e import _named_source_root, _open_contained_file, _relative_parts
from .sound_results import _application, _safe_candidate

_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".ogg", ".m4a"})


def _read_approved_audio(
    runtime: McpRuntime,
    *,
    root_name: str,
    relative_path: str,
) -> tuple[bytes, str]:
    _approved_name, root = _named_source_root(runtime, root_name)
    parts = _relative_parts(relative_path, allow_empty=False)
    filename = parts[-1]
    if Path(filename).suffix.casefold() not in _AUDIO_EXTENSIONS:
        raise PandratorMcpError(
            "invalid_format",
            "Audio imports support WAV, MP3, FLAC, OGG, and M4A files.",
        )
    descriptor = _open_contained_file(root, parts)
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
                raise PandratorMcpError(
                    "source_invalid",
                    "The selected audio source must be a non-empty regular file.",
                )
            if before.st_size > MAX_AUDIO_IMPORT_BYTES:
                raise PandratorMcpError(
                    "source_too_large",
                    "MCP audio imports may not exceed 16 MiB.",
                )
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = handle.read(min(1024 * 1024, MAX_AUDIO_IMPORT_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_AUDIO_IMPORT_BYTES:
                    raise PandratorMcpError(
                        "source_too_large",
                        "MCP audio imports may not exceed 16 MiB.",
                    )
            after = os.fstat(handle.fileno())
            before_identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if before_identity != after_identity or size != before.st_size:
                raise PandratorMcpError(
                    "source_changed",
                    "The selected audio source changed while it was being read.",
                    retryable=True,
                )
            content = b"".join(chunks)
            return content, filename
    except OSError as error:
        raise PandratorMcpError(
            "source_unavailable",
            "The selected audio source could not be read.",
            retryable=True,
        ) from error


def import_audio_reference(
    runtime: McpRuntime,
    arguments: ImportAudioReferenceInput,
) -> ToolOutcome:
    body, filename = _read_approved_audio(
        runtime,
        root_name=arguments.root_name,
        relative_path=arguments.relative_path,
    )
    result = _application(runtime).import_audio_reference(
        target=arguments.target,
        filename=filename,
        body=body,
        idempotency_key=arguments.idempotency_key,
    )
    if arguments.target == "sound":
        candidate = _safe_candidate(result)
        return ToolOutcome(result={"candidate": candidate})
    return ToolOutcome(
        result={
            key: result[key] for key in ("artifact_id", "sha256", "size_bytes") if key in result
        }
    )
