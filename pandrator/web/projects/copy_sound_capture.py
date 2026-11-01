"""Bounded sound capture inside an already authorized project-copy snapshot."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..sound_project_models import ProjectSoundCue, ProjectSoundPlan
from ..sound_project_queries import MAX_CUES
from .copy_contracts import project_copy_error
from .copy_sound import CUE_FIELDS, SOUND_FAMILIES, freeze_sound_cue, freeze_sound_variant

_CHUNK = 500
_VARIANT_SCALARS = (
    "id", "workspace_id", "artifact_id", "state", "sample_rate", "channels",
    "frame_count", "duration_seconds",
)


def capture_current_sounds(
    session: Session,
    *,
    source_project_id: str,
    workspace_id: str,
    session_workflows: Mapping[str, str],
    speech_revisions: Mapping[str, str | None],
    max_rows: int,
    max_artifacts: int,
) -> tuple[dict[str, list[dict[str, Any]]], set[str], int]:
    """Read current headers before selected bodies; caller owns project authority."""
    rows: dict[str, list[dict[str, Any]]] = {key: [] for key in SOUND_FAMILIES}
    latest = (
        select(ProjectSoundPlan.session_id, func.max(ProjectSoundPlan.revision).label("revision"))
        .where(ProjectSoundPlan.session_id.in_(session_workflows))
        .group_by(ProjectSoundPlan.session_id)
        .subquery()
    )
    headers = list(session.execute(
        select(
            ProjectSoundPlan.id, ProjectSoundPlan.session_id, ProjectSoundPlan.revision,
            ProjectSoundPlan.speech_plan_revision_id,
            m.GenerationPlan.active_revision_id.label("active_speech_revision"),
        )
        .join(latest, and_(latest.c.session_id == ProjectSoundPlan.session_id,
            latest.c.revision == ProjectSoundPlan.revision))
        .outerjoin(m.GenerationPlan, m.GenerationPlan.session_id == ProjectSoundPlan.session_id)
        .limit(len(session_workflows) + 1)
    ).mappings())
    if len(headers) > len(session_workflows):
        raise project_copy_error("copy_graph_invalid")
    omitted = 0
    for header in headers:
        pin, sid = header["speech_plan_revision_id"], header["session_id"]
        if pin != header["active_speech_revision"] or (
            pin is not None and speech_revisions.get(sid) != pin
        ):
            omitted += 1
            continue
        rows["project_sound_plans"].append({
            key: header[key] for key in ("id", "session_id", "revision", "speech_plan_revision_id")
        })
    plans = {row["id"]: row for row in rows["project_sound_plans"]}
    if not plans:
        return rows, set(), omitted
    cue_headers = list(session.execute(
        select(ProjectSoundCue.id, ProjectSoundCue.plan_id, ProjectSoundCue.variant_id)
        .where(ProjectSoundCue.plan_id.in_(plans)).limit(max_rows + 1)
    ))
    if len(cue_headers) + len(plans) > max_rows or any(
        count > MAX_CUES for count in Counter(row.plan_id for row in cue_headers).values()
    ):
        raise project_copy_error("copy_limit_exceeded")
    variant_ids = sorted({row.variant_id for row in cue_headers})
    artifact_ids: set[str] = set()
    # This first query reads only ownership and readiness, including joined roots.
    # No cue JSON, variant provenance, item name or artifact metadata is selected.
    for start in range(0, len(variant_ids), _CHUNK):
        chunk = variant_ids[start:start + _CHUNK]
        owners = list(session.execute(
            select(
                m.SoundVariant.id, m.SoundVariant.artifact_id, m.SoundVariant.item_id,
                m.SoundVariant.workspace_id.label("variant_workspace"),
                m.SoundVariant.state.label("variant_state"),
                m.SoundItem.workspace_id.label("item_workspace"),
                m.Artifact.workspace_id.label("artifact_workspace"),
                m.Artifact.project_id.label("artifact_project"),
                m.Artifact.session_id.label("artifact_session"),
                m.Artifact.kind.label("artifact_kind"), m.Artifact.role.label("artifact_role"),
                m.Artifact.state.label("artifact_state"),
            )
            .outerjoin(m.SoundItem, m.SoundItem.id == m.SoundVariant.item_id)
            .outerjoin(m.Artifact, m.Artifact.id == m.SoundVariant.artifact_id)
            .where(m.SoundVariant.id.in_(chunk))
        ).mappings())
        if {row["id"] for row in owners} != set(chunk):
            raise project_copy_error("copy_graph_invalid")
        for owner in owners:
            if (
                owner["variant_workspace"] != workspace_id or owner["variant_state"] != "ready"
                or owner["artifact_id"] is None or owner["artifact_workspace"] != workspace_id
                or owner["artifact_project"] not in {None, source_project_id}
                or owner["artifact_session"] is not None or owner["artifact_kind"] != "audio"
                or owner["artifact_role"] != "sound.original" or owner["artifact_state"] != "current"
                or owner["item_id"] is not None and owner["item_workspace"] != workspace_id
            ):
                raise project_copy_error("copy_graph_invalid")
            artifact_ids.add(owner["artifact_id"])
    if len(artifact_ids) > max_artifacts or len(plans) + len(cue_headers) + len(variant_ids) > max_rows:
        raise project_copy_error("copy_limit_exceeded")

    cue_columns = [getattr(ProjectSoundCue, key) for key in sorted(CUE_FIELDS)]
    for start in range(0, len(cue_headers), _CHUNK):
        chunk = [row.id for row in cue_headers[start:start + _CHUNK]]
        for row in session.execute(select(*cue_columns).where(ProjectSoundCue.id.in_(chunk))).mappings():
            try:
                rows["project_sound_cues"].append(freeze_sound_cue(
                    {key: row[key] for key in CUE_FIELDS},
                    workflow_kind=session_workflows[plans[row["plan_id"]]["session_id"]],
                ))
            except (ValueError, TypeError, KeyError) as error:
                raise project_copy_error("copy_graph_invalid") from error
    if len(rows["project_sound_cues"]) != len(cue_headers):
        raise project_copy_error("copy_graph_invalid")

    # Select only the explicit display name from provenance; never load its body.
    name = case(
        (m.SoundItem.id.is_not(None), m.SoundItem.name),
        else_=case((func.json_valid(m.SoundVariant.provenance_json),
            func.json_extract(m.SoundVariant.provenance_json, "$.display_name")), else_=None),
    ).label("display_name")
    variant_columns = [getattr(m.SoundVariant, key) for key in _VARIANT_SCALARS]
    for start in range(0, len(variant_ids), _CHUNK):
        chunk = variant_ids[start:start + _CHUNK]
        for row in session.execute(
            select(*variant_columns, name)
            .outerjoin(m.SoundItem, m.SoundItem.id == m.SoundVariant.item_id)
            .where(m.SoundVariant.id.in_(chunk))
        ).mappings():
            try:
                display = row["display_name"]
                rows["sound_variants"].append(freeze_sound_variant(
                    {key: row[key] for key in _VARIANT_SCALARS},
                    display_name=display if type(display) is str else None,
                ))
            except (ValueError, TypeError, KeyError) as error:
                raise project_copy_error("copy_graph_invalid") from error
    if {row["id"] for row in rows["sound_variants"]} != set(variant_ids):
        raise project_copy_error("copy_graph_invalid")
    return rows, artifact_ids, omitted
