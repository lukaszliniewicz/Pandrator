"""Exact keyword-rich callable interfaces for text workflow owners."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from threading import Event

    from pandrator.runtime import DataPaths

    from .agentic_runs import AgenticRunStore, ResumableAgentRun
    from .credentials import ResolvedCredential
    from .database import Database
    from .models import Artifact
    from .web_research import WebResearchResult
    from .workflow_generation_protocols import Progress


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


class ResolveSecretReferenceProtocol(Protocol):
    def __call__(
        self,
        database: Database,
        paths: DataPaths,
        reference: object,
        *,
        fallback_environment_variable: str = "",
        preloaded_database_credentials: Mapping[str, str] | None = None,
    ) -> ResolvedCredential: ...


class ResolveRunPassageSettingsProtocol(Protocol):
    def __call__(
        self,
        session_id: str,
        payload_settings: dict[str, Any] | None,
        *,
        database: Database | None = None,
    ) -> tuple[dict[str, int], int]: ...


class PreparePassageInputProtocol(Protocol):
    def __call__(
        self,
        artifact: Artifact,
        source_path: Path,
        directory: Path,
        *,
        source_passage_settings: dict[str, Any] | None = None,
        source_passage_settings_revision: int | None = None,
    ) -> tuple[Path, list[dict[str, Any]], dict[int, str]]: ...


class SourcePassageRunLedgerProtocol(Protocol):
    def __call__(
        self,
        session_id: str,
        requested_settings: dict[str, Any],
        *,
        effective: dict[str, int] | None = None,
        settings_revision: int | None = None,
    ) -> dict[str, Any]: ...


class RunStageWebResearchProtocol(Protocol):
    def __call__(
        self,
        *,
        stage: str,
        session_id: str,
        source_artifact: Artifact,
        source_path: Path,
        settings: dict[str, Any],
        progress: Progress,
        cancel_event: Event,
        completed_units: dict[str, dict[str, Any]],
        persist_checkpoint: TransformCheckpointProtocol,
    ) -> WebResearchResult | None: ...
