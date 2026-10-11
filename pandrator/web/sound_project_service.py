"""Session sound-plan facade; callers own IMMEDIATE write transactions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .artifacts import ArtifactService
from .database import Database
from .models import GenerationPlan, GenerationPlanRevision, GenerationSegment
from .sound_project_commands import (
    assert_revision,
    latest_cues,
    new_cue,
    normalize_placement,
    require_variant,
    write_version,
)
from .sound_project_models import ProjectSoundPlan
from .sound_project_queries import MAX_CUES, cue_rows, cue_summary, current_header, plan_header
from .sound_project_revisions import clone_sound_plan, rebase_sound_plan
from .sound_project_schemas import SoundCueCreate, SoundCueUpdate, SoundPlanRestore


class ProjectSoundService:
    def __init__(self, database: Database, artifacts: ArtifactService):
        self.database = database
        self.artifacts = artifacts

    def get_plan(
        self,
        session_id: str,
        cursor: str | None = None,
        limit: int = 40,
        segment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100.")
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or str(UUID(cursor)) != cursor:
                    raise ValueError
            except ValueError as error:
                raise ValueError("Invalid sound-plan cursor.") from error
        if segment_ids is not None and (
            not isinstance(segment_ids, list)
            or len(segment_ids) > 100
            or any(
                not isinstance(value, str) or not value or len(value) > 36 for value in segment_ids
            )
        ):
            raise ValueError("segment_ids must contain at most 100 segment IDs.")
        with self.database.snapshot_session() as session:
            _record, plan, active = current_header(session, session_id)
            header = plan_header(session_id, plan, active)
            if plan is None:
                return {**header, "cues": {"items": [], "next_cursor": None}}
            rows = cue_rows(
                session, plan.id, cursor=cursor, limit=limit + 1, segment_ids=segment_ids
            )
            visible = rows[:limit]
            return {
                **header,
                "cues": {
                    "items": [cue_summary(row) for row in visible],
                    "next_cursor": visible[-1][0].logical_id if len(rows) > limit else None,
                },
            }

    def _editable(self, session: Session, session_id: str, expected_revision: int):
        record, plan, active = current_header(session, session_id)
        assert_revision(plan, expected_revision)
        if plan is not None and plan.speech_plan_revision_id != active:
            raise ValueError("The speech plan changed; rebase sound cues before editing.")
        return record, plan, active

    def add_in_session(
        self, session: Session, session_id: str, payload: SoundCueCreate | dict[str, Any]
    ) -> dict[str, Any]:
        request = SoundCueCreate.model_validate(payload)
        record, old, active = self._editable(session, session_id, request.expected_revision)
        require_variant(session, request.variant_id)
        anchor, playback, status = normalize_placement(
            session, request.anchor, request.playback, record.workflow_kind, active
        )
        cues = latest_cues(session, old)
        cue = new_cue(request.variant_id, anchor, playback, request.included, status)
        cues.append(cue)
        plan = write_version(session, session_id, old, active, cues)
        return {
            **plan_header(session_id, plan, active),
            "cue": self._summary_in_session(session, plan.id, cue["logical_id"]),
        }

    def update_in_session(
        self,
        session: Session,
        session_id: str,
        logical_cue_id: str,
        payload: SoundCueUpdate | dict[str, Any],
    ) -> dict[str, Any]:
        request = SoundCueUpdate.model_validate(payload)
        record, old, active = self._editable(session, session_id, request.expected_revision)
        cues = latest_cues(session, old)
        cue = next((item for item in cues if item["logical_id"] == logical_cue_id), None)
        if cue is None:
            raise KeyError(logical_cue_id)
        if request.variant_id is not None:
            require_variant(session, request.variant_id)
            cue["variant_id"] = request.variant_id
        if request.anchor is not None or request.playback is not None:
            original_anchor = deepcopy(cue["anchor_json"])
            original_status = cue["placement_status"]
            anchor_input = request.anchor or self._public_anchor(cue["anchor_json"])
            playback_input = request.playback or self._playback(cue["playback_json"])
            anchor, playback, status = normalize_placement(
                session, anchor_input, playback_input, record.workflow_kind, active
            )
            if request.anchor is None:
                retain_resolution = playback["mode"] == cue["playback_json"].get("mode")
                anchor.update(
                    {
                        key: value
                        for key, value in original_anchor.items()
                        if key.startswith("origin_") or (key == "resolution" and retain_resolution)
                    }
                )
                if retain_resolution:
                    status = original_status
            cue.update(anchor_json=anchor, playback_json=playback, placement_status=status)
        if request.included is not None:
            cue["included"] = request.included
        plan = write_version(session, session_id, old, active, cues)
        return {
            **plan_header(session_id, plan, active),
            "cue": self._summary_in_session(session, plan.id, logical_cue_id),
        }

    def remove_in_session(
        self, session: Session, session_id: str, logical_cue_id: str, expected_revision: int
    ) -> dict[str, Any]:
        _record, old, active = self._editable(session, session_id, expected_revision)
        cues = latest_cues(session, old)
        kept = [item for item in cues if item["logical_id"] != logical_cue_id]
        if len(kept) == len(cues):
            raise KeyError(logical_cue_id)
        plan = write_version(session, session_id, old, active, kept)
        return {**plan_header(session_id, plan, active), "removed_logical_id": logical_cue_id}

    def restore_in_session(
        self, session: Session, session_id: str, payload: SoundPlanRestore | dict[str, Any]
    ) -> dict[str, Any]:
        request = SoundPlanRestore.model_validate(payload)
        _record, old, active = current_header(session, session_id)
        assert_revision(old, request.expected_revision)
        source = session.get(ProjectSoundPlan, request.source_plan_id)
        if source is None or source.session_id != session_id:
            raise KeyError(request.source_plan_id)
        cues = latest_cues(session, source)
        if source.speech_plan_revision_id != active:
            anchored_ids = {
                cue["anchor_json"]["segment_id"]
                for cue in cues
                if cue["anchor_json"].get("segment_id")
            }
            active_ids = set(
                session.scalars(
                    select(GenerationSegment.id).where(
                        GenerationSegment.plan_revision_id == active,
                        GenerationSegment.id.in_(anchored_ids),
                    )
                )
            )
            for cue in cues:
                anchor = cue["anchor_json"]
                if anchor["kind"] in {"after_word", "next_pause"} or (
                    anchor["kind"] != "source_time" and anchor.get("segment_id") not in active_ids
                ):
                    cue["placement_status"] = "needs_placement"
        for cue in cues:
            if cue["anchor_json"]["kind"] in {"after_word", "next_pause"}:
                cue["placement_status"] = "needs_placement"
                cue["anchor_json"].pop("resolution", None)
        plan = write_version(session, session_id, old, active, cues)
        return {**plan_header(session_id, plan, active), "restored_from_plan_id": source.id}

    def snapshot_in_session(
        self, session: Session, session_id: str, *, revision: int | None = None
    ) -> dict[str, Any]:
        _record, latest, active = current_header(session, session_id)
        if revision is None:
            plan = latest
        elif type(revision) is not int or revision < 1:
            raise ValueError("revision must be a positive integer.")
        else:
            plan = session.scalar(
                select(ProjectSoundPlan).where(
                    ProjectSoundPlan.session_id == session_id, ProjectSoundPlan.revision == revision
                )
            )
            if plan is None:
                raise KeyError(revision)
        header = plan_header(session_id, plan, active)
        if plan is None:
            return {**header, "cues": []}
        rows = cue_rows(session, plan.id, limit=MAX_CUES + 1)
        if len(rows) > MAX_CUES:
            raise ValueError("Sound plan exceeds the renderer cue limit.")
        cues = []
        for cue, variant, item, artifact in rows:
            summary = cue_summary((cue, variant, item, artifact))
            summary["provenance"] = deepcopy(variant.provenance_json or {})
            summary["artifact_path"] = (
                str(self.artifacts.paths.managed_path(artifact.relative_path))
                if artifact is not None
                else None
            )
            cues.append(summary)
        return {**header, "cues": cues}

    def rebase_in_session(
        self,
        session: Session,
        session_id: str,
        new_speech_plan_revision_id: str,
        boundary_mapping: dict[tuple[str, str], str | None],
    ) -> dict[str, Any] | None:
        return rebase_sound_plan(
            session,
            session_id,
            new_speech_plan_revision_id,
            boundary_mapping,
        )

    def clone_in_session(
        self,
        session: Session,
        source_id: str,
        target_id: str,
        source_segment_map: dict[str, str],
        carry_media_assets: bool = True,
    ) -> dict[str, Any]:
        return clone_sound_plan(
            session,
            source_id,
            target_id,
            source_segment_map,
            carry_media_assets,
        )

    @staticmethod
    def _public_anchor(value: dict[str, Any]):
        from .sound_project_schemas import SoundAnchor

        return SoundAnchor.model_validate(
            {
                key: item for key, item in value.items()
                if not key.startswith("origin_") and key != "resolution"
            }
        )

    @staticmethod
    def _playback(value: dict[str, Any]):
        from .sound_project_schemas import SoundPlayback

        return SoundPlayback.model_validate(value)

    @staticmethod
    def _require_segment(session: Session, segment_id: str, revision_id: str) -> None:
        if (
            session.scalar(
                select(GenerationSegment.id).where(
                    GenerationSegment.id == segment_id,
                    GenerationSegment.plan_revision_id == revision_id,
                )
            )
            is None
        ):
            raise ValueError("Mapped segment does not belong to the new speech revision.")

    @staticmethod
    def _require_session_speech_revision(
        session: Session, session_id: str, revision_id: str
    ) -> None:
        if (
            session.scalar(
                select(GenerationPlanRevision.id)
                .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
                .where(
                    GenerationPlanRevision.id == revision_id,
                    GenerationPlan.session_id == session_id,
                )
            )
            is None
        ):
            raise ValueError("Speech revision does not belong to this project.")

    @staticmethod
    def _summary_in_session(session: Session, plan_id: str, logical_id: str) -> dict[str, Any]:
        rows = cue_rows(session, plan_id, logical_id=logical_id, limit=1)
        if not rows:
            raise KeyError(logical_id)
        return cue_summary(rows[0])
