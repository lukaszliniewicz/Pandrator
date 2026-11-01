"""Real-database checks for passive artifact and usage access policy."""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pandrator.web.access.artifact_policy import (
    ArtifactNotFound,
    require_artifact_access,
    visible_artifact_ids_statement,
    visible_usage_event_ids_statement,
)
from pandrator.web.access.contracts import AccessContext, AccessDenied
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import actor_from_principal
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
    AutomationClientProjectGrant,
)
from pandrator.web.model_base import new_id
from pandrator.web.models import Artifact, AutomationClient, UsageEvent
from pandrator.web.projects.models import ProjectMembership
from tests.test_job_access_routes import SCOPES
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import credential as route_credential
from tests.test_job_access_routes import no_network as no_network


class _Unset:
    __slots__ = ()


_UNSET = _Unset()


def _context(
    case: Any,
    name: str,
    *,
    scopes: frozenset[str] = SCOPES,
    grant_team: bool = True,
) -> Any:
    return route_credential(case, name, scopes=scopes, grant_team=grant_team)


def _session_id(project_record: Any, requested: str | None | _Unset) -> str | None:
    if isinstance(requested, _Unset):
        return project_record.id if project_record is not None else None
    return requested


def _artifact(
    case: Any,
    *,
    project: str | None = "a",
    workspace_id: str | None = None,
    session_id: str | None | _Unset = _UNSET,
    state: str = "current",
) -> str:
    identifier = new_id()
    project_record = getattr(case, project) if project is not None else None
    actual_session_id = _session_id(project_record, session_id)
    row = Artifact(
        id=identifier,
        workspace_id=workspace_id or case.team,
        project_id=project_record.project_id if project_record is not None else None,
        session_id=actual_session_id,
        kind="synthetic.policy-test",
        role="artifact",
        relative_path=f"artifact-policy/{identifier}.bin",
        state=state,
        metadata_json={},
    )
    with case.database.immediate_session() as session:
        session.add(row)
        session.flush()
    return identifier


def _usage_event(
    case: Any,
    *,
    project: str | None = "a",
    workspace_id: str | None = None,
    session_id: str | None | _Unset = _UNSET,
    artifact_id: str | None = None,
) -> str:
    identifier = new_id()
    project_record = getattr(case, project) if project is not None else None
    actual_session_id = _session_id(project_record, session_id)
    row = UsageEvent(
        id=identifier,
        workspace_id=workspace_id or case.team,
        project_id=project_record.project_id if project_record is not None else None,
        session_id=actual_session_id,
        artifact_id=artifact_id,
        provider_key="synthetic",
        model_id="policy-test",
        raw_usage_json={"sentinel": "must-not-grant-authority"},
    )
    with case.database.immediate_session() as session:
        session.add(row)
        session.flush()
    return identifier


def _ids(session: Session, context: AccessContext) -> set[str]:
    return set(session.scalars(visible_artifact_ids_statement(session, context)))


@pytest.mark.parametrize("name", ("viewer", "editor", "owner"))
def test_project_viewer_editor_and_workspace_owner_can_read_project_artifacts(case, name):
    if name in {"viewer", "editor"}:
        with case.database.immediate_session() as session:
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == case.users[name],
                )
            )
    actor = _context(case, name)
    identifier = _artifact(case, project="a")

    with case.database.snapshot_session() as session:
        assert identifier in _ids(session, AccessContext(actor.actor))
        assert (
            require_artifact_access(session, AccessContext(actor.actor), identifier).id
            == identifier
        )


def test_team_rootless_reads_require_workspace_owner_or_admin(case):
    with case.database.immediate_session() as session:
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == case.team,
                WorkspaceMembership.user_id == case.users["editor"],
            )
        )
    rootless_id = _artifact(case, project=None)
    editor = _context(case, "editor")
    owner = _context(case, "owner")

    with case.database.snapshot_session() as session:
        assert rootless_id not in _ids(session, AccessContext(editor.actor))
        with pytest.raises(ArtifactNotFound):
            require_artifact_access(session, AccessContext(editor.actor), rootless_id)
        assert rootless_id in _ids(session, AccessContext(owner.actor))

    with case.database.immediate_session() as session:
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == case.team,
                WorkspaceMembership.user_id == case.users["viewer"],
            )
        )
        session.add(
            WorkspaceMembership(
                workspace_id=case.team,
                user_id=case.users["viewer"],
                role="admin",
            )
        )
    admin = _context(case, "viewer")
    with case.database.snapshot_session() as session:
        assert rootless_id in _ids(session, AccessContext(admin.actor))


