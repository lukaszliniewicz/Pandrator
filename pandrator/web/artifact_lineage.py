"""Ancestry walks shared by fork validation and dispatch admission."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Artifact, ArtifactEdge


def artifact_ancestor_ids(session: Session, artifact_id: str) -> set[str]:
    """Return the artifact and every artifact it descends from.

    Ancestry follows registered parent edges and the ``source_artifact_id``
    recorded in an artifact's metadata. The walk is iterative and visits each
    artifact once, so a cycle cannot keep it running.
    """

    ancestors: set[str] = set()
    frontier = [str(artifact_id)]
    while frontier:
        child_id = frontier.pop()
        if child_id in ancestors:
            continue
        ancestors.add(child_id)
        frontier.extend(
            session.scalars(
                select(ArtifactEdge.parent_artifact_id).where(
                    ArtifactEdge.child_artifact_id == child_id
                )
            ).all()
        )
        item = session.get(Artifact, child_id)
        source_id = (item.metadata_json or {}).get("source_artifact_id") if item else None
        if source_id:
            frontier.append(str(source_id))
    return ancestors
