"""Generation-segment inspection reads and pure response projections."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.sql.elements import ColumnElement, TextClause

from .database import Database
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


def _take_payload(take: AudioTake, artifact: Artifact | None) -> dict[str, Any]:
    metadata = (artifact.metadata_json or {}) if artifact is not None else {}
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
        plan_revision_id: str | None = None,
        view: str = "full",
        fields: list[str] | None = None,
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

        project_segments({"items": []}, view=view, fields=fields)
        requested_plan_revision_id = plan_revision_id
        audio_snapshot, _ = self.settings.resolve(session_id)
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
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            selected_run = None
            if generation_run_id:
                selected_run = session.get(GenerationRun, generation_run_id)
                if selected_run is None or selected_run.session_id != session_id:
                    raise KeyError(generation_run_id)
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
            if end_ordinal is not None:
                if end_ordinal < max(0, cursor):
                    raise ValueError("The end ordinal must not precede the requested start.")
                filters.append(GenerationSegment.ordinal <= end_ordinal)
            page_filters = [*filters, GenerationSegment.ordinal >= max(0, cursor)]
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
            takes_by_segment: dict[str, list[AudioTake]] = {}
            artifacts_by_id: dict[str, Artifact] = {}
            if rows:
                takes = list(
                    session.scalars(
                        select(AudioTake)
                        .where(
                            AudioTake.generation_segment_id.in_(
                                [item.id for item in rows]
                            )
                        )
                        .order_by(AudioTake.created_at.desc())
                    ).all()
                )
                for take in takes:
                    takes_by_segment.setdefault(take.generation_segment_id, []).append(
                        take
                    )
                artifact_ids = [take.artifact_id for take in takes if take.artifact_id]
                if artifact_ids:
                    artifacts_by_id = {
                        artifact.id: artifact
                        for artifact in session.scalars(
                            select(Artifact).where(Artifact.id.in_(artifact_ids))
                        ).all()
                    }
            from .passage_markers import describe_passages
            from .speech_annotation_view import annotation_xml_by_segment

            annotation_xml = annotation_xml_by_segment(
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
                    **({"speech_annotation_xml": annotation_xml[item.id]}
                       if annotation_xml.get(item.id) else {}),
                    "optimization_status": item.optimization_status,
                    "optimization_reviewed": item.optimization_reviewed,
                    "optimization_model": item.optimization_model,
                    "voice_id": item.voice_id,
                    "voice": item.voice,
                    "language": item.language,
                    "silence_after_ms": item.silence_after_ms,
                    "marked": item.marked,
                    "removed": item.removed,
                    "status": item.status,
                    "revision": item.revision,
                    "takes": [
                        _take_payload(
                            take,
                            artifacts_by_id.get(take.artifact_id)
                            if take.artifact_id else None,
                        )
                        for take in takes_by_segment.get(item.id, [])
                    ],
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
                active_take = next((take for take in takes_by_segment.get(row.id, []) if take.is_active), None)
                artifact = artifacts_by_id.get(active_take.artifact_id) if active_take and active_take.artifact_id else None
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
