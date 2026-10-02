"""Generation take publication inside a caller-owned transaction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .artifacts import ArtifactService, PreparedArtifactRegistration
from .models import AudioTake, GenerationSegment, UsageEvent, utcnow


class MarkOutputAssembliesStaleProtocol(Protocol):
    def __call__(
        self,
        session: Session,
        session_id: str,
        *,
        generation_run_id: str | None = None,
        include_later_runs: bool = False,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class GenerationTakePublication:
    segment: GenerationSegment
    session_id: str
    run_id: str
    output_run_id: str
    path: Path
    kind: str
    duration_ms: int
    parent_ids: list[str]
    parent_take_id: str | None
    settings: dict[str, Any]
    metadata: dict[str, Any]
    prepared: PreparedArtifactRegistration
    expected_selection: dict[str, Any] | None
    verification: dict[str, Any] | None


def publish_generation_take(
    session: Session,
    artifacts: ArtifactService,
    publication: GenerationTakePublication,
    *,
    usage_event_factory: Callable[[str], UsageEvent | None] | None,
    mark_stale: MarkOutputAssembliesStaleProtocol,
) -> None:
    """Publish one take without opening or committing a transaction."""
    segment = publication.segment
    segment_id = segment.id
    artifact = artifacts.register_in_session(
        session,
        publication.path,
        kind="audio",
        role="generation_take",
        session_id=publication.session_id,
        parent_ids=publication.parent_ids,
        settings=publication.settings,
        metadata=publication.metadata,
        _prepared=publication.prepared,
    )
    if usage_event_factory is not None:
        usage_event = usage_event_factory(artifact.id)
        if usage_event is not None:
            session.add(usage_event)
    from .generation_edit_audio import selection_is_unchanged

    selected_before = session.scalar(
        select(AudioTake).where(
            AudioTake.generation_segment_id == segment_id,
            AudioTake.is_active.is_(True),
        )
    )
    expected_selection = publication.expected_selection
    activate_new = selection_is_unchanged(selected_before, expected_selection)
    if activate_new:
        deactivate = update(AudioTake).where(
            AudioTake.generation_segment_id == segment_id,
            AudioTake.is_active.is_(True),
        )
        session.execute(
            deactivate.values(
                is_active=False,
                revision=AudioTake.revision + 1,
            ).execution_options(synchronize_session=False)
        )
    new_take = AudioTake(
        generation_segment_id=segment_id,
        generation_run_id=publication.output_run_id,
        artifact_id=artifact.id,
        parent_take_id=publication.parent_take_id,
        kind=publication.kind,
        status="completed",
        settings_hash=artifact.settings_hash,
        duration_ms=publication.duration_ms,
        is_active=activate_new,
    )
    session.add(new_take)
    session.flush()
    from .generation_edit_audio import publish_to_edit_copy

    publish_to_edit_copy(session, segment, new_take, artifact, expected_selection)
    segment.status = (
        "completed" if activate_new else (selected_before.status if selected_before else "ready")
    )
    if publication.verification is not None and publication.verification.get("status") != "passed":
        segment.marked = True
    segment.updated_at = utcnow()
    mark_stale(
        session,
        publication.session_id,
        generation_run_id=publication.output_run_id,
        include_later_runs=publication.output_run_id != publication.run_id,
    )
