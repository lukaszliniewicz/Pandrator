"""Small callback-backed context for output worker workflows.

The private callback names intentionally preserve the structural interface used by
soundtrack_export helpers, which accept a handler-like object for path and artifact
resolution operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .models import Artifact, SessionRecord


@dataclass(frozen=True, slots=True)
class OutputWorkflowContext:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    _resolve_input: Callable[[str], tuple[Artifact, Path]]
    _session_dir: Callable[[str], Path]
    _session_record: Callable[[str], SessionRecord]
    _operation_dir: Callable[[str, str], Path]
