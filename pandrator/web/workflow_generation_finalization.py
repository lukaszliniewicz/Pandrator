"""Generation run finalization with explicit database and verification dependencies."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import func, select

from .database import Database
from .models import Artifact, AudioTake, GenerationRun, GenerationSegment, utcnow


def finalize_generation_run(
    database: Database,
    *,
    run_id: str,
    output_run_id: str,
    plan_revision_id: str,
    operation: str,
    repair_requested: bool,
) -> tuple[str, int]:
    """Count remaining segments and settle the run in one write transaction."""
    with database.immediate_session() as session:
        run = session.get(GenerationRun, run_id)
        if run is None:
            raise KeyError(run_id)
        if operation == "rvc" or (
            operation == "regenerate" and run.output_generation_run_id is None
        ):
            incomplete = int(
                session.scalar(
                    select(func.count())
                    .select_from(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan_revision_id,
                        GenerationSegment.removed.is_(False),
                        GenerationSegment.status != "completed",
                    )
                )
                or 0
            )
        else:
            completed_segments = (
                select(AudioTake.generation_segment_id)
                .where(
                    AudioTake.generation_run_id == output_run_id,
                    AudioTake.status == "completed",
                    AudioTake.artifact_id.is_not(None),
                )
                .distinct()
            )
            incomplete = int(
                session.scalar(
                    select(func.count())
                    .select_from(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan_revision_id,
                        GenerationSegment.removed.is_(False),
                        ~GenerationSegment.id.in_(completed_segments),
                    )
                )
                or 0
            )
        final_status = "partial" if incomplete else "completed"
        if run.cancel_requested or run.status in {"cancel_requested", "canceled"}:
            final_status = run.status
        elif run.pause_requested or run.status in {"pausing", "pause_requested", "paused"}:
            final_status = "paused"
            if run.status != "paused":
                run.status = "paused"
                run.updated_at = utcnow()
        else:
            run.status = (
                "running" if repair_requested and final_status == "completed" else final_status
            )
            run.updated_at = utcnow()
        if output_run_id != run_id and final_status in {"completed", "partial"}:
            output_run = session.get(GenerationRun, output_run_id)
            if output_run is not None and output_run.status in {
                "completed",
                "partial",
                "failed",
                "canceled",
            }:
                output_run.status = final_status
                output_run.updated_at = utcnow()
    return final_status, incomplete


def restore_optional_pass_status(database: Database, run_id: str, final_status: str) -> str:
    """Restore the first-pass result or settle a durable stop request."""
    with database.immediate_session() as session:
        current = session.get(GenerationRun, run_id)
        if current is None:
            return final_status
        if current.cancel_requested or current.status in {"cancel_requested", "canceled"}:
            return current.status
        if current.pause_requested or current.status in {"pausing", "pause_requested", "paused"}:
            if current.status != "paused":
                current.status = "paused"
                current.updated_at = utcnow()
            return "paused"
        current.status = final_status
        return final_status


def finalize_run_audio_verification(
    database: Database,
    run_id: str,
    *,
    find_outliers: Callable[[Sequence[float | None]], dict[int, dict[str, float]]],
    add_warning: Callable[[dict[str, Any], dict[str, float]], dict[str, Any]],
) -> int:
    """Check the latest available take per segment within this output run."""
    marked_segment_ids: set[str] = set()
    with database.session() as session:
        rows = list(
            session.execute(
                select(AudioTake, Artifact)
                .join(Artifact, AudioTake.artifact_id == Artifact.id)
                .where(
                    AudioTake.generation_run_id == run_id,
                    AudioTake.status == "completed",
                )
                .order_by(AudioTake.created_at.desc())
            ).all()
        )
        grouped: dict[tuple[str, str], list[tuple[AudioTake, Artifact, dict[str, Any]]]] = {}
        seen_segments: set[str] = set()
        for take, artifact in rows:
            if take.generation_segment_id in seen_segments:
                continue
            seen_segments.add(take.generation_segment_id)
            metadata = dict(artifact.metadata_json or {})
            verification = metadata.get("audio_verification")
            if not isinstance(verification, dict) or verification.get("mode") != "signal":
                continue
            if verification.get("status") != "passed":
                marked_segment_ids.add(take.generation_segment_id)
            key = (str(take.kind or ""), str(take.settings_hash or ""))
            grouped.setdefault(key, []).append((take, artifact, verification))

        for entries in grouped.values():
            values = [(entry[2].get("metrics") or {}).get("rms_dbfs") for entry in entries]
            for index, detail in find_outliers(values).items():
                take, artifact, verification = entries[index]
                metadata = dict(artifact.metadata_json or {})
                metadata["audio_verification"] = add_warning(verification, detail)
                artifact.metadata_json = metadata
                artifact.updated_at = utcnow()
                marked_segment_ids.add(take.generation_segment_id)

        for segment_id in marked_segment_ids:
            segment = session.get(GenerationSegment, segment_id)
            if segment is not None:
                segment.marked = True
                segment.updated_at = utcnow()
    return len(marked_segment_ids)
