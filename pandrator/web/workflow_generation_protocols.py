"""Exact callable interfaces shared by generation execution owners."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pandrator.runtime import DataPaths

    from .database import Database
    from .manager_proxy import LocalManagerProxy
    from .models import UsageEvent
    from .tts_providers import TtsBatchResult, TtsCapabilities

Progress = Callable[[float, str | None], None]


class OptimizeGenerationTextsProtocol(Protocol):
    def __call__(
        self,
        session_id: str,
        segment_ids: list[str],
        texts: list[str],
        settings: dict[str, Any],
        cancel_event: threading.Event,
        progress: Progress,
        *,
        job_id: str | None = None,
        generation_run_id: str | None = None,
        source_artifact_id: str | None = None,
        pronunciation_settings: dict[str, Any] | None = None,
        pronunciation_language: str | None = None,
        pronunciation_voice_language: str | None = None,
    ) -> tuple[list[str], str]: ...


class StreamingTtsBatchProtocol(Protocol):
    def __call__(
        self,
        items: list[tuple[str, str, dict[str, Any]]],
        *,
        batch_size: int,
        tts_urls: dict[str, str],
        cancel_event: threading.Event,
    ) -> Iterator[TtsBatchResult]: ...


class EnsureQwenVoiceProtocol(Protocol):
    def __call__(
        self,
        settings: dict[str, Any],
        *,
        base_url: str,
        verified: set[str],
        cancel_event: threading.Event,
    ) -> None: ...


class NegotiatedTtsBatchProtocol(Protocol):
    def __call__(
        self,
        settings: dict[str, Any],
        tts_urls: dict[str, str],
        *,
        capabilities: TtsCapabilities | None = None,
    ) -> int: ...


class RecordTtsUsageProtocol(Protocol):
    def __call__(
        self,
        session_id: str,
        settings: dict[str, Any],
        text: str,
        duration_ms: int,
        *,
        job_id: str | None = None,
        artifact_id: str | None = None,
        generation_run_id: str | None = None,
    ) -> None: ...


class TtsUsageEventProtocol(Protocol):
    def __call__(
        self,
        session_id: str,
        settings: dict[str, Any],
        text: str,
        duration_ms: int,
        *,
        job_id: str | None = None,
        artifact_id: str | None = None,
        generation_run_id: str | None = None,
    ) -> UsageEvent | None: ...


class HydrateTtsSettingsProtocol(Protocol):
    def __call__(
        self,
        database: Database,
        paths: DataPaths,
        settings: dict[str, Any],
        *,
        manager_bridge: LocalManagerProxy | None = None,
    ) -> dict[str, Any]: ...


class ApplySegmentTtsOverridesProtocol(Protocol):
    def __call__(
        self,
        settings: dict[str, Any],
        *,
        language: str | None = None,
        voice: str | None = None,
    ) -> dict[str, Any]: ...


class VoiceoverSecondPassProtocol(Protocol):
    def __call__(
        self,
        settings_snapshot: dict[str, Any],
        *,
        operation: str,
        has_selected_ids: bool,
        workflow_kind: str,
    ) -> str | None: ...
