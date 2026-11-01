"""Small callback-backed context for output worker workflows.

The private callback names intentionally preserve the structural interface used by
soundtrack_export helpers, which accept a handler-like object for path and artifact
resolution operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypedDict

from sqlalchemy.orm import Session

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
    _render_sound_mix: Callable[..., Artifact] | None = None
    _before_io: Callable[[], None] | None = None
    _authorization_guard: Callable[[Session], None] | None = None
    _publish_output: Callable[..., Artifact] | None = None
    _output_destination: Callable[[Path], Path] | None = None
    _authorize_input: Callable[[Session, str], None] | None = None


class _OutputIoOptions(TypedDict, total=False):
    before_io: Callable[[], None]


class _OutputAccessOptions(TypedDict, total=False):
    authorization_guard: Callable[[Session], None]
    authorize_input: Callable[[Session, str], None]


def output_access_options(context: OutputWorkflowContext) -> _OutputAccessOptions:
    options: _OutputAccessOptions = {}
    guard = getattr(context, "_authorization_guard", None)
    source_guard = getattr(context, "_authorize_input", None)
    if guard is not None:
        options["authorization_guard"] = guard
    if source_guard is not None:
        options["authorize_input"] = source_guard
    return options


def output_io_options(context: OutputWorkflowContext) -> _OutputIoOptions:
    """Keep omitted callbacks omitted for trusted legacy helper adapters."""
    callback = getattr(context, "_before_io", None)
    return {} if callback is None else {"before_io": callback}


def check_output_io(context: OutputWorkflowContext) -> None:
    callback = getattr(context, "_before_io", None)
    if callback is not None:
        callback()


def require_output_access(context: OutputWorkflowContext, session: Session) -> None:
    callback = getattr(context, "_authorization_guard", None)
    if callback is not None:
        callback(session)


def authorize_output_input(
    context: OutputWorkflowContext, session: Session, artifact_id: str
) -> None:
    """Call after current output authority was proved in this same unit."""
    callback = getattr(context, "_authorize_input", None)
    if callback is not None:
        callback(session, artifact_id)


def output_destination(context: OutputWorkflowContext, path: Path) -> Path:
    callback = getattr(context, "_output_destination", None)
    return callback(path) if callback is not None else path


def publish_output(context: OutputWorkflowContext, path: Path, **kwargs: Any) -> Artifact:
    callback = getattr(context, "_publish_output", None)
    if callback is not None:
        return callback(path, **kwargs)
    return context.artifacts.register(path, **kwargs)
