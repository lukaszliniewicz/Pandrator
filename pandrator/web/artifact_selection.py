"""Lineage-aware selection and history for transformation artifacts."""

from __future__ import annotations

from collections import deque
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Artifact, ArtifactEdge, SessionRecord, SessionStageSelection, utcnow


STAGE_OUTPUT_ROLES: dict[str, tuple[str, ...]] = {
    "transcribe": ("transcription",),
    "correct": ("correction",),
    "translate": ("translation",),
    "clean_source": ("clean_text",),
    "prepare_text": ("prepared_text",),
    "optimize_tts": ("tts_optimized",),
}
ROLE_TO_STAGE = {
    role: stage_key
    for stage_key, roles in STAGE_OUTPUT_ROLES.items()
    for role in roles
}
STAGE_RANK = {
    "transcribe": 10,
    "clean_source": 10,
    "correct": 20,
    "prepare_text": 20,
    "translate": 30,
    "optimize_tts": 40,
}


def canonical_stage_key(stage_key: str) -> str:
    return "optimize_tts" if stage_key == "optimize_document" else stage_key


def _edges(session: Session) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    parents: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}
    for parent_id, child_id in session.execute(
        select(ArtifactEdge.parent_artifact_id, ArtifactEdge.child_artifact_id)
    ):
        parents.setdefault(child_id, set()).add(parent_id)
        children.setdefault(parent_id, set()).add(child_id)
    return parents, children


def _closure(seed: str, graph: dict[str, set[str]]) -> set[str]:
    found: set[str] = set()
    pending = [seed]
    while pending:
        current = pending.pop()
        for related in graph.get(current, set()):
            if related in found:
                continue
            found.add(related)
            pending.append(related)
    return found


def _upsert(session: Session, session_id: str, stage_key: str, artifact_id: str) -> SessionStageSelection:
    stage_key = canonical_stage_key(stage_key)
    record = session.get(SessionStageSelection, (session_id, stage_key))
    if record is None:
        record = SessionStageSelection(
            session_id=session_id,
            stage_key=stage_key,
            artifact_id=artifact_id,
        )
        session.add(record)
    elif record.artifact_id != artifact_id:
        record.artifact_id = artifact_id
        record.revision += 1
        record.updated_at = utcnow()
    return record


def _mark_cleared(session: Session, session_id: str, stage_key: str) -> SessionStageSelection:
    stage_key = canonical_stage_key(stage_key)
    record = session.get(SessionStageSelection, (session_id, stage_key))
    if record is None:
        record = SessionStageSelection(
            session_id=session_id,
            stage_key=stage_key,
            artifact_id=None,
        )
        session.add(record)
    elif record.artifact_id is not None:
        record.artifact_id = None
        record.revision += 1
        record.updated_at = utcnow()
    return record


def selected_artifacts(
    session: Session,
    session_id: str,
    artifacts: list[Artifact] | None = None,
) -> dict[str, Artifact]:
    """Return explicit selections, with legacy current artifacts as fallback."""
    records = list(
        session.scalars(
            select(SessionStageSelection).where(SessionStageSelection.session_id == session_id)
        ).all()
    )
    selected: dict[str, Artifact] = {}
    explicit_stages = {record.stage_key for record in records}
    for record in records:
        artifact = session.get(Artifact, record.artifact_id) if record.artifact_id else None
        if artifact is not None:
            selected[record.stage_key] = artifact
    if artifacts is None:
        artifacts = list(
            session.scalars(
                select(Artifact)
                .where(Artifact.session_id == session_id)
                .order_by(Artifact.created_at.desc())
            ).all()
        )
    for artifact in artifacts:
        stage_key = ROLE_TO_STAGE.get(artifact.role)
        if stage_key and stage_key not in explicit_stages and artifact.state == "current":
            selected[stage_key] = artifact
    return selected


