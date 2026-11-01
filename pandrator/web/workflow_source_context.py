"""Shared source workflow context and captured callback contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .models import Artifact, SessionRecord

Progress = Callable[[float, str | None], None]


class GenerationPlanStoreProtocol(Protocol):
    def __call__(
        self,
        session_id: str,
        records: list[dict[str, Any]],
        *,
        settings: dict[str, Any],
        source_revision_id: str | None = None,
        source_artifact_id: str | None = None,
        db_session: Session | None = None,
    ) -> tuple[str, list[str]]: ...


class SourceCleaningProgressFactoryProtocol(Protocol):
    def __call__(
        self,
        progress: Progress,
        start: float,
        end: float,
        *,
        phase_names: list[str],
        phase_budgets: dict[str, int],
    ) -> Callable[[str], None]: ...


@dataclass(frozen=True, slots=True)
class SourceWorkflowContext:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    _resolve_input: Callable[[str], tuple[Artifact, Path]]
    _session_dir: Callable[[str], Path]
    _operation_dir: Callable[[str, str], Path]
    _session_record: Callable[[str], SessionRecord]
    _store_generation_plan: GenerationPlanStoreProtocol
    _validate_download_url: Callable[[str], str]
    _scaled_progress_callback: Callable[[Progress, float, float], Progress]
    _fraction_message_callback: Callable[[Progress, float, float], Callable[[str], None]]
    _source_cleaning_progress_callback: SourceCleaningProgressFactoryProtocol
