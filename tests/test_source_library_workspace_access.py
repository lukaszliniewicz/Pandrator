"""Disposable source-library namespaces, finite dependencies and fresh authority."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import delete, event, insert, select, text

from pandrator.web import subtitle_sources
from pandrator.web.access import source_library_policy as policy
from pandrator.web.access.contracts import AccessDenied, ProjectAction, WorkspaceNotFound
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.execution_actor import (
    HostActor,
    actor_to_storage,
    execution_actor_scope,
)
from pandrator.web.accounts.models import Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.job_execution import current_job_lease
from pandrator.web.models import (
    ApiToken,
    Artifact,
    Job,
    SessionRecord,
    SessionSource,
    SourceAsset,
    new_id,
    utcnow,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.models import Project, ProjectMembership
from pandrator.web.source_library import SourceLibraryService
from tests.test_job_access_policy import _context
from tests.test_job_access_policy import block_network_attempts as block_network_attempts
from tests.test_job_access_policy import case as case

READ = frozenset({"app.read"})
EDIT = frozenset({"app.read", "app.write"})


def _asset(case, workspace, *, owner_session=None, kind="text", name="Synthetic source"):
    with case.database.session() as unit:
        artifact = None
        if owner_session is not None:
            record = unit.get(SessionRecord, owner_session)
            assert record is not None
            artifact = Artifact(
                id=new_id(),
                workspace_id=workspace,
                project_id=record.project_id,
                session_id=owner_session,
                kind=kind,
                role="source",
                relative_path=f"artifacts/{new_id()}.txt",
                content_hash="a" * 64,
            )
            unit.add(artifact)
            unit.flush()
        asset = SourceAsset(
            id=new_id(),
            workspace_id=workspace,
            artifact_id=artifact.id if artifact else None,
            display_name=name,
            kind=kind,
            metadata_json={"private": name},
        )
        unit.add(asset)
        unit.flush()
        return asset.id


def _link(case, session_id, asset_id, *, role="primary", current=True):
    with case.database.session() as unit:
        link = SessionSource(
            session_id=session_id, source_asset_id=asset_id, role=role, is_current=current
        )
        unit.add(link)
        unit.flush()
        return link.id


@pytest.fixture
def sources(case):
    with case.database.session() as unit:
        unit.add(WorkspaceMembership(workspace_id=case.team, user_id=case.user, role="member"))
        unit.add(ProjectMembership(project_id=case.a, user_id=case.other, role="viewer"))
        target_a = unit.scalar(select(Project.source_session_id).where(Project.id == case.a))
        target_b = unit.scalar(select(Project.source_session_id).where(Project.id == case.b))
        foreign = Workspace(id=new_id(), kind="team", name="Foreign team", state="active")
        unit.add(foreign)
        unit.flush()
        unit.add(WorkspaceMembership(workspace_id=foreign.id, user_id=case.outsider, role="owner"))
        private_record = SessionRecord(id=new_id(), name="Foreign personal reference")
        create_session_project(unit, private_record, workspace_id=case.personal[case.outsider])
    personal = _asset(case, case.personal[case.user], name="Actual user personal source")
    other_personal = _asset(case, case.personal[case.owner], name="Other personal source")
    team = _asset(case, case.team, name="Team current source")
    historical = _asset(case, case.team, name="Team historical source")
    foreign_asset = _asset(case, foreign.id, name="Foreign team source")
    _link(case, target_a, team)
    _link(case, target_b, team)
    _link(case, target_a, historical, role="historical", current=False)
    _link(case, target_b, historical, role="historical", current=True)
    return SimpleNamespace(
        case=case,
        library=SourceLibraryService(case.database),
        a=target_a,
        b=target_b,
        personal=personal,
        other_personal=other_personal,
        team=team,
        historical=historical,
        foreign_workspace=foreign.id,
        foreign_asset=foreign_asset,
        private_session=private_record.id,
    )


def _member_context(s, *, scopes=READ, personal=True, team=True):
    workspaces = tuple(
        value
        for enabled, value in ((personal, s.case.personal[s.case.user]), (team, s.case.team))
        if enabled
    )
    return _context(s.case, s.case.user, scopes=scopes, workspaces=workspaces)


@pytest.mark.parametrize("view", ["full", "compact"])
def test_actual_personal_default_and_explicit_member_team_namespace(sources, view):
    s = sources
    with execution_actor_scope(_member_context(s).actor):
        personal = s.library.list(view=view)
        team = s.library.list(workspace_id=s.case.team, view=view)
    assert {item["id"] for item in personal} == {s.personal}
    assert {item["workspace_id"] for item in personal} == {s.case.personal[s.case.user]}
    assert {item["id"] for item in team} == {s.team, s.historical}
    assert all(item["workspace_id"] == s.case.team for item in team)


@pytest.mark.parametrize("admin", [False, True])
def test_foreign_personal_and_team_namespaces_do_not_gain_admin_wildcard(sources, admin):
    s = sources
    context = (
        _context(
            s.case,
            s.case.outsider,
            scopes=READ | {"app.admin"},
            workspaces=(s.case.personal[s.case.outsider],),
        )
        if admin
        else _member_context(s)
    )
    hidden = (
        (s.case.personal[s.case.user], s.case.team)
        if admin
        else (s.case.personal[s.case.owner], s.foreign_workspace)
    )
    with execution_actor_scope(context.actor):
        for workspace in hidden:
            with pytest.raises(WorkspaceNotFound):
                s.library.list(workspace_id=workspace)
        for identifier in (s.personal, s.team) if admin else (s.other_personal, s.foreign_asset):
            with s.case.database.session() as unit, pytest.raises(KeyError):
                policy.require_source_asset_access(unit, identifier)


def test_read_only_library_scope_cannot_rename_trash_publish_or_attach(sources):
    s = sources
    context = _member_context(s)
    with execution_actor_scope(context.actor):
        assert s.library.references(s.team)["total"] == 2
        for command in (
            lambda: s.library.rename(s.team, 1, "Changed"),
            lambda: s.library.set_state(s.personal, 1, "trashed"),
            lambda: s.library.attach(s.a, s.team),
        ):
            with pytest.raises(AccessDenied):
                command()
        with s.case.database.session() as unit:
            with pytest.raises(AccessDenied):
                policy.require_source_artifact_publication_access(unit, "missing")


def test_project_only_grant_has_finite_current_and_historical_session_listing(sources):
    s = sources
    context = _context(s.case, s.case.other, scopes=READ, projects=(s.case.a,))
    with execution_actor_scope(context.actor):
        full = {item["id"]: item for item in s.library.list(session_id=s.a)}
        compact = {item["id"]: item for item in s.library.list(session_id=s.a, view="compact")}
        assert set(full) == {s.team, s.historical}
        for identifier, item in compact.items():
            assert item == {key: full[identifier][key] for key in item}
        assert full[s.team]["reference_count"] == 1 and full[s.team]["current_reference_count"] == 1
        assert (
            full[s.historical]["reference_count"] == 1
            and full[s.historical]["current_reference_count"] == 0
        )
        with pytest.raises(WorkspaceNotFound):
            s.library.list(workspace_id=s.case.team)
        with pytest.raises(WorkspaceNotFound):
            s.library.list()
        with pytest.raises(KeyError):
            s.library.references(s.team)
        with pytest.raises(AccessDenied):
            s.library.attach(s.a, s.team)
        with s.case.database.session() as unit:
            policy.require_source_asset_access(unit, s.team, project_session_id=s.a)
            with pytest.raises(KeyError):
                policy.require_source_asset_access(unit, s.historical, project_session_id=s.a)
            with pytest.raises(KeyError):
                policy.require_source_asset_access(unit, s.foreign_asset, project_session_id=s.a)


def test_project_scoped_member_can_attach_owned_dependency_and_adopt_current_attachment(
    sources, monkeypatch
):
    s = sources
    owned = _asset(s.case, s.case.team, owner_session=s.a)
    context = _context(s.case, s.case.user, scopes=EDIT, projects=(s.case.a,))
    s.library.artifacts = MagicMock()
    adopted = Mock(return_value={"reused": True})
    monkeypatch.setattr(subtitle_sources, "adopt_subtitle_source_in_session", adopted)
    with execution_actor_scope(context.actor):
        assert s.library.attach(s.a, s.team, role="secondary")["source_asset_id"] == s.team
        result = s.library.attach(s.a, owned, role="primary")
        assert result["source_asset_id"] == owned
        assert s.library.adopt_subtitles(s.a, owned)["source_asset_id"] == owned
        with s.case.database.session() as unit:
            policy.require_source_asset_access(unit, s.team, project_session_id=s.a)
        with pytest.raises(KeyError):
            s.library.attach(s.a, s.foreign_asset, role="secondary")
        with pytest.raises(KeyError):
            s.library.attach(s.a, s.historical, role="secondary")
        with pytest.raises(KeyError):
            s.library.rename(owned, 1, "No library authority")
        with pytest.raises(WorkspaceNotFound):
            s.library.list(workspace_id=s.case.team)
    adopted.assert_called_once()


def test_publication_uses_current_edit_artifact_scope_before_body(sources):
    s = sources
    owned = _asset(s.case, s.case.team, owner_session=s.a)
    with s.case.database.session() as unit:
        artifact_id = unit.scalar(select(SourceAsset.artifact_id).where(SourceAsset.id == owned))
    context = _context(s.case, s.case.user, scopes=EDIT, projects=(s.case.a,))
    with execution_actor_scope(context.actor):
        assert s.library.ensure_for_artifact(artifact_id).id == owned
        with pytest.raises(KeyError):
            s.library.ensure_for_artifact("foreign-artifact")
    viewer = _context(s.case, s.case.other, scopes=EDIT, projects=(s.case.a,))
    with execution_actor_scope(viewer.actor), s.case.database.session() as unit:
        with pytest.raises(KeyError):
            policy.require_source_artifact_publication_access(unit, artifact_id)


def _corrupt_legacy_reference(s, asset_id):
    """Inject only disposable legacy inconsistency and restore its guard immediately."""
    trigger = "pandrator_media_scope_session_sources_insert"
    with s.case.database.session() as unit:
        definition = unit.scalar(
            text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": trigger},
        )
        assert definition is not None
        unit.connection().exec_driver_sql(f"DROP TRIGGER {trigger}")
        try:
            unit.execute(
                insert(SessionSource).values(
                    id=new_id(),
                    session_id=s.private_session,
                    source_asset_id=asset_id,
                    role="legacy",
                    is_current=True,
                )
            )
        finally:
            unit.connection().exec_driver_sql(definition)
        assert unit.connection().exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_reference_count_and_pagination_exclude_foreign_legacy_reference(sources):
    s = sources
    _corrupt_legacy_reference(s, s.team)
    context = _member_context(s, scopes=EDIT)
    with execution_actor_scope(context.actor):
        page = s.library.references(s.team, limit=1)
        assert page["total"] == 2 and page["next_offset"] == 1
        second = s.library.references(s.team, limit=1, offset=1)
        assert second["total"] == 2 and second["next_offset"] is None
        assert {item["session_id"] for item in page["items"] + second["items"]} == {s.a, s.b}
        full = {item["id"]: item for item in s.library.list(workspace_id=s.case.team)}
        compact = {
            item["id"]: item for item in s.library.list(workspace_id=s.case.team, view="compact")
        }
        assert full[s.team]["reference_count"] == compact[s.team]["reference_count"] == 2
        result = s.library.rename(s.team, 1, "Renamed")
        assert result["reference_count"] == 2 and result["current_reference_count"] == 2


def test_hidden_reference_still_blocks_trash_without_returning_its_count(sources):
    s = sources
    asset = _asset(s.case, s.case.team)
    _corrupt_legacy_reference(s, asset)
    with execution_actor_scope(_member_context(s, scopes=EDIT).actor):
        full = {item["id"]: item for item in s.library.list(workspace_id=s.case.team)}
        assert full[asset]["reference_count"] == 0
        with pytest.raises(ValueError):
            s.library.set_state(asset, 1, "trashed")
    with s.case.database.session() as unit:
        row = unit.get(SourceAsset, asset)
        assert row is not None and row.state == "current" and row.revision == 1


@pytest.mark.parametrize("revocation", ["token", "membership", "grant"])
def test_fresh_writer_checks_current_credential_membership_and_grant(sources, revocation):
    s = sources
    context = _member_context(s, scopes=EDIT)
    with s.case.database.session() as unit:
        if revocation == "token":
            token = unit.get(ApiToken, context.actor.token_id)
            assert token is not None
            token.revoked_at = utcnow()
        elif revocation == "membership":
            member = unit.get(WorkspaceMembership, (s.case.team, s.case.user))
            assert member is not None
            unit.delete(member)
        else:
            unit.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == context.actor.token_id,
                    ApiTokenWorkspaceGrant.workspace_id == s.case.team,
                )
            )
    with execution_actor_scope(context.actor), pytest.raises((AccessDenied, KeyError)):
        s.library.rename(s.team, 1, "Denied")


@contextmanager
def _leased(actor, lease):
    token = current_job_lease.set(lease)
    try:
        with execution_actor_scope(actor):
            yield
    finally:
        current_job_lease.reset(token)


@pytest.mark.parametrize("defect", [None, "revoked", "lease_owner", "saved_actor"])
def test_actual_leased_library_read_tolerates_expiry_but_not_revocation_or_lost_proof(
    sources, defect
):
    s = sources
    context = _member_context(s)
    lease = {"job_id": new_id(), "owner_id": "source-worker", "lease_generation": 2}
    stored = actor_to_storage(context.actor)
    assert stored is not None
    with s.case.database.session() as unit:
        unit.add(
            Job(
                id=lease["job_id"],
                kind="synthetic.source-library",
                workspace_id=s.case.team,
                project_id=s.case.a,
                status="running",
                payload_json={},
                lease_owner=lease["owner_id"],
                lease_generation=2,
                lease_expires_at=utcnow() + timedelta(hours=1),
            )
        )
        unit.flush()
        unit.add(JobActorRecord(job_id=lease["job_id"], **stored))
        token = unit.get(ApiToken, context.actor.token_id)
        assert token is not None
        token.expires_at = utcnow() - timedelta(minutes=1)
        if defect == "revoked":
            token.revoked_at = utcnow()
        elif defect == "saved_actor":
            unit.flush()
            actor = unit.get(JobActorRecord, lease["job_id"])
            assert actor is not None
            actor.auth_revision = 9
    if defect == "lease_owner":
        lease["owner_id"] = "wrong-worker"
    with _leased(context.actor, lease):
        if defect is None:
            assert {item["id"] for item in s.library.list(workspace_id=s.case.team)} == {
                s.team,
                s.historical,
            }
        else:
            with pytest.raises(AccessDenied):
                s.library.list(workspace_id=s.case.team)


@pytest.mark.parametrize("view", ["full", "compact"])
def test_listing_query_count_does_not_grow_with_one_vs_twenty_five_rows(sources, view):
    s = sources
    context = _member_context(s)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    def listing():
        statements.clear()
        event.listen(s.case.database.engine, "before_cursor_execute", capture)
        try:
            with execution_actor_scope(context.actor):
                result = s.library.list(view=view)
            return result, list(statements)
        finally:
            event.remove(s.case.database.engine, "before_cursor_execute", capture)

    one, initial = listing()
    assert len(one) == 1
    for _ in range(24):
        _asset(s.case, s.case.personal[s.case.user])
    many, later = listing()
    assert len(many) == 25 and len(initial) == len(later)
    assert any("WHERE source_assets.workspace_id" in statement for statement in later)


def test_none_leaf_compatibility_and_explicit_trusted_workspace_filter(sources):
    unit: Any = MagicMock()
    with execution_actor_scope(None):
        assert policy.require_source_library_workspace(unit, ProjectAction.READ) is None
        assert (
            policy.require_source_library_workspace(
                unit, ProjectAction.EDIT, workspace_id="explicit"
            )
            == "explicit"
        )
        policy.require_source_asset_access(unit, "asset")
        policy.require_source_artifact_publication_access(unit, "artifact")
        policy.require_source_backfill_access(unit)
        policy.source_reference_visibility_clause()
        assert {
            item["id"]
            for item in sources.library.list(workspace_id=sources.case.personal[sources.case.user])
        } == {sources.personal}
    assert unit.mock_calls == []


def test_host_proof_and_user_backfill_denial_before_global_reads(sources):
    unit: Any = MagicMock()
    with execution_actor_scope(HostActor("pandrator-cli")):
        assert (
            policy.require_source_library_workspace(
                unit, ProjectAction.READ, workspace_id="explicit"
            )
            == "explicit"
        )
        policy.require_source_asset_access(unit, "asset")
        policy.require_source_artifact_publication_access(unit, "artifact")
        policy.require_source_backfill_access(unit)
    unit.scalar.assert_not_called()
    unit.execute.assert_not_called()
    database: Any = SimpleNamespace(session=lambda: nullcontext(unit))
    service = SourceLibraryService(database)
    with execution_actor_scope(_member_context(sources).actor), pytest.raises(AccessDenied):
        service.backfill_legacy()
    unit.scalars.assert_not_called()
