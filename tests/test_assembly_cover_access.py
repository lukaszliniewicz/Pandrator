"""Cover headers require the selected input's own current read authority."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, event, select, update

from pandrator.web.access.assembly_input_policy import require_assembly_cover
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.accounts.models import User, WorkspaceMembership
from pandrator.web.model_base import new_id
from pandrator.web.models import Artifact
from pandrator.web.projects.models import Project
from tests.test_generation_execution_policy import case as case
from tests.test_generation_execution_policy import credential
from tests.test_generation_execution_policy import no_network as no_network
from tests.test_generation_execution_policy import template as template


def image(case, *, root=None, workspace_id=None):
    root = case.a if root is None and workspace_id is None else root
    identifier = new_id()
    with case.database.immediate_session() as session:
        if root is not None:
            workspace_id = session.scalar(
                select(Project.workspace_id).where(Project.id == root.project_id)
            )
        session.add(
            Artifact(
                id=identifier,
                kind="image",
                role="cover",
                session_id=root.id if root else None,
                project_id=root.project_id if root else None,
                workspace_id=workspace_id or case.team,
                relative_path=f"artifacts/{identifier}.png",
                mime_type="image/png",
                content_hash="a" * 64,
                metadata_json={"private": "cover metadata sentinel"},
            )
        )
    return identifier


@pytest.mark.parametrize("ownership", ["project", "workspace"])
def test_member_reads_project_and_workspace_owned_covers_without_file_io(case, ownership):
    identifier = (
        image(case, root=case.a) if ownership == "project" else image(case, workspace_id=case.team)
    )
    with execution_actor_scope(credential(case, "member").actor):
        with case.database.snapshot_session() as session:
            proof = require_assembly_cover(session, identifier)
            assert require_assembly_cover(session, identifier, expected=proof) == proof
    assert proof.workspace_id == case.team and proof.content_hash == "a" * 64


def test_member_cannot_resolve_another_users_private_cover_metadata(case):
    outsider = case.services.sessions.create(
        "Private cover", workspace_id=case.personal[case.users["outsider"]]
    )
    identifier = image(case, root=outsider, workspace_id=None)
    statements = []
    with execution_actor_scope(credential(case, "member").actor):
        with case.database.snapshot_session() as session:
            pending = session.get_one(User, case.users["member"])
            before = pending.display_name
            pending.display_name = "Must not flush"

            def capture(_c, _cu, sql, _p, _co, _e):
                statements.append(sql)

            event.listen(case.database.engine, "before_cursor_execute", capture)
            try:
                with pytest.raises(AccessDenied):
                    require_assembly_cover(session, identifier)
            finally:
                event.remove(case.database.engine, "before_cursor_execute", capture)
    with case.database.snapshot_session() as session:
        assert session.get_one(User, case.users["member"]).display_name == before
    assert all(sql.lstrip().startswith("SELECT") for sql in statements)
    assert not any("metadata_json" in sql or "relative_path" in sql for sql in statements)


@pytest.mark.parametrize("change", ["membership", "content", "state"])
def test_selected_cover_rechecks_current_rights_and_header_pin(case, change):
    identifier = image(case, root=case.a)
    actor = credential(case, "member").actor
    with execution_actor_scope(actor):
        with case.database.snapshot_session() as session:
            proof = require_assembly_cover(session, identifier)
    with case.database.immediate_session() as session:
        if change == "membership":
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == actor.user_id,
                )
            )
        else:
            session.execute(
                update(Artifact)
                .where(Artifact.id == identifier)
                .values(
                    **({"content_hash": "b" * 64} if change == "content" else {"state": "deleted"})
                )
            )
    with execution_actor_scope(actor):
        with case.database.snapshot_session() as session:
            with pytest.raises(AccessDenied):
                require_assembly_cover(session, identifier, expected=proof)
