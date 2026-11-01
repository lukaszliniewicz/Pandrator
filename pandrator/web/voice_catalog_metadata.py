"""Workspace-owned annotations for stable provider catalogue references."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .models import VoiceCatalogOverride, utcnow
from .settings_policy import RevisionConflict
from .voice_catalog_schemas import CatalogVoiceUpdate, VoiceReference, canonical_voice_key
from .voice_identity import voice_creation_workspace
from .voice_library import validate_profile_evidence


def _validate_stored_references(rows: Sequence[VoiceCatalogOverride], workspace_id: str) -> None:
    for row in rows:
        reference = VoiceReference.model_validate(row.voice_ref_json)
        if row.workspace_id != workspace_id or canonical_voice_key(reference) != row.voice_key:
            raise ValueError(
                "The stored voice catalogue annotation has inconsistent ownership or reference."
            )
        # Historical managed overrides are retained annotations, ignored by
        # catalogue assembly. Their JSON does not confer Voice access and may
        # legitimately outlive that Voice, just like profile evidence history.


def catalog_overrides(
    session: Session, *, workspace_id: str | None = None
) -> dict[str, dict[str, Any]]:
    """Build annotation payloads after validating every selected stored reference."""
    with session.no_autoflush:
        workspace = voice_creation_workspace(session, workspace_id=workspace_id)
        rows = session.scalars(
            select(VoiceCatalogOverride)
            .where(VoiceCatalogOverride.workspace_id == workspace.id)
            .order_by(VoiceCatalogOverride.voice_key)
        ).all()
        _validate_stored_references(rows, workspace.id)
        return {
            row.voice_key: {
                "name": row.name,
                "description": row.description,
                "voice_category": row.voice_category,
                "profile": deepcopy(row.profile_json),
                "revision": row.revision,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in rows
        }


def update_catalog_metadata(
    session: Session,
    paths: DataPaths,
    payload: CatalogVoiceUpdate | Mapping[str, Any],
    *,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Validate before mutating inside the caller's short write transaction.

    The adapter separately checks provider catalogue membership and actor policy.
    This boundary establishes ownership and failed-command atomicity only.
    """
    validated = (
        payload
        if isinstance(payload, CatalogVoiceUpdate)
        else CatalogVoiceUpdate.model_validate(payload)
    )
    if validated.reference.kind != "provider":
        raise ValueError("Use the managed voice endpoint to edit a saved reference voice.")
    key = canonical_voice_key(validated.reference)
    changes = validated.changes.model_dump(mode="json", exclude_unset=True)
    with session.no_autoflush:
        workspace = voice_creation_workspace(session, workspace_id=workspace_id)
        row = session.get(VoiceCatalogOverride, (workspace.id, key))
        if row is not None:
            _validate_stored_references([row], workspace.id)
        if (row.revision if row else 0) != validated.expected_revision:
            raise RevisionConflict("The voice metadata changed in another client.")
        validate_profile_evidence(session, paths, changes.get("profile"), workspace_id=workspace.id)

    if row is None:
        row = VoiceCatalogOverride(
            workspace_id=workspace.id,
            voice_key=key,
            voice_ref_json=validated.reference.model_dump(exclude_none=True),
            revision=0,
        )
        session.add(row)
    if (
        "voice_category" in changes
        and changes["voice_category"] != row.voice_category
        and "profile" not in changes
        and row.profile_json
    ):
        profile = deepcopy(row.profile_json)
        evidence = dict(profile.get("evidence") or {})
        evidence.pop("voice_category", None)
        profile["evidence"] = evidence
        row.profile_json = profile
    for name, value in changes.items():
        setattr(
            row,
            "profile_json" if name == "profile" else name,
            value.strip() if name == "name" and value else value,
        )
    row.revision += 1
    row.updated_at = utcnow()
    session.flush()
    return {
        "key": key,
        "reference": deepcopy(row.voice_ref_json),
        "revision": row.revision,
        "name": row.name,
        "description": row.description,
        "voice_category": row.voice_category,
        "profile": deepcopy(row.profile_json),
    }


__all__ = ["catalog_overrides", "update_catalog_metadata"]
