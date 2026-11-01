"""Workspace authority for session bundle import admission."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessContext, AccessDenied
from pandrator.web.access.session_bundle_policy import require_bundle_import_creation_access
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.execution_actor import (
    HostActor,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.job_commands import enqueue_bundle_import_in_session
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_library_workspace_http import library as library  # noqa: F401
from tests.test_source_preparation_workspace_access import FULL_SCOPES, _actor, _project


def _source(f: Any, root: dict[str, Any], *, content: str = "Bundle source bytes."):
    path = f.services.paths.uploads / f"bundle-admission-{uuid4()}.txt"
    path.write_text(content, encoding="utf-8")
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path,
            kind="source",
            role="upload",
            session_id=root["id"],
        )
    return artifact, path


def _job_count(f: Any) -> int:
    with f.case.database.snapshot_session() as unit:
        return unit.scalar(select(func.count()).select_from(m.Job)) or 0


def _invalid_metadata(f: Any, artifact_id: str) -> None:
    with f.case.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE artifacts SET metadata_json = :value WHERE id = :id"),
            {"value": "not-json", "id": artifact_id},
        )


def test_none_short_circuits_and_host_uses_only_an_active_artifact_header(library):
    unit = Mock()
    with execution_actor_scope(None):
        assert require_bundle_import_creation_access(unit, "not-consulted") is None
    assert unit.mock_calls == []

    f, u = library, library.u
    root = _project(u.alice, u.ah, u.workspace_id)
    artifact, _path = _source(f, root)
    with execution_actor_scope(HostActor("pandrator-cli")):
        with f.case.database.snapshot_session() as unit:
            assert require_bundle_import_creation_access(unit, artifact.id) == u.workspace_id


def test_bundle_import_command_requires_exact_context_and_string_inputs(library):
    f, u = library, library.u
    root = _project(u.alice, u.ah, u.workspace_id)
    artifact, _path = _source(f, root)
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    context = AccessContext(actor)
    before = _job_count(f)
    with f.case.database.immediate_session() as unit:
        with pytest.raises(TypeError, match="An access context is required\\."):
            enqueue_bundle_import_in_session(
                unit,
                cast(Any, object()),
                f.services.jobs,
                source_artifact_id=artifact.id,
            )
        with pytest.raises(TypeError, match="source_artifact_id must be a string\\."):
            enqueue_bundle_import_in_session(
                unit,
                context,
                f.services.jobs,
                source_artifact_id=cast(Any, None),
            )
        with pytest.raises(TypeError, match="name must be a string or None\\."):
            enqueue_bundle_import_in_session(
                unit,
                context,
                f.services.jobs,
                source_artifact_id=artifact.id,
                name=cast(Any, 1),
            )
    assert _job_count(f) == before


def test_workspace_member_imports_without_artifact_body_and_queue_captures_actor(
    library, monkeypatch
):
    f, u = library, library.u
    root = _project(u.alice, u.ah, u.workspace_id)
    artifact, path = _source(f, root, content="Do not alter bundle bytes.")
    _invalid_metadata(f, artifact.id)
    before_bytes = path.read_bytes()
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    context = AccessContext(actor)
    with f.case.database.snapshot_session() as unit:
        assert (
            unit.scalar(
                select(WorkspaceMembership.role).where(
                    WorkspaceMembership.workspace_id == u.workspace_id,
                    WorkspaceMembership.user_id == u.alice_id,
                )
            )
            == "member"
        )
    forbidden = Mock(side_effect=AssertionError("Admission must not resolve source files"))
    monkeypatch.setattr(f.services.artifacts, "resolve", forbidden)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        with execution_actor_scope(None), f.case.database.immediate_session() as unit:
            job = enqueue_bundle_import_in_session(
                unit,
                context,
                f.services.jobs,
                source_artifact_id=artifact.id,
                name="Imported source",
            )
            job_id = job.id
            assert current_execution_actor.get() is None
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)

    forbidden.assert_not_called()
    assert path.read_bytes() == before_bytes
    assert any("select artifacts.workspace_id" in sql for sql in statements)
    assert not any("select" in sql and "artifacts.metadata_json" in sql for sql in statements)
    with f.case.database.snapshot_session() as unit:
        job = unit.get_one(m.Job, job_id)
        assert job.kind == "session.bundle.import"
        assert job.payload_json == {
            "source_artifact_id": artifact.id,
            "name": "Imported source",
        }
        assert job.session_id is None
        assert job.project_id is None
        assert job.workspace_id == u.workspace_id
        assert job.max_attempts == 1
        assert job.resource_keys_json == []
        assert unit.get_one(JobActorRecord, job_id).user_id == u.alice_id


@pytest.mark.parametrize(
    "scopes",
    [frozenset({"app.read", "app.run"}), frozenset({"app.read", "app.write"})],
)
def test_bundle_import_requires_both_write_and_run_scopes(library, scopes):
    f, u = library, library.u
    root = _project(u.alice, u.ah, u.workspace_id)
    artifact, _path = _source(f, root)
    actor = _actor(f.case, u.alice_id, scopes, workspaces=(u.workspace_id,))
    before = _job_count(f)
    with execution_actor_scope(HostActor("pandrator-cli")):
        with f.case.database.immediate_session() as unit:
            with pytest.raises(AccessDenied):
                enqueue_bundle_import_in_session(
                    unit,
                    AccessContext(actor),
                    f.services.jobs,
                    source_artifact_id=artifact.id,
                )
            assert current_execution_actor.get() == HostActor("pandrator-cli")
    assert _job_count(f) == before


def test_project_only_credential_cannot_import_workspace_source(library):
    f, u = library, library.u
    root = _project(u.alice, u.ah, u.workspace_id)
    artifact, path = _source(f, root)
    before_bytes = path.read_bytes()
    actor = _actor(
        f.case,
        u.alice_id,
        FULL_SCOPES,
        projects=(root["project_id"],),
    )
    before = _job_count(f)
    with f.case.database.immediate_session() as unit:
        with pytest.raises(KeyError, match="Bundle artifact not found\\."):
            enqueue_bundle_import_in_session(
                unit,
                AccessContext(actor),
                f.services.jobs,
                source_artifact_id=artifact.id,
            )
    assert _job_count(f) == before
    assert path.read_bytes() == before_bytes


@pytest.mark.parametrize("principal", ["foreign_member", "unrelated_admin"])
def test_foreign_private_source_is_opaque_and_never_enqueued(library, monkeypatch, principal):
    f, u = library, library.u
    root = _project(u.bob, u.bh)
    artifact, _path = _source(f, root)
    _invalid_metadata(f, artifact.id)
    if principal == "foreign_member":
        actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    else:
        owner_id = f.case.auth.initial_account().user_id
        actor = _actor(
            f.case,
            owner_id,
            FULL_SCOPES | {"app.admin"},
            workspaces=(f.homes[owner_id],),
        )
    enqueue = Mock(side_effect=AssertionError("Denied import reached the queue"))
    monkeypatch.setattr(f.services.jobs, "enqueue_in_session", enqueue)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    before = _job_count(f)
    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        with f.case.database.immediate_session() as unit:
            with pytest.raises(KeyError, match="Bundle artifact not found\\."):
                enqueue_bundle_import_in_session(
                    unit,
                    AccessContext(actor),
                    f.services.jobs,
                    source_artifact_id=artifact.id,
                )
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)
    enqueue.assert_not_called()
    assert _job_count(f) == before
    assert not any("select" in sql and "artifacts.metadata_json" in sql for sql in statements)


@pytest.mark.parametrize("revocation", ["membership", "workspace_credential"])
def test_import_rechecks_membership_and_resource_grants_after_actor_capture(library, revocation):
    f, u = library, library.u
    root = _project(u.alice, u.ah, u.workspace_id)
    artifact, path = _source(f, root)
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    context = AccessContext(actor)
    if revocation == "membership":
        with f.case.database.immediate_session() as unit:
            unit.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == u.workspace_id,
                    WorkspaceMembership.user_id == u.alice_id,
                )
            )
    else:
        with f.case.database.immediate_session() as unit:
            unit.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == actor.token_id,
                    ApiTokenWorkspaceGrant.workspace_id == u.workspace_id,
                )
            )
    before = _job_count(f)
    before_bytes = path.read_bytes()
    with f.case.database.immediate_session() as unit:
        with pytest.raises(KeyError, match="Bundle artifact not found\\."):
            enqueue_bundle_import_in_session(
                unit,
                context,
                f.services.jobs,
                source_artifact_id=artifact.id,
            )
    assert _job_count(f) == before
    assert path.read_bytes() == before_bytes


@pytest.mark.parametrize("failure", ["missing", "deleted", "archived_workspace"])
def test_missing_deleted_or_archived_bundle_headers_share_opaque_error(library, failure):
    f, u = library, library.u
    root = _project(u.alice, u.ah, u.workspace_id)
    artifact, path = _source(f, root)
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    if failure == "deleted":
        with f.case.database.immediate_session() as unit:
            unit.get_one(m.Artifact, artifact.id).state = "deleted"
    elif failure == "archived_workspace":
        with f.case.database.immediate_session() as unit:
            unit.get_one(Workspace, u.workspace_id).state = "archived"
    source_id = str(uuid4()) if failure == "missing" else artifact.id
    before = _job_count(f)
    before_bytes = path.read_bytes()
    with f.case.database.immediate_session() as unit:
        with pytest.raises(KeyError, match="Bundle artifact not found\\."):
            enqueue_bundle_import_in_session(
                unit,
                AccessContext(actor),
                f.services.jobs,
                source_artifact_id=source_id,
            )
    assert _job_count(f) == before
    assert path.read_bytes() == before_bytes
