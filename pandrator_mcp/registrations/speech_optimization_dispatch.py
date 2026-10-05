"""Speech-optimization dispatch registrations borrow the adapter's validation guard."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ..context import McpRuntime
from ..native_text import NativeNullableString
from ..schemas.delegation import DelegationContextCapsuleInput, DelegationContextDeltaInput
from ..schemas.speech_optimization_dispatch import (
    ClaimSpeechOptimizationDispatchBatchInput,
    CreateSpeechOptimizationDispatchRunInput,
    GetSpeechOptimizationDispatchRunInput,
    ListSpeechOptimizationDispatchRunsInput,
    ReleaseSpeechOptimizationDispatchBatchInput,
    RenewSpeechOptimizationDispatchBatchInput,
    SpeechOptimizationDispatchResultInput,
    SubmitSpeechOptimizationDispatchBatchInput,
)
from ..tools.speech_optimization_dispatch import (
    claim_speech_optimization_dispatch_batch,
    create_speech_optimization_dispatch_run,
    get_speech_optimization_dispatch_run,
    list_speech_optimization_dispatch_runs,
    release_speech_optimization_dispatch_batch,
    renew_speech_optimization_dispatch_batch,
    submit_speech_optimization_dispatch_batch,
)


def register_speech_optimization_dispatch_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_validated_input: Callable[..., dict[str, Any]],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    """Register speech-optimization dispatch tools at their existing inventory position."""

    @server.tool(
        name="pandrator_create_speech_optimization_dispatch_run",
        title="Create a passive speech-optimization run",
        annotations=write_action,
    )
    def speech_optimization_dispatch_create_tool(
        session_id: str,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        source_artifact_id: Annotated[
            NativeNullableString,
            Field(min_length=1, max_length=80),
        ] = None,
        language: Annotated[
            NativeNullableString,
            Field(min_length=1, max_length=40),
        ] = None,
        voice_language: Annotated[
            NativeNullableString,
            Field(min_length=1, max_length=40),
        ] = None,
        tts_service: Annotated[
            NativeNullableString,
            Field(min_length=1, max_length=80),
        ] = None,
        instructions: Annotated[str, Field(max_length=16_000)] = "",
        char_limit: Annotated[int, Field(ge=1, le=1_000_000)] = 20_000,
        max_units_per_batch: Annotated[int, Field(ge=1, le=500)] = 100,
        context_before: Annotated[int, Field(ge=0, le=20)] = 4,
        context_after: Annotated[int, Field(ge=0, le=20)] = 2,
        include_timing: bool = True,
        annotation_mode: Literal["off", "dialogue", "speakers"] = "off",
        annotation_only: bool = False,
        execution_mode: Literal["serial", "parallel"] = "serial",
        max_parallel_batches: Annotated[int, Field(ge=1, le=8)] = 1,
        context_capsule: DelegationContextCapsuleInput | None = None,
    ) -> dict[str, Any]:
        """Queue serial or bounded-parallel speech-text batches for this MCP model."""

        return _call_with_validated_input(
            create_speech_optimization_dispatch_run,
            runtime,
            CreateSpeechOptimizationDispatchRunInput,
            {
                key: value
                for key, value in locals().items()
                if key in CreateSpeechOptimizationDispatchRunInput.model_fields
                and value is not None
            },
        )

    @server.tool(
        name="pandrator_list_speech_optimization_dispatch_runs",
        title="List passive speech-optimization runs",
        annotations=read_only,
    )
    def speech_optimization_dispatch_list_tool(
        session_id: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List run metadata without exposing speech text or lease capabilities."""

        return _call_with_validated_input(
            list_speech_optimization_dispatch_runs,
            runtime,
            ListSpeechOptimizationDispatchRunsInput,
            {
                "session_id": session_id,
                "limit": limit,
            },
        )

    @server.tool(
        name="pandrator_get_speech_optimization_dispatch_run",
        title="Inspect a passive speech-optimization run",
        annotations=read_only,
    )
    def speech_optimization_dispatch_get_tool(run_id: str) -> dict[str, Any]:
        """Inspect progress and final artifact metadata without batch contents."""

        return _call_with_validated_input(
            get_speech_optimization_dispatch_run,
            runtime,
            GetSpeechOptimizationDispatchRunInput,
            {
                "run_id": run_id,
            },
        )

    @server.tool(
        name="pandrator_claim_speech_optimization_dispatch_batch",
        title="Claim a passive speech-text batch",
        annotations=write_action,
    )
    def speech_optimization_dispatch_claim_tool(
        run_id: str,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Claim the next sequential units plus read-only boundary context."""

        return _call_with_validated_input(
            claim_speech_optimization_dispatch_batch,
            runtime,
            ClaimSpeechOptimizationDispatchBatchInput,
            {
                "run_id": run_id,
                "lease_seconds": lease_seconds,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_renew_speech_optimization_dispatch_batch",
        title="Renew a speech-optimization lease",
        annotations=write_action,
    )
    def speech_optimization_dispatch_renew_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Renew only the matching speech-text batch lease."""

        return _call_with_validated_input(
            renew_speech_optimization_dispatch_batch,
            runtime,
            RenewSpeechOptimizationDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "lease_seconds": lease_seconds,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_release_speech_optimization_dispatch_batch",
        title="Release a speech-optimization lease",
        annotations=write_action,
    )
    def speech_optimization_dispatch_release_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
    ) -> dict[str, Any]:
        """Return an unfinished speech-text batch to the ready queue."""

        return _call_with_validated_input(
            release_speech_optimization_dispatch_batch,
            runtime,
            ReleaseSpeechOptimizationDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_submit_speech_optimization_dispatch_batch",
        title="Submit a passive speech-text batch",
        annotations=write_action,
    )
    def speech_optimization_dispatch_submit_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        result: SpeechOptimizationDispatchResultInput,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        character_proposals: list[dict[str, Any]] | None = None,
        context_delta: DelegationContextDeltaInput | None = None,
    ) -> dict[str, Any]:
        """Return every unit exactly once so Pandrator can materialize the revision."""

        return _call_with_validated_input(
            submit_speech_optimization_dispatch_batch,
            runtime,
            SubmitSpeechOptimizationDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "result": result,
                "character_proposals": character_proposals or [],
                "context_delta": context_delta or DelegationContextDeltaInput(),
                "idempotency_key": idempotency_key,
            },
        )
