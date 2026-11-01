"""Credential namespaces; namespace selection does not establish actor permission."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .accounts.models import User, Workspace
from .job_identity import job_creation_ownership


@dataclass(frozen=True)
class CredentialNamespace:
    workspace_id: str
    legacy_host_access: bool
    host_credentials_allowed: bool


def credential_namespace(session: Session, workspace_id: str | None = None) -> CredentialNamespace:
    """Resolve current SQL scope and restrict ambient host access to the solo seed.

    Other workspaces do not inherit host environment/keyring/file authority. The
    original namespace's identity can still be recognized when its owning user is
    disabled; explicit access to another active workspace remains independent.
    """
    with session.no_autoflush:
        owner = job_creation_ownership(session, workspace_id=workspace_id)
        legacy_workspaces = session.execute(
            select(Workspace.id, User.is_active, User.is_instance_admin)
            .join(User, Workspace.personal_owner_user_id == User.id)
            .where(Workspace.kind == "personal", User.legacy_subject == "owner")
        ).all()
        if len(legacy_workspaces) != 1:
            raise RuntimeError(
                "The original personal credential namespace is missing or ambiguous."
            )
        legacy_workspace = legacy_workspaces[0]
        legacy_host_access = owner.workspace_id == legacy_workspace.id
        return CredentialNamespace(
            owner.workspace_id,
            legacy_host_access,
            legacy_host_access
            and legacy_workspace.is_active
            and legacy_workspace.is_instance_admin,
        )


__all__ = ["CredentialNamespace", "credential_namespace"]