def choose_artifact(
    session: Session,
    session_id: str,
    stage_key: str,
    artifact_id: str,
) -> dict[str, Any]:
    """Choose an artifact, restore its ancestors, and clear incompatible descendants."""
    stage_key = canonical_stage_key(stage_key)
    if session.get(SessionRecord, session_id) is None:
        raise KeyError(session_id)
    artifact = session.get(Artifact, artifact_id)
    if artifact is None or artifact.session_id != session_id:
        raise KeyError(artifact_id)
    if artifact.role not in STAGE_OUTPUT_ROLES.get(stage_key, ()):
        raise ValueError(f"Artifact role '{artifact.role}' is not produced by stage '{stage_key}'.")

    parents, _children = _edges(session)
    artifacts_by_id = {
        item.id: item
        for item in session.scalars(select(Artifact).where(Artifact.session_id == session_id)).all()
    }
    # Breadth-first traversal chooses the nearest compatible ancestor when a
    # derived artifact has more than one provenance edge.
    restored: dict[str, str] = {stage_key: artifact.id}
    pending: deque[str] = deque([artifact.id])
    visited = {artifact.id}
    while pending:
        child_id = pending.popleft()
        for parent_id in parents.get(child_id, set()):
            if parent_id in visited:
                continue
            visited.add(parent_id)
            pending.append(parent_id)
            parent = artifacts_by_id.get(parent_id)
            parent_stage = ROLE_TO_STAGE.get(parent.role) if parent else None
            if parent_stage and parent_stage not in restored:
                restored[parent_stage] = parent_id
    for restored_stage, restored_id in restored.items():
        _upsert(session, session_id, restored_stage, restored_id)

    selected_rows = list(
        session.scalars(
            select(SessionStageSelection).where(SessionStageSelection.session_id == session_id)
        ).all()
    )
    cleared: list[str] = []
    chosen_rank = STAGE_RANK.get(stage_key, 0)
    for row in selected_rows:
        if not row.artifact_id or row.stage_key in restored or STAGE_RANK.get(row.stage_key, 0) <= chosen_rank:
            continue
        downstream_ancestors = _closure(row.artifact_id, parents)
        if artifact.id not in downstream_ancestors:
            cleared.append(row.stage_key)
            _mark_cleared(session, session_id, row.stage_key)
    session.flush()
    chosen = session.get(SessionStageSelection, (session_id, stage_key))
    return {
        "stage_key": stage_key,
        "artifact_id": artifact.id,
        "revision": chosen.revision if chosen else 0,
        "restored": restored,
        "cleared": sorted(cleared, key=lambda key: STAGE_RANK.get(key, 0)),
    }


def activate_registered_artifact(session: Session, artifact: Artifact) -> None:
    stage_key = ROLE_TO_STAGE.get(artifact.role)
    if artifact.session_id and stage_key:
        choose_artifact(session, artifact.session_id, stage_key, artifact.id)


def select_source_path(session: Session, session_id: str, source_artifact_id: str | None) -> dict[str, Any]:
    """Restore the newest coherent path below a source, or clear stage choices."""
    rows = list(
        session.scalars(
            select(SessionStageSelection).where(SessionStageSelection.session_id == session_id)
        ).all()
    )
    if not source_artifact_id:
        for row in rows:
            _mark_cleared(session, session_id, row.stage_key)
        return {"restored": {}, "cleared": [row.stage_key for row in rows]}
    _parents, children = _edges(session)
    descendant_ids = _closure(source_artifact_id, children)
    candidates = [
        artifact
        for artifact in session.scalars(
            select(Artifact)
            .where(Artifact.session_id == session_id, Artifact.id.in_(descendant_ids))
            .order_by(Artifact.created_at.desc())
        ).all()
        if artifact.role in ROLE_TO_STAGE
    ] if descendant_ids else []
    candidates.sort(
        key=lambda artifact: (STAGE_RANK.get(ROLE_TO_STAGE[artifact.role], 0), artifact.created_at),
        reverse=True,
    )
    if candidates:
        chosen = candidates[0]
        return choose_artifact(session, session_id, ROLE_TO_STAGE[chosen.role], chosen.id)
    cleared = []
    for row in rows:
        if row.artifact_id is not None:
            cleared.append(row.stage_key)
        _mark_cleared(session, session_id, row.stage_key)
    # Rows for every stage suppress the legacy current-artifact fallback while
    # this new source has no compatible derived path.
    for stage_key in STAGE_OUTPUT_ROLES:
        _mark_cleared(session, session_id, stage_key)
    session.flush()
    return {"restored": {}, "cleared": cleared}


