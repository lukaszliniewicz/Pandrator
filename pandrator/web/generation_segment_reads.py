"""Generation-segment inspection reads and pure response projections."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import load_only
from sqlalchemy.sql.elements import ColumnElement, TextClause

from .database import Database
from .generation_run_selection import resolve_generation_run_take_ids
from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    SessionRecord,
)
from .workspace_settings import WorkspaceSettingsService


def _unresolved_legacy_speaker(error: ValueError) -> bool:
    message = str(error).lower()
    return "unresolved speaker" in message or "ambiguous speaker" in message


def _freeze_legacy_boundaries(
    session,
    revision_id: str,
    snapshot: dict[str, Any],
    segments: list[GenerationSegment],
) -> None:
    """Freeze available legacy rows without consulting current cast controls.

    Rows whose historical markup refers to a speaker absent from the run's
    snapshot remain unprojected, so ``assembly_pause`` returns their stored
    pause. Other rows still use their frozen audio settings and annotations.
    """
    from .speech_boundaries import freeze_boundaries

    try:
        freeze_boundaries(session, revision_id, snapshot, segments=segments)
        return
    except ValueError as exc:
        if not _unresolved_legacy_speaker(exc):
            raise

    unresolved_ids: set[str] = set()

    def locate_unresolved(rows: list[GenerationSegment]) -> None:
        # freeze_boundaries writes only the top-level speech_boundaries key;
        # a shallow copy keeps probes bounded even for annotation-rich runs.
        probe = dict(snapshot)
        try:
            freeze_boundaries(session, revision_id, probe, segments=rows)
        except ValueError as exc:
            if not _unresolved_legacy_speaker(exc):
                raise
            if len(rows) == 1:
                unresolved_ids.add(rows[0].id)
                return
            midpoint = len(rows) // 2
            locate_unresolved(rows[:midpoint])
            locate_unresolved(rows[midpoint:])

    locate_unresolved(segments)
    frozen_boundaries: dict[str, Any] = {}
    safe_chunk: list[GenerationSegment] = []

    def freeze_chunk() -> None:
        if not safe_chunk:
            return
        freeze_boundaries(session, revision_id, snapshot, segments=safe_chunk)
        frozen_boundaries.update(snapshot.pop("speech_boundaries", {}))
        safe_chunk.clear()

    for segment in segments:
        if segment.id in unresolved_ids:
            freeze_chunk()
        else:
            safe_chunk.append(segment)
    freeze_chunk()
    snapshot["speech_boundaries"] = frozen_boundaries


def _take_payload(
    take: AudioTake,
    artifact: Artifact | None,
    *,
    audio_settings: Mapping[str, Any] | None = None,
    allow_assembly_silences: bool = True,
) -> dict[str, Any]:
    raw_metadata = (artifact.metadata_json or {}) if artifact is not None else {}
    metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    rendered_voices = []
    seen = set()
    render_parts = metadata.get("render_parts")
    for part in render_parts if isinstance(render_parts, list) else []:
        if not isinstance(part, Mapping):
            continue
        key = (part.get("voice"), tuple(part.get("speaker_ids") or []), part.get("source"))
        if key not in seen:
            seen.add(key)
            rendered_voices.append({"voice": key[0], "speaker_ids": list(key[1]), "speaker_labels": list(part.get("speaker_labels") or []), "source": key[2]})
    insertions = ()
    if allow_assembly_silences and isinstance(metadata, Mapping):
        try:
            from .assembly_silences import render_part_silences

            insertions = render_part_silences(metadata, audio_settings)
        except (AttributeError, TypeError, ValueError):
            # Old or malformed metadata does not authorize adding audio to a take.
            insertions = ()
    assembly_silences = [
        {
            "at_ms": insertion.offset_frames * 1000 / insertion.sample_rate_hz,
            "duration_ms": insertion.duration_ms,
        }
        for insertion in insertions
    ]
    return {
        "id": take.id,
        "generation_run_id": take.generation_run_id,
        "generation_task_run_id": metadata.get("generation_task_run_id"),
        "artifact_id": take.artifact_id,
        "parent_take_id": take.parent_take_id,
        "kind": take.kind,
        "status": take.status,
        "duration_ms": take.duration_ms,
        "is_active": take.is_active,
        "revision": take.revision,
        "created_at": take.created_at.isoformat(),
        "source_text": metadata.get("source_text"),
        "synthesized_text": metadata.get("synthesized_text"),
        "llm_optimized": bool(metadata.get("llm_optimized")),
        "llm_model": metadata.get("llm_model"),
        "audio_verification": metadata.get("audio_verification"),
        "rendered_voices": rendered_voices,
        "assembly_silences": assembly_silences,
        "is_reused": bool(metadata.get("generation_run_id") and metadata["generation_run_id"] != take.generation_run_id),
    }


class GenerationSegmentReader:
    """Inspect segment pages using request-scoped database and settings inputs."""

    def __init__(self, database: Database, settings: WorkspaceSettingsService):
        self.database = database
        self.settings = settings

    def list_segments(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
        status: str | None = None,
        marked: bool | None = None,
        verification: str | None = None,
        generation_run_id: str | None = None,
        audio_version_side: str = "after",
        take_history: str = "full",
        plan_revision_id: str | None = None,
        view: str = "full",
        fields: list[str] | None = None,
        segment_ids: list[str] | None = None,
        end_ordinal: int | None = None,
        around_ordinal: int | None = None,
        source_cue_id: str | None = None,
        radius: int = 2,
        q: str | None = None,
        match_case: bool = False,
        whole_word: bool = False,
        text_field: str = "text",
        boundary_flags: bool | None = None,
    ) -> dict[str, Any]:
        from .generation_review import project_segments, source_reference_values
        from .generation_search import literal_matches

        if take_history not in {"selected", "full"}:
            raise ValueError("take_history must be 'selected' or 'full'.")
        project_segments({"items": []}, view=view, fields=fields)
        if segment_ids is not None:
            if not 1 <= len(segment_ids) <= 100 or len(set(segment_ids)) != len(segment_ids):
                raise ValueError("segment_ids must contain 1–100 unique identifiers.")
            if around_ordinal is not None or source_cue_id is not None:
                raise ValueError("Choose explicit segment IDs or contextual inspection.")
        requested_plan_revision_id = plan_revision_id
        audio_snapshot, _ = self.settings.resolve(session_id, sections=["tts", "audio"])
        project_assembly_pause = (
            (fields is None and view == "full")
            or (fields is not None and "assembly_silence_after_ms" in fields)
        )
        if radius < 0 or radius > 25:
            raise ValueError("Inspection radius must be between 0 and 25 blocks.")
        if around_ordinal is not None and source_cue_id is not None:
            raise ValueError("Choose either a block ordinal or a source cue for contextual inspection.")
        limit = max(1, min(int(limit), 250))
        if verification not in {None, "issues"}:
            raise ValueError("verification must be 'issues' when supplied.")
        if q is not None and len(q) > 4000:
            raise ValueError("Search query must be at most 4000 characters.")
        if text_field not in {"text", "spoken"}:
            raise ValueError("text_field must be 'text' or 'spoken'.")
        with self.database.session() as session:
            session_record = session.get(SessionRecord, session_id)
            if session_record is None:
                raise KeyError(session_id)
            selected_run = None
            if generation_run_id:
                selected_run = session.get(GenerationRun, generation_run_id)
                if selected_run is None or selected_run.session_id != session_id:
                    raise KeyError(generation_run_id)
            from .generation_audio_versions import audio_version_take_ids

            if audio_version_side not in {"before", "after"}:
                raise ValueError("Audio version side must be before or after.")
            pinned = audio_version_take_ids(selected_run, audio_version_side) if selected_run else None
            if audio_version_side == "before" and pinned is None:
                raise ValueError("A before view requires a recast audio version.")
            plan = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            if plan is None or not plan.active_revision_id:
                return {
                    "items": [],
                    "next_cursor": None,
                    "total": 0,
                    "boundary_flag_count": 0,
                    "plan_revision_id": None,
                    "plan_revision_number": None,
                    "parent_revision_id": None,
                    "operation_json": {},
                    "speech_block_settings": {},
                }
            plan_revision_id = requested_plan_revision_id or plan.active_revision_id
            if selected_run is not None:
                if requested_plan_revision_id and selected_run.plan_revision_id != requested_plan_revision_id:
                    raise ValueError("The selected run does not use the requested speech-plan revision.")
                plan_revision_id = selected_run.plan_revision_id
            plan_revision = session.get(GenerationPlanRevision, plan_revision_id)
            if plan_revision is None or plan_revision.plan_id != plan.id:
                raise KeyError(plan_revision_id)
            revision_settings = (
                dict(plan_revision.settings_json or {})
                if plan_revision is not None
                else {}
            )
            speech_block_settings = {
                key: value
                for key, value in revision_settings.items()
                if str(key).startswith("speech_block_")
            }
            filters: list[ColumnElement[bool] | TextClause] = [
                GenerationSegment.plan_revision_id == plan_revision_id
            ]
            if segment_ids is not None:
                found_ids = set(session.scalars(select(GenerationSegment.id).where(
                    GenerationSegment.plan_revision_id == plan_revision_id,
                    GenerationSegment.id.in_(segment_ids),
                )))
                if found_ids != set(segment_ids):
                    raise ValueError("Some segment IDs do not belong to the selected speech revision.")
                filters.append(GenerationSegment.id.in_(segment_ids))
            search_matches_by_id: dict[str, list[dict[str, int]]] = {}
            if q:
                searchable_rows = session.execute(
                    select(
                        GenerationSegment.id,
                        GenerationSegment.text,
                        GenerationSegment.optimized_text,
                    ).where(GenerationSegment.plan_revision_id == plan_revision_id)
                )
                matched_ids = []
                for row in searchable_rows:
                    matches = literal_matches(
                        (
                            row.optimized_text
                            if text_field == "spoken" and row.optimized_text
                            else row.text
                        )
                        or "",
                        q,
                        match_case=match_case,
                        whole_word=whole_word,
                    )
                    if matches:
                        matched_ids.append(row.id)
                        search_matches_by_id[row.id] = matches
                filters.append(
                    sql_text(
                        "generation_segments.id IN "
                        "(SELECT value FROM json_each(:matched_ids_json))"
                    ).bindparams(matched_ids_json=json.dumps(matched_ids))
                )
            if status:
                filters.append(GenerationSegment.status == status)
            if marked is not None:
                filters.append(GenerationSegment.marked.is_(marked))
            risk_flags_count = func.coalesce(
                func.json_array_length(
                    func.json_extract(
                        GenerationSegment.speech_block_provenance_json,
                        "$.risk_flags",
                    )
                ),
                0,
            )
            if boundary_flags is not None:
                filters.append(GenerationSegment.removed.is_(False))
                if boundary_flags:
                    filters.append(risk_flags_count > 0)
                else:
                    filters.append(risk_flags_count == 0)
            if verification == "issues":
                verification_status = Artifact.metadata_json["audio_verification"][
                    "status"
                ].as_string()
                filters.append(
                    select(AudioTake.id)
                    .join(Artifact, Artifact.id == AudioTake.artifact_id)
                    .where(
                        AudioTake.generation_segment_id == GenerationSegment.id,
                        AudioTake.is_active.is_(True),
                        verification_status.in_(("warning", "failed")),
                    )
                    .exists()
                )
            if around_ordinal is not None:
                if around_ordinal < 0:
                    raise ValueError("Block ordinals cannot be negative.")
                cursor = max(0, around_ordinal - radius)
                end_ordinal = around_ordinal + radius
            if source_cue_id is not None:
                evidence_rows = session.execute(select(
                    GenerationSegment.ordinal, GenerationSegment.source_segment_ids_json,
                    GenerationSegment.speech_block_provenance_json,
                ).where(GenerationSegment.plan_revision_id == plan_revision_id))
                matching = [
                    ordinal
                    for ordinal, source_ids, provenance in evidence_rows
                    if str(source_cue_id) in source_reference_values(provenance, source_ids)
                ]
                if not matching:
                    raise ValueError("No speech blocks reference the requested source cue in this revision.")
                cursor = max(0, min(matching) - radius)
                end_ordinal = max(matching) + radius
            page_filters = [*filters, GenerationSegment.ordinal >= max(0, cursor)]
            if end_ordinal is not None:
                if end_ordinal < max(0, cursor):
                    raise ValueError("The end ordinal must not precede the requested start.")
                # A visible/context window limits rows, not the matching plan
                # count. Otherwise refreshing eight rows shrinks the header
                # from the full plan size to eight.
                page_filters.append(GenerationSegment.ordinal <= end_ordinal)
            rows = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(*page_filters)
                    .order_by(GenerationSegment.ordinal)
                    .limit(limit + 1)
                ).all()
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            assembly_pauses: dict[str, int] = {}
            run_settings_snapshot = (
                selected_run.settings_snapshot_json
                if selected_run is not None
                and isinstance(selected_run.settings_snapshot_json, Mapping)
                else {}
            )
            take_audio_settings = (
                run_settings_snapshot.get("audio")
                if selected_run is not None
                else audio_snapshot.get("audio")
            )
            if not isinstance(take_audio_settings, Mapping):
                take_audio_settings = None
            if project_assembly_pause:
                if session_record.workflow_kind != "audiobook":
                    assembly_pauses = {row.id: 0 for row in rows}
                elif rows:
                    if selected_run is not None:
                        boundary_snapshot = deepcopy(dict(run_settings_snapshot))
                    else:
                        boundary_snapshot = {
                            "audio": deepcopy(audio_snapshot.get("audio") or {})
                        }
                    if "speech_boundaries" not in boundary_snapshot:
                        if selected_run is not None:
                            performance_snapshot = boundary_snapshot.get(
                                "performance_snapshot"
                            )
                            if not isinstance(performance_snapshot, Mapping):
                                performance_snapshot = {}
                            else:
                                performance_snapshot = dict(performance_snapshot)
                            if not isinstance(
                                performance_snapshot.get("character_dictionary"), list
                            ):
                                # The parser expects a catalogue list; an empty
                                # list prevents fallback to mutable current cast data.
                                performance_snapshot["character_dictionary"] = []
                            boundary_snapshot["performance_snapshot"] = (
                                performance_snapshot
                            )
                        boundary_rows = [row for row in rows if not row.removed]
                        if boundary_rows:
                            next_ordinals = [
                                select(func.min(GenerationSegment.ordinal))
                                .where(
                                    GenerationSegment.plan_revision_id == plan_revision_id,
                                    GenerationSegment.removed.is_(False),
                                    GenerationSegment.ordinal > row.ordinal,
                                )
                                .scalar_subquery()
                                for row in boundary_rows
                            ]
                            successors = list(
                                session.scalars(
                                    select(GenerationSegment)
                                    .where(
                                        GenerationSegment.plan_revision_id == plan_revision_id,
                                        GenerationSegment.removed.is_(False),
                                        GenerationSegment.ordinal.in_(next_ordinals),
                                    )
                                    .order_by(GenerationSegment.ordinal)
                                    .limit(len(boundary_rows))
                                ).all()
                            )
                            boundary_rows = sorted(
                                {row.id: row for row in (*boundary_rows, *successors)}.values(),
                                key=lambda row: row.ordinal,
                            )
                        if selected_run is None:
                            # Use the same resolved provider/project settings as
                            # the audio identity below, without a second read.
                            tts_settings = audio_snapshot.get("tts") or {}
                            if (
                                tts_settings.get("casting_enabled")
                                or tts_settings.get("performance_enabled")
                            ):
                                from .models import PerformancePlan

                                adopted_plan = session.scalar(
                                    select(PerformancePlan.id).where(
                                        PerformancePlan.plan_revision_id == plan_revision_id,
                                        PerformancePlan.status == "adopted",
                                    )
                                )
                                if adopted_plan:
                                    from .performance_plans import freeze_performance_snapshot

                                    freeze_performance_snapshot(
                                        session, plan_revision_id, boundary_snapshot
                                    )
                        if selected_run is not None:
                            _freeze_legacy_boundaries(
                                session,
                                plan_revision_id,
                                boundary_snapshot,
                                boundary_rows,
                            )
                        else:
                            from .speech_boundaries import freeze_boundaries

                            freeze_boundaries(
                                session,
                                plan_revision_id,
                                boundary_snapshot,
                                segments=boundary_rows,
                            )
                    from .speech_boundaries import assembly_pause

                    assembly_pauses = {
                        row.id: (
                            0
                            if row.node_kind == "subtitle_cue"
                            else assembly_pause(row, boundary_snapshot)
                        )
                        for row in rows
                    }
            takes_by_segment: dict[str, list[AudioTake]] = {}
            artifacts_by_id: dict[str, Artifact] = {}
            take_stats_by_segment: dict[str, dict[str, Any]] = {}
            if rows and selected_run is not None and pinned is None:
                # Match assembly's chronological cutoff, not whichever newer
                # take happens to be active while reviewing an earlier run.
                pinned = resolve_generation_run_take_ids(
                    session,
                    selected_run,
                    segment_ids=[row.id for row in rows],
                )
            if rows:
                page_segment_ids = [item.id for item in rows]
                if take_history == "full":
                    takes = list(
                        session.scalars(
                            select(AudioTake)
                            .where(
                                AudioTake.generation_segment_id.in_(page_segment_ids)
                            )
                            .order_by(AudioTake.created_at.desc())
                        ).all()
                    )
                    for take in takes:
                        takes_by_segment.setdefault(
                            take.generation_segment_id, []
                        ).append(take)
                    artifact_ids = [
                        take.artifact_id for take in takes if take.artifact_id
                    ]
                else:
                    take_metadata_by_segment: dict[str, list[Any]] = {}
                    metadata_rows = session.execute(
                        select(
                            AudioTake.id,
                            AudioTake.generation_segment_id,
                            AudioTake.is_active,
                            AudioTake.status,
                            AudioTake.artifact_id,
                        )
                        .where(AudioTake.generation_segment_id.in_(page_segment_ids))
                        .order_by(AudioTake.created_at.desc())
                    )
                    for metadata in metadata_rows:
                        take_metadata_by_segment.setdefault(
                            metadata.generation_segment_id, []
                        ).append(metadata)

                    selected_take_ids: set[str] = set()
                    for row in rows:
                        metadata = take_metadata_by_segment.get(row.id, [])
                        selected_take_id = (
                            pinned.get(row.id)
                            if pinned is not None
                            else next(
                                (
                                    take.id
                                    for take in metadata
                                    if take.is_active
                                ),
                                None,
                            )
                        )
                        if selected_take_id is not None and any(
                            take.id == selected_take_id for take in metadata
                        ):
                            selected_take_ids.add(selected_take_id)

                        active_usable = next(
                            (
                                take
                                for take in metadata
                                if take.is_active
                                and take.status == "completed"
                                and take.artifact_id
                            ),
                            None,
                        )
                        take_stats_by_segment[row.id] = {
                            "take_count": len(metadata),
                            "take_history_loaded": len(metadata) == 0,
                            "active_take_id": active_usable.id
                            if active_usable is not None
                            else None,
                            "has_usable_take": bool(
                                active_usable is not None and row.status == "completed"
                            ),
                        }

                    if selected_take_ids:
                        takes = list(
                            session.scalars(
                                select(AudioTake).where(
                                    AudioTake.id.in_(selected_take_ids)
                                )
                            ).all()
                        )
                        for take in takes:
                            takes_by_segment.setdefault(
                                take.generation_segment_id, []
                            ).append(take)
                        for row in rows:
                            stats = take_stats_by_segment[row.id]
                            stats["take_history_loaded"] = (
                                len(takes_by_segment.get(row.id, []))
                                == stats["take_count"]
                            )
                    artifact_ids = [
                        take.artifact_id
                        for takes in takes_by_segment.values()
                        for take in takes
                        if take.artifact_id
                    ]
                if artifact_ids:
                    artifact_query = select(Artifact).where(
                        Artifact.id.in_(artifact_ids)
                    )
                    if take_history == "selected":
                        artifact_query = artifact_query.options(
                            load_only(
                                Artifact.id,
                                Artifact.state,
                                Artifact.metadata_json,
                                Artifact.content_hash,
                            )
                        )
                    artifacts_by_id = {
                        artifact.id: artifact
                        for artifact in session.scalars(
                            artifact_query
                        ).all()
                    }
            from .passage_markers import describe_passages
            from .speech_annotation_view import annotation_view_by_segment

            annotation_view = annotation_view_by_segment(
                session, plan_revision_id, rows, selected_run
            ) if view == "full" and fields is None else {}

            items = [
                {
                    "id": item.id,
                    "ordinal": item.ordinal,
                    "node_kind": item.node_kind,
                    "paragraph_break_after": item.paragraph_break_after,
                    "speaker": item.speaker,
                    "text": item.text,
                    "source_segment_ids": list(item.source_segment_ids_json or []),
                    "speech_block_provenance": dict(
                        item.speech_block_provenance_json or {}
                    ),
                    **({"passage_structure": describe_passages(item)}
                       if (view == "full" and fields is None) or (fields and "passage_structure" in fields) else {}),
                    "alignment_group": item.alignment_group,
                    "optimized_text": item.optimized_text,
                    "speech_plan": dict(item.speech_plan_json or {}),
                    **annotation_view.get(item.id, {}),
                    "optimization_status": item.optimization_status,
                    "optimization_reviewed": item.optimization_reviewed,
                    "optimization_model": item.optimization_model,
                    "voice_id": item.voice_id,
                    "voice": item.voice,
                    "language": item.language,
                    "silence_after_ms": item.silence_after_ms,
                    **(
                        {"assembly_silence_after_ms": assembly_pauses.get(item.id, 0)}
                        if project_assembly_pause
                        else {}
                    ),
                    "marked": item.marked,
                    "removed": item.removed,
                    "status": item.status,
                    "revision": item.revision,
                    **({"history_take_id": pinned.get(item.id)} if pinned is not None else {}),
                    "takes": [
                        _take_payload(
                            take,
                            artifacts_by_id.get(take.artifact_id)
                            if take.artifact_id else None,
                            audio_settings=take_audio_settings,
                            allow_assembly_silences=(
                                session_record.workflow_kind == "audiobook"
                                and item.node_kind != "subtitle_cue"
                            ),
                        )
                        for take in takes_by_segment.get(item.id, [])
                    ],
                    **take_stats_by_segment.get(item.id, {}),
                }
                for item in rows
            ]
            if q is not None:
                for item in items:
                    item["search_matches"] = search_matches_by_id.get(item["id"], [])
            from .generation_audio_identity import (
                AudioIdentityContext,
                take_reuse_reason,
            )

            audio_identity = AudioIdentityContext(session, selected_run.settings_snapshot_json if selected_run else audio_snapshot)
            for row, item in zip(rows, items, strict=True):
                active_take = next((take for take in takes_by_segment.get(row.id, [])
                                    if take.id == pinned.get(row.id)), None) if pinned is not None else next(
                    (take for take in takes_by_segment.get(row.id, []) if take.is_active), None
                )
                artifact = artifacts_by_id.get(active_take.artifact_id) if active_take and active_take.artifact_id else None
                item["effective_take"] = (
                    {
                        "id": active_take.id,
                        "generation_run_id": active_take.generation_run_id,
                        "artifact_id": active_take.artifact_id,
                        "content_hash": artifact.content_hash,
                        "duration_ms": active_take.duration_ms,
                        "status": active_take.status,
                    }
                    if active_take and artifact and artifact.state != "deleted"
                    and active_take.status in {"completed", "stale"}
                    else None
                )
                reason = take_reuse_reason(row, active_take, artifact, audio_identity.for_segment(row))
                item["audio_reuse_reason"] = reason
                item["has_reusable_take"] = reason == "reusable"
            total = int(
                session.scalar(
                    select(func.count()).select_from(GenerationSegment).where(*filters)
                )
                or 0
            )
            boundary_flag_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan_revision_id,
                        GenerationSegment.removed.is_(False),
                        risk_flags_count > 0,
                    )
                )
                or 0
            )
            return project_segments({
                "items": items,
                "next_cursor": rows[-1].ordinal + 1 if rows and has_more else None,
                "total": total,
                "boundary_flag_count": boundary_flag_count,
                "plan_revision_id": plan_revision_id,
                "active_plan_revision_id": plan.active_revision_id,
                "is_active_revision": plan_revision_id == plan.active_revision_id,
                "plan_revision_number": (
                    plan_revision.revision_number if plan_revision is not None else None
                ),
                "parent_revision_id": (
                    plan_revision.parent_revision_id
                    if plan_revision is not None
                    else None
                ),
                "operation_json": (
                    dict(plan_revision.operation_json or {})
                    if plan_revision is not None
                    else {}
                ),
                "speech_block_settings": speech_block_settings,
            }, view=view, fields=fields)


def updated_segment_payload(segment: GenerationSegment) -> dict[str, Any]:
    from .passage_markers import describe_passages

    return {
        "passage_structure": describe_passages(segment),
        "id": segment.id,
        "plan_revision_id": segment.plan_revision_id,
        "ordinal": segment.ordinal,
        "node_kind": segment.node_kind,
        "paragraph_break_after": segment.paragraph_break_after,
        "speaker": segment.speaker,
        "alignment_group": segment.alignment_group,
        "text": segment.text,
        "source_segment_ids": list(segment.source_segment_ids_json or []),
        "speech_block_provenance": dict(segment.speech_block_provenance_json or {}),
        "optimized_text": segment.optimized_text,
        "speech_plan": dict(segment.speech_plan_json or {}),
        "optimization_status": segment.optimization_status,
        "optimization_reviewed": segment.optimization_reviewed,
        "optimization_model": segment.optimization_model,
        "voice_id": segment.voice_id,
        "voice": segment.voice,
        "language": segment.language,
        "silence_after_ms": segment.silence_after_ms,
        "marked": segment.marked,
        "removed": segment.removed,
        "status": segment.status,
        "revision": segment.revision,
    }
