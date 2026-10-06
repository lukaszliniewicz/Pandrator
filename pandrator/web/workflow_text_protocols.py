"""Exact keyword-rich callable interfaces for text workflow owners."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .agentic_runs import AgenticRunStore, ResumableAgentRun
    from .models import Artifact


class StoreSrtDocumentProtocol(Protocol):
    def __call__(
        self,
        session_id: str,
        artifact: Artifact,
        stage: str,
        *,
        language: str | None = None,
        parent_artifact: Artifact | None = None,
        speaker_overrides: dict[int, str] | None = None,
        logical_passages: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str]: ...


class StoreTimedWordsProtocol(Protocol):
    def __call__(
        self,
        revision_id: str,
        metadata_path: Path,
        *,
        segment_by_source_cue_id: dict[str, int] | None = None,
        segment_by_word_ordinal: dict[int, int] | None = None,
    ) -> int: ...


class TransformCheckpointProtocol(Protocol):
    def __call__(
        self,
        unit_key: str,
        output: dict[str, Any],
        *,
        phase: str = "transform",
        usage_stage: str = ...,
        usage_settings: dict[str, Any] = ...,
    ) -> None: ...


class BeginAgenticOperationProtocol(Protocol):
    def __call__(
        self,
        *,
        payload: dict[str, Any],
        kind: str,
        source_artifact: Artifact,
        requested_settings: dict[str, Any],
        instructions: str,
        usage_settings: dict[str, Any],
    ) -> tuple[AgenticRunStore, ResumableAgentRun, TransformCheckpointProtocol]: ...


class SaveSpeechPlanProposalsProtocol(Protocol):
    def __call__(
        self,
        *,
        library: Any,
        session_id: str,
        plan: dict[str, Any],
        backend: str,
        model_name: str,
        default_language: str,
    ) -> list[dict[str, Any]]: ...


class RecordUsageProtocol(Protocol):
    def __call__(
        self,
        session_id: str,
        stage: str,
        settings: dict[str, Any],
        result: Any,
        *,
        job_id: str | None = None,
        artifact_id: str | None = None,
        generation_run_id: str | None = None,
        agent_run_id: str | None = None,
        request_key: str | None = None,
    ) -> None: ...