def clear_selection(session: Session, session_id: str, stage_key: str) -> dict[str, Any]:
    stage_key = canonical_stage_key(stage_key)
    if session.get(SessionRecord, session_id) is None:
        raise KeyError(session_id)
    rank = STAGE_RANK.get(stage_key)
    if rank is None:
        raise ValueError(f"Stage '{stage_key}' does not have a selectable artifact.")
    cleared: list[str] = []
    for row in session.scalars(
        select(SessionStageSelection).where(SessionStageSelection.session_id == session_id)
    ).all():
        if STAGE_RANK.get(row.stage_key, -1) >= rank:
            cleared.append(row.stage_key)
            _mark_cleared(session, session_id, row.stage_key)
    if stage_key not in cleared:
        cleared.append(stage_key)
        _mark_cleared(session, session_id, stage_key)
    session.flush()
    record = session.get(SessionStageSelection, (session_id, stage_key))
    return {"stage_key": stage_key, "artifact_id": None, "revision": record.revision if record else 0, "cleared": cleared}


def stage_history(session: Session, session_id: str, stage_key: str) -> dict[str, Any]:
    stage_key = canonical_stage_key(stage_key)
    roles = STAGE_OUTPUT_ROLES.get(stage_key)
    if roles is None:
        raise ValueError(f"Stage '{stage_key}' does not have selectable artifacts.")
    artifacts = list(
        session.scalars(
            select(Artifact)
            .where(Artifact.session_id == session_id, Artifact.role.in_(roles))
            .order_by(Artifact.created_at.asc())
        ).all()
    )
    selected = selected_artifacts(session, session_id, artifacts).get(stage_key)
    selection = session.get(SessionStageSelection, (session_id, stage_key))
    parent_ids_by_child: dict[str, list[str]] = {}
    if artifacts:
        ids = [artifact.id for artifact in artifacts]
        for parent_id, child_id in session.execute(
            select(ArtifactEdge.parent_artifact_id, ArtifactEdge.child_artifact_id).where(
                ArtifactEdge.child_artifact_id.in_(ids)
            )
        ):
            parent_ids_by_child.setdefault(child_id, []).append(parent_id)
    items = []
    for version, artifact in enumerate(artifacts, start=1):
        items.append(
            {
                "id": artifact.id,
                "version": version,
                "kind": artifact.kind,
                "role": artifact.role,
                "relative_path": artifact.relative_path,
                "mime_type": artifact.mime_type,
                "size_bytes": artifact.size_bytes,
                "state": artifact.state,
                "settings_hash": artifact.settings_hash,
                "metadata_json": artifact.metadata_json or {},
                "parent_ids": parent_ids_by_child.get(artifact.id, []),
                "created_at": artifact.created_at.isoformat(),
                "is_selected": bool(selected and selected.id == artifact.id),
            }
        )
    items.reverse()
    return {
        "stage_key": stage_key,
        "selected_artifact_id": selected.id if selected else None,
        "revision": selection.revision if selection else 0,
        "items": items,
    }


def rerun_impact(session: Session, session_id: str, stage_key: str) -> dict[str, Any]:
    stage_key = canonical_stage_key(stage_key)
    history = stage_history(session, session_id, stage_key)
    selected_id = history["selected_artifact_id"]
    if not selected_id:
        return {
            "stage_key": stage_key,
            "selected_artifact": None,
            "dependent_selections": [],
            "descendant_counts": {},
            "descendant_total": 0,
        }
    _parents, children = _edges(session)
    descendant_ids = _closure(selected_id, children)
    dependent_selections = []
    for row in session.scalars(
        select(SessionStageSelection).where(SessionStageSelection.session_id == session_id)
    ).all():
        if not row.artifact_id or row.artifact_id not in descendant_ids:
            continue
        artifact = session.get(Artifact, row.artifact_id)
        if artifact is not None:
            dependent_selections.append(
                {"stage_key": row.stage_key, "artifact_id": artifact.id, "role": artifact.role}
            )
    counts: dict[str, int] = {}
    if descendant_ids:
        for role in session.scalars(
            select(Artifact.role).where(
                Artifact.id.in_(descendant_ids), Artifact.session_id == session_id
            )
        ).all():
            counts[role] = counts.get(role, 0) + 1
    selected_item = next(
        (item for item in history["items"] if item["id"] == selected_id),
        None,
    )
    return {
        "stage_key": stage_key,
        "selected_artifact": selected_item,
        "dependent_selections": sorted(
            dependent_selections, key=lambda item: STAGE_RANK.get(item["stage_key"], 0)
        ),
        "descendant_counts": counts,
        "descendant_total": sum(counts.values()),
    }