def test_overbroad_instance_admin_credential_does_not_bypass_project_or_rootless_roots(case):
    admin = _context(case, "outsider", scopes=SCOPES | {"app.admin"})
    project_id = _artifact(case, project="a")
    rootless_id = _artifact(case, project=None)

    with case.database.snapshot_session() as session:
        context = AccessContext(admin.actor)
        assert project_id not in _ids(session, context)
        assert rootless_id not in _ids(session, context)
        hidden_key = session.identity_key(Artifact, (project_id,))
        with pytest.raises(ArtifactNotFound):
            require_artifact_access(session, context, project_id)
        assert hidden_key not in session.identity_map


def test_project_only_credential_ceiling_does_not_grant_rootless_artifacts(case):
    scoped = _context(case, "editor", grant_team=False)
    project_id = _artifact(case, project="a")
    rootless_id = _artifact(case, project=None)
    with case.database.immediate_session() as session:
        session.execute(
            delete(ApiTokenWorkspaceGrant).where(ApiTokenWorkspaceGrant.token_id == scoped.token.id)
        )
        session.add(
            ApiTokenProjectGrant(
                token_id=scoped.token.id,
                project_id=case.a.project_id,
            )
        )

    with case.database.snapshot_session() as session:
        context = AccessContext(scoped.actor)
        assert _ids(session, context) == {project_id}
        with pytest.raises(ArtifactNotFound):
            require_artifact_access(session, context, rootless_id)


def test_actual_noninitial_personal_owner_can_read_rootless_artifacts(case):
    owner_id = case.users["editor"]
    workspace_id = case.personal[owner_id]
    personal_id = _artifact(case, project=None, workspace_id=workspace_id)
    credential = _context(case, "editor", grant_team=False)

    with case.database.snapshot_session() as session:
        workspace = session.get(Workspace, workspace_id)
        membership = session.get(WorkspaceMembership, (workspace_id, owner_id))
        assert workspace is not None and workspace.personal_owner_user_id == owner_id
        assert membership is not None and membership.role == "owner"
        assert personal_id in _ids(session, AccessContext(credential.actor))


@pytest.mark.parametrize("revocation", ("project-membership", "workspace-archive"))
def test_current_project_membership_and_workspace_state_are_rechecked(case, revocation):
    project_id = _artifact(case, project="a")
    actor_name = "editor" if revocation == "project-membership" else "owner"
    if revocation == "project-membership":
        with case.database.immediate_session() as session:
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == case.users[actor_name],
                )
            )
    actor = _context(case, actor_name)
    context = AccessContext(actor.actor)

    with case.database.snapshot_session() as session:
        assert project_id in _ids(session, context)

    with case.database.immediate_session() as session:
        if revocation == "project-membership":
            session.execute(
                delete(ProjectMembership).where(
                    ProjectMembership.project_id == case.a.project_id,
                    ProjectMembership.user_id == case.users["editor"],
                )
            )
        else:
            session.execute(
                update(Workspace).where(Workspace.id == case.team).values(state="archived")
            )

    with case.database.snapshot_session() as session:
        assert project_id not in _ids(session, context)
        with pytest.raises(ArtifactNotFound):
            require_artifact_access(session, context, project_id)


@pytest.mark.parametrize("invalid_actor", ("missing-app-read", "disabled-user"))
def test_fresh_actor_must_have_app_read_and_active_user(case, invalid_actor):
    scopes = frozenset({"app.write"}) if invalid_actor == "missing-app-read" else SCOPES
    actor = _context(case, "editor", scopes=scopes)
    if invalid_actor == "disabled-user":
        with case.database.immediate_session() as session:
            session.execute(
                update(User).where(User.id == case.users["editor"]).values(is_active=False)
            )

    with case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            visible_artifact_ids_statement(session, AccessContext(actor.actor))


def test_statement_builder_requires_the_exact_access_context_type(case):
    with case.database.snapshot_session() as session:
        with pytest.raises(TypeError):
            visible_artifact_ids_statement(session, cast(AccessContext, object()))


def test_deleted_lifecycle_state_does_not_remove_authorized_history(case):
    actor = _context(case, "editor")
    identifier = _artifact(case, project="a", state="deleted")

    with case.database.snapshot_session() as session:
        result = require_artifact_access(session, AccessContext(actor.actor), identifier)
        assert result.id == identifier
        assert result.state == "deleted"


def test_expected_session_mismatch_is_indistinguishable_from_missing(case):
    actor = _context(case, "editor")
    identifier = _artifact(case, project="a")

    with case.database.snapshot_session() as session:
        assert (
            require_artifact_access(
                session,
                AccessContext(actor.actor),
                identifier,
                expected_session_id=case.a.id,
            ).id
            == identifier
        )
        with pytest.raises(ArtifactNotFound) as denied:
            require_artifact_access(
                session,
                AccessContext(actor.actor),
                identifier,
                expected_session_id=case.b.id,
            )
        assert denied.value.args == ("Artifact not found.",)


