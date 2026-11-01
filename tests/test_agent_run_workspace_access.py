"""Current project authority for durable agent-run management."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text

from pandrator.web import models as m
from pandrator.web.access.agent_run_policy import require_current_agent_run_access
from pandrator.web.access.contracts import AccessDenied, ProjectAction
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.agentic_runs import AgenticRunStore
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_library_workspace_http import library as library  # noqa: F401
from tests.test_source_preparation_workspace_access import (
    FULL_SCOPES,
    _actor,
    _attach,
    _detach_current_source,
    _project,
)


def _source(f: Any, root: dict[str, Any], *, content: str = "Original source bytes."):
    path = f.services.paths.uploads / f"agent-run-{uuid4()}.txt"
    path.write_text(content, encoding="utf-8")
    with execution_actor_scope(HostActor("pandrator-cli")):
        source = f.services.artifacts.register(
            path,
            kind="source",
            role="upload",
            session_id=root["id"],
        )
    return source, path


def _create_run_job(
    f: Any,
    root: dict[str, Any],
    source: Any,
) -> tuple[str, str]:
    with f.case.database.immediate_session() as unit:
        job = f.services.jobs.enqueue_in_session(
            unit,
            "source.clean",
            {"session_id": root["id"], "payload_probe": "do not load"},
            project_id=root["project_id"],
            resource_keys=[f"session:{root['id']}", "service:llm"],
        )
        job.status = "failed"
        run = m.AgentRun(
            kind="source_cleaning",
            session_id=root["id"],
            source_artifact_id=source.id,
            status="failed",
            source_content_hash=str(source.content_hash or ""),
            job_id=job.id,
            settings_json={},
        )
        unit.add(run)
        unit.flush()
        return run.id, job.id


def _counts(f: Any) -> tuple[int, int]:
    with f.case.database.snapshot_session() as unit:
        return (
            unit.scalar(select(func.count()).select_from(m.AgentRun)) or 0,
            unit.scalar(select(func.count()).select_from(m.Job)) or 0,
        )


def _set_invalid_metadata(f: Any, artifact_id: str) -> None:
    with f.case.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE artifacts SET metadata_json = :invalid WHERE id = :id"),
            {"invalid": "not-json", "id": artifact_id},
        )


def test_run_policy_uses_saved_root_and_only_reads_the_run_header(library):
    f, u = library, library.u
    root = _project(u.alice, u.ah, u.workspace_id)
    with f.case.database.immediate_session() as unit:
        run = m.AgentRun(session_id=root["id"], status="failed")
        unit.add(run)
        unit.flush()
        run_id = run.id
    reader = _actor(
        f.case,
        u.alice_id,
        frozenset({"app.read"}),
        projects=(root["project_id"],),
    )
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        with execution_actor_scope(reader):
            with f.case.database.snapshot_session() as unit:
                require_current_agent_run_access(unit, run_id, ProjectAction.READ)
            for action in (ProjectAction.RUN, ProjectAction.EDIT):
                with (
                    f.case.database.snapshot_session() as unit,
                    pytest.raises(AccessDenied),
                ):
                    require_current_agent_run_access(unit, run_id, action)
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)

    run_reads = [sql for sql in statements if "agent_runs" in sql]
    assert any("select agent_runs.session_id" in sql for sql in run_reads)
    assert all(
        sql.split("from", 1)[0].strip() == "select agent_runs.session_id" for sql in run_reads
    )
    assert not any("agent_steps" in sql for sql in statements)


@pytest.mark.parametrize("principal", ["foreign_member", "unrelated_admin"])
def test_run_policy_hides_foreign_private_roots_even_from_unrelated_admin(library, principal):
    f, u = library, library.u
    target = _project(u.bob, u.bh)
    with f.case.database.immediate_session() as unit:
        run = m.AgentRun(session_id=target["id"], status="failed")
        unit.add(run)
        unit.flush()
        run_id = run.id
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
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        with execution_actor_scope(actor), f.case.database.snapshot_session() as unit:
            with pytest.raises(SessionNotFound, match="Session not found\\."):
                require_current_agent_run_access(unit, run_id, ProjectAction.READ)
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)
    assert any("select agent_runs.session_id" in sql for sql in statements)
    assert all(
        sql.split("from", 1)[0].strip() == "select agent_runs.session_id"
        for sql in statements
        if "agent_runs" in sql
    )
    assert not any("agent_steps" in sql for sql in statements)


def test_run_policy_missing_root_is_opaque_and_none_has_no_sql(library):
    unit = Mock()
    with execution_actor_scope(None):
        require_current_agent_run_access(unit, "unused")
        assert unit.mock_calls == []
        with pytest.raises(TypeError, match="valid project action"):
            require_current_agent_run_access(unit, "unused", "read")  # type: ignore[arg-type]
        assert unit.mock_calls == []

    f, u = library, library.u
    actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    with execution_actor_scope(actor), f.case.database.snapshot_session() as unit:
        with pytest.raises(KeyError, match="Agentic operation not found\\."):
            require_current_agent_run_access(unit, "missing-agent-run")


@pytest.mark.parametrize("denial", ["foreign_target", "foreign_input"])
def test_agent_run_create_denies_foreign_root_or_source_before_path_or_resolver(
    library, monkeypatch, denial
):
    f, u = library, library.u
    if denial == "foreign_target":
        target = _project(u.bob, u.bh)
        source, _path = _source(f, target)
        _attach(f, target["id"], source)
        actor = _actor(f.case, u.alice_id, FULL_SCOPES, workspaces=(u.workspace_id,))
    else:
        origin = _project(u.bob, u.bh)
        target = _project(u.alice, u.ah, u.workspace_id)
        source, _path = _source(f, origin)
        actor = _actor(
            f.case,
            u.alice_id,
            FULL_SCOPES,
            projects=(target["project_id"],),
        )

    before = _counts(f)
    forbidden = Mock(side_effect=AssertionError("Denied source reached artifact path access"))
    monkeypatch.setattr(f.services.artifacts, "resolve", forbidden)
    monkeypatch.setattr(type(f.services.artifacts.paths), "managed_path", forbidden)
    with execution_actor_scope(actor):
        if denial == "foreign_target":
            with pytest.raises(SessionNotFound):
                f.services.agent_runs.create(target["id"], source.id, {})
        else:
            with pytest.raises(KeyError, match="Source artifact not found\\."):
                f.services.agent_runs.create(target["id"], source.id, {})
    forbidden.assert_not_called()
    assert _counts(f) == before


def test_attached_same_workspace_dependency_is_admitted_without_metadata_or_resolver(
    library, monkeypatch
):
    f, u = library, library.u
    origin = _project(u.bob, u.bh, u.workspace_id)
    target = _project(u.alice, u.ah, u.workspace_id)
    source, path = _source(f, origin, content="Keep every original byte.")
    _attach(f, target["id"], source)
    _set_invalid_metadata(f, source.id)
    actor = _actor(
        f.case,
        u.alice_id,
        frozenset({"app.read", "app.run"}),
        projects=(target["project_id"],),
    )
    resolver = Mock(side_effect=AssertionError("Actor path must use the finite header"))
    monkeypatch.setattr(f.services.artifacts, "resolve", resolver)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        with execution_actor_scope(actor):
            result = f.services.agent_runs.create(target["id"], source.id, {"phase": "review"})
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)

    resolver.assert_not_called()
    assert result["status"] == "queued"
    assert path.read_bytes() == b"Keep every original byte."
    assert not any("select" in sql and "artifacts.metadata_json" in sql for sql in statements)
    with f.case.database.snapshot_session() as unit:
        run = unit.get_one(m.AgentRun, result["id"])
        assert run.source_content_hash == source.content_hash
        assert unit.get_one(m.Job, result["job_id"]).kind == "source.clean"


@pytest.mark.parametrize("loss", ["detached_input", "membership"])
def test_loss_between_snapshot_and_writer_denies_without_new_rows(library, monkeypatch, loss):
    f, u = library, library.u
    origin = _project(u.bob, u.bh, u.workspace_id)
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _source(f, origin)
    asset = _attach(f, target["id"], source)
    actor = _actor(
        f.case,
        u.alice_id,
        FULL_SCOPES,
        projects=(target["project_id"],),
    )
    paths = f.services.artifacts.paths
    paths_type = type(paths)
    original_managed_path = paths_type.managed_path
    calls = 0

    def detach_after_initial_proof(current_paths, relative_path: str):
        nonlocal calls
        if current_paths is not paths:
            return original_managed_path(current_paths, relative_path)
        calls += 1
        if loss == "detached_input":
            _detach_current_source(f, target["id"], asset.id)
        else:
            with f.case.database.immediate_session() as unit:
                unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))
        return original_managed_path(current_paths, relative_path)

    monkeypatch.setattr(paths_type, "managed_path", detach_after_initial_proof)
    before = _counts(f)
    with execution_actor_scope(actor):
        expected = (
            "Source artifact not found\\." if loss == "detached_input" else "Session not found\\."
        )
        with pytest.raises(KeyError, match=expected):
            f.services.agent_runs.create(target["id"], source.id, {})

    assert calls == 1
    assert _counts(f) == before
    assert _path.read_text() == "Original source bytes."


def test_workspace_member_can_resume_as_self_and_accept_without_run_scope(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _source(f, target)
    _attach(f, target["id"], source)
    alice = _actor(
        f.case,
        u.alice_id,
        frozenset({"app.read", "app.run"}),
        workspaces=(u.workspace_id,),
    )
    with execution_actor_scope(alice):
        created = f.services.agent_runs.create(target["id"], source.id, {})
    with f.case.database.immediate_session() as unit:
        unit.get_one(m.AgentRun, created["id"]).status = "failed"
        unit.get_one(m.Job, created["job_id"]).status = "failed"
        unit.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == u.workspace_id,
                WorkspaceMembership.user_id == u.alice_id,
            )
        )

    bob_run = _actor(
        f.case,
        u.bob_id,
        frozenset({"app.read", "app.run"}),
        workspaces=(u.workspace_id,),
    )
    bob_edit = _actor(
        f.case,
        u.bob_id,
        frozenset({"app.read", "app.write"}),
        workspaces=(u.workspace_id,),
    )
    before = _counts(f)
    with execution_actor_scope(bob_edit):
        with pytest.raises(AccessDenied):
            f.services.agent_runs.resume(created["id"])
    assert _counts(f) == before

    with execution_actor_scope(bob_run):
        with f.case.database.snapshot_session() as unit:
            require_current_agent_run_access(unit, created["id"], ProjectAction.READ)
        resumed = f.services.agent_runs.resume(created["id"])
    assert resumed["status"] == "retrying"
    assert resumed["job_id"] != created["job_id"]
    with f.case.database.immediate_session() as unit:
        run = unit.get_one(m.AgentRun, created["id"])
        run.status = "completed"
        run.result_artifact_id = source.id
        new_job_actor = unit.get_one(JobActorRecord, resumed["job_id"])
        assert new_job_actor.user_id == u.bob_id

    with execution_actor_scope(bob_edit):
        accepted = f.services.agent_runs.accept(created["id"])
    assert accepted["status"] == "accepted"
    assert accepted["result_artifact_id"] == source.id


def test_resume_rejects_mismatched_job_root_before_job_body_or_run_claim(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _source(f, target)
    # The database already rejects a different project's job. A legacy job
    # scoped only to this project passes that guard, but is not a session job.
    run_id, job_id = _create_run_job(f, target, source)
    _set_invalid_metadata(f, source.id)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    before = _counts(f)
    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        with f.case.database.immediate_session() as unit:
            with pytest.raises(
                ValueError,
                match="The original job is unavailable; start the stage again\\.",
            ):
                AgenticRunStore(f.case.database).prepare_resume_in_session(unit, run_id)
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)

    job_selects = [sql for sql in statements if "jobs" in sql]
    assert any("select jobs.session_id" in sql for sql in job_selects)
    assert not any("jobs.payload_json" in sql for sql in job_selects)
    assert not any("select" in sql and "artifacts.metadata_json" in sql for sql in statements)
    assert _counts(f) == before
    with f.case.database.snapshot_session() as unit:
        assert unit.get_one(m.AgentRun, run_id).status == "failed"
        job = unit.get_one(m.Job, job_id)
        assert job.session_id is None and job.project_id == target["project_id"]
