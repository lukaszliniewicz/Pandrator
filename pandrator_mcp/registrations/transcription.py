"""Transcription MCP registrations borrow the adapter's guarded validation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ..context import McpRuntime
from ..native_enums import NativeNullableEnum
from ..native_text import NativeNullableString
from ..schemas.transcription import (
    CancelTranscriptionInput,
    DeleteTranscriptionInput,
    GetTranscriptionInput,
    GetTranscriptionResultInput,
    TranscribeInput,
    TranscriptionSource,
)
from ..tools.transcription import (
    cancel_transcription,
    delete_transcription,
    get_transcription,
    get_transcription_result,
    transcribe,
)


def register_transcription_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_validated_input: Callable[..., dict[str, Any]],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    """Register transcription tools at their original inventory position."""

    @server.tool(
        name="pandrator_transcribe",
        title="Upload and transcribe a source",
        annotations=write_action,
    )
    def transcription_tool(
        source: TranscriptionSource,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        format: Literal["txt", "srt", "json"] = "txt",
        language: Annotated[
            str,
            Field(
                min_length=1,
                max_length=40,
                pattern=r"^[A-Za-z0-9_-]+$",
            ),
        ] = "auto",
        engine: Annotated[
            NativeNullableString,
            Field(max_length=80, pattern=r"^[A-Za-z0-9_-]+$"),
        ] = None,
        model_quantization: Annotated[
            NativeNullableString,
            Field(max_length=40, pattern=r"^[A-Za-z0-9_.-]+$"),
        ] = None,
        compute_backend: NativeNullableEnum[
            Literal["auto", "cpu", "cuda", "vulkan", "metal"]
        ] = None,
        qwen_asr_model: NativeNullableEnum[Literal["qwen3_asr_0_6b", "qwen3_asr_1_7b"]] = None,
        transcription_vocal_isolation: NativeNullableEnum[
            Literal["off", "bs_roformer", "mel_band_roformer", "htdemucs"]
        ] = None,
        wait_seconds: Annotated[int, Field(ge=0, le=30)] = 30,
    ) -> dict[str, Any]:
        """Upload one bounded source, resume its chunks, and start transcription."""

        return _call_with_validated_input(
            transcribe,
            runtime,
            TranscribeInput,
            {
                "source": source,
                "format": format,
                "language": language,
                "engine": engine,
                "model_quantization": model_quantization,
                "compute_backend": compute_backend,
                "qwen_asr_model": qwen_asr_model,
                "transcription_vocal_isolation": transcription_vocal_isolation,
                "wait_seconds": wait_seconds,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_transcription_get",
        title="Inspect transcription status",
        annotations=read_only,
    )
    def transcription_get_tool(
        id: Annotated[str, Field(min_length=1, max_length=120)],
        format: NativeNullableEnum[Literal["txt", "srt", "json"]] = None,
        wait_seconds: Annotated[int, Field(ge=0, le=30)] = 0,
    ) -> dict[str, Any]:
        """Return the bounded status snapshot for one transcription."""

        return _call_with_validated_input(
            get_transcription,
            runtime,
            GetTranscriptionInput,
            {"id": id, "format": format, "wait_seconds": wait_seconds},
        )

    @server.tool(
        name="pandrator_transcription_result",
        title="Read a transcription result page",
        annotations=read_only,
    )
    def transcription_result_tool(
        id: Annotated[str, Field(min_length=1, max_length=120)],
        format: Literal["txt", "srt", "json"] = "txt",
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=32_768)] = 16_000,
    ) -> dict[str, Any]:
        """Read a bounded, concatenable result page."""

        return _call_with_validated_input(
            get_transcription_result,
            runtime,
            GetTranscriptionResultInput,
            {"id": id, "format": format, "offset": offset, "limit": limit},
        )

    @server.tool(
        name="pandrator_transcription_cancel",
        title="Cancel a transcription",
        annotations=write_action,
    )
    def transcription_cancel_tool(
        id: Annotated[str, Field(min_length=1, max_length=120)],
    ) -> dict[str, Any]:
        """Cancel one transcription idempotently."""

        return _call_with_validated_input(
            cancel_transcription,
            runtime,
            CancelTranscriptionInput,
            {"id": id},
        )

    @server.tool(
        name="pandrator_transcription_delete",
        title="Delete a transcription",
        annotations=write_action,
    )
    def transcription_delete_tool(
        id: Annotated[str, Field(min_length=1, max_length=120)],
    ) -> dict[str, Any]:
        """Delete one transcription idempotently."""

        return _call_with_validated_input(
            delete_transcription,
            runtime,
            DeleteTranscriptionInput,
            {"id": id},
        )