def test_session_pointer_must_resolve_to_the_same_project(case):
    actor = _context(case, "editor")
    matching_id = _artifact(case, project="a", session_id=case.a.id)
    project_root_id = _artifact(case, project="a", session_id=None)
    # Mandatory ownership triggers prevent storing contradictory scope at all.
    with pytest.raises(IntegrityError):
        _artifact(case, project="a", session_id=case.b.id)
    with pytest.raises(IntegrityError):
        _artifact(case, project="a", workspace_id=case.personal[case.users["editor"]])

    with case.database.snapshot_session() as session:
        assert matching_id in _ids(session, AccessContext(actor.actor))
        assert project_root_id in _ids(session, AccessContext(actor.actor))
        assert _ids(session, AccessContext(actor.actor)) == {matching_id, project_root_id}


def test_same_session_identity_map_is_refreshed_and_revocation_hides_preloaded_artifact(case):
    actor = _context(case, "editor")
    identifier = _artifact(case, project="a")

    with case.database.immediate_session() as session:
        cached = session.get(Artifact, identifier)
        assert cached is not None
        session.execute(
            update(Artifact).where(Artifact.id == identifier).values(state="deleted"),
            execution_options={"synchronize_session": False},
        )
        refreshed = require_artifact_access(session, AccessContext(actor.actor), identifier)
        assert refreshed is cached
        assert refreshed.state == "deleted"

        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == case.a.project_id,
                ProjectMembership.user_id == case.users["editor"],
            ),
            execution_options={"synchronize_session": False},
        )
        with pytest.raises(ArtifactNotFound):
            require_artifact_access(session, AccessContext(actor.actor), identifier)


def test_require_access_does_not_autoflush_a_pending_artifact(case):
    actor = _context(case, "editor")
    identifier = new_id()
    pending = Artifact(
        id=identifier,
        workspace_id=case.team,
        project_id=case.a.project_id,
        session_id=case.a.id,
        kind="synthetic.policy-test",
        role="artifact",
        relative_path=f"artifact-policy/{identifier}.bin",
        metadata_json={},
    )

    with case.database.immediate_session() as session:
        session.add(pending)
        with pytest.raises(ArtifactNotFound):
            require_artifact_access(session, AccessContext(actor.actor), identifier)
        assert pending in session.new


def test_usage_event_visibility_is_sql_scoped_before_payload_aggregation(case):
    editor = _context(case, "editor")
    owner = _context(case, "owner")
    visible_artifact_id = _artifact(case, project="a")
    visible_id = _usage_event(case, project="a", artifact_id=visible_artifact_id)
    hidden_project_id = _usage_event(case, project="b")
    hidden_rootless_id = _usage_event(case, project=None)

    with case.database.snapshot_session() as session:
        editor_ids = set(
            session.scalars(visible_usage_event_ids_statement(session, AccessContext(editor.actor)))
        )
        assert editor_ids == {visible_id}
        owner_ids = set(
            session.scalars(visible_usage_event_ids_statement(session, AccessContext(owner.actor)))
        )
        assert owner_ids == {visible_id, hidden_project_id, hidden_rootless_id}


def test_current_client_project_grant_narrows_unchanged_token_snapshot(case):
    client_id = new_id()
    with case.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="Synthetic artifact policy client",
                subject=client_id,
                redirect_uris_json=["http://localhost/callback"],
                allowed_scopes_json=sorted(SCOPES),
                target_instance_id="synthetic",
                canonical_origin="http://localhost",
                created_by="artifact-policy-test",
                user_id=case.users["editor"],
                user_auth_revision=1,
            )
        )
        session.flush()
        session.add(
            AutomationClientProjectGrant(
                client_id=client_id,
                project_id=case.a.project_id,
            )
        )

    _token, raw = case.auth.create_api_token(
        "Synthetic artifact policy client token",
        user_id=case.users["editor"],
        auth_revision=1,
        scopes=SCOPES,
        client_id=client_id,
        resource_grants=CredentialResources(project_ids=frozenset({case.a.project_id})),
    )
    principal = case.auth.resolve_api_token(
        raw,
        network_zone="loopback",
        target_instance_id=None,
    )
    assert principal is not None
    actor = actor_from_principal(principal)
    assert actor is not None and actor.client_id == client_id
    context = AccessContext(actor)
    visible_id = _artifact(case, project="a")
    other_id = _artifact(case, project="b")

    with case.database.snapshot_session() as session:
        assert _ids(session, context) == {visible_id}

    with case.database.immediate_session() as session:
        session.execute(
            delete(AutomationClientProjectGrant).where(
                AutomationClientProjectGrant.client_id == client_id
            )
        )
        session.add(
            AutomationClientProjectGrant(
                client_id=client_id,
                project_id=case.b.project_id,
            )
        )

    with case.database.snapshot_session() as session:
        assert _ids(session, context) == set()
        with pytest.raises(ArtifactNotFound):
            require_artifact_access(session, context, visible_id)
        with pytest.raises(ArtifactNotFound):
            require_artifact_access(session, context, other_id)
