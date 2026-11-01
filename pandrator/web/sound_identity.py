"""Persisted ownership checks for reusable sound-library records."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .accounts.identity import active_workspace, initial_personal_workspace
from .accounts.models import Workspace
from .models import Artifact, SoundItem, SoundVariant
from .projects.artifact_identity import resolve_artifact_ownership


def sound_creation_workspace(session: Session, *, workspace_id: str | None = None) -> Workspace:
    """Resolve an active requested sound owner or the validated initial workspace."""
    with session.no_autoflush:
        if workspace_id is None:
            return initial_personal_workspace(session)
        return active_workspace(session, workspace_id, "Requested sound workspace")


def _stored_workspace(
    session: Session,
    workspace_id: str | None,
    *,
    owner_kind: str,
    owner_id: str,
) -> Workspace:
    if workspace_id is None:
        raise ValueError(f"{owner_kind} {owner_id!r} has no stored workspace ownership.")
    return active_workspace(session, workspace_id, f"{owner_kind} {owner_id!r} ownership")


def _require_requested_workspace(
    session: Session,
    *,
    requested_workspace_id: str | None,
    stored_workspace: Workspace,
    owner_kind: str,
    owner_id: str,
) -> None:
    if requested_workspace_id is None:
        return
    requested = active_workspace(
        session, requested_workspace_id, f"Requested {owner_kind.lower()} workspace"
    )
    if requested.id != stored_workspace.id:
        raise ValueError(
            f"{owner_kind} {owner_id!r} belongs to workspace {stored_workspace.id!r}, "
            f"not requested workspace {requested.id!r}."
        )


def sound_item_workspace(
    session: Session,
    item: SoundItem,
    *,
    workspace_id: str | None = None,
) -> Workspace:
    """Validate a sound item's stored owner and preferred-variant link."""
    with session.no_autoflush:
        workspace = _stored_workspace(
            session,
            item.workspace_id,
            owner_kind="Sound item",
            owner_id=item.id,
        )
        _require_requested_workspace(
            session,
            requested_workspace_id=workspace_id,
            stored_workspace=workspace,
            owner_kind="Sound item",
            owner_id=item.id,
        )

        if item.preferred_variant_id is not None:
            preferred = session.get(SoundVariant, item.preferred_variant_id)
            if preferred is None:
                raise ValueError(
                    f"Sound item {item.id!r} references missing preferred variant "
                    f"{item.preferred_variant_id!r}."
                )
            if preferred.item_id != item.id:
                raise ValueError(
                    f"Preferred variant {preferred.id!r} does not belong to sound item {item.id!r}."
                )
            if preferred.workspace_id != workspace.id:
                raise ValueError(
                    f"Preferred variant {preferred.id!r} does not share sound item "
                    f"{item.id!r}'s workspace."
                )
        return workspace


def sound_variant_workspace(
    session: Session,
    variant: SoundVariant,
    *,
    workspace_id: str | None = None,
) -> Workspace:
    """Validate a variant's owner and any attached item or artifact owner."""
    with session.no_autoflush:
        workspace = _stored_workspace(
            session,
            variant.workspace_id,
            owner_kind="Sound variant",
            owner_id=variant.id,
        )
        _require_requested_workspace(
            session,
            requested_workspace_id=workspace_id,
            stored_workspace=workspace,
            owner_kind="Sound variant",
            owner_id=variant.id,
        )

        if variant.item_id is not None:
            item = session.get(SoundItem, variant.item_id)
            if item is None:
                raise ValueError(
                    f"Sound variant {variant.id!r} references missing item {variant.item_id!r}."
                )
            if item.workspace_id != workspace.id:
                raise ValueError(
                    f"Sound variant {variant.id!r} and item {item.id!r} have "
                    "different workspace ownership."
                )

        if variant.artifact_id is not None:
            artifact = session.get(Artifact, variant.artifact_id)
            if artifact is None:
                raise ValueError(
                    f"Sound variant {variant.id!r} references missing artifact "
                    f"{variant.artifact_id!r}."
                )
            artifact_owner = resolve_artifact_ownership(session, artifact=artifact)
            if artifact_owner.workspace_id != workspace.id:
                raise ValueError(
                    f"Sound variant {variant.id!r} and artifact {artifact.id!r} have "
                    "different workspace ownership."
                )

        return workspace


__all__ = [
    "sound_creation_workspace",
    "sound_item_workspace",
    "sound_variant_workspace",
]
