"""Artifact reads use current ownership before metadata, paging and file IO."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pymupdf as fitz
import pytest
from sqlalchemy import delete, text

from pandrator.web.accounts.models import Workspace
from pandrator.web.http_lifecycle import ApiGuards
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    OutputAssembly,
    UsageEvent,
    new_id,
    utcnow,
)
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.usage_identity import bind_usage_ownership
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import credential, error
from tests.test_job_access_routes import no_network as no_network


def artifact(case, *, project="a", workspace=None, parents=None, metadata=None):
    path = case.services.paths.artifacts / f"{new_id()}.txt"
    path.write_bytes(b"0123456789")
    row = case.services.artifacts.register(
        path,
        kind="text",
        role="export",
        session_id=getattr(case, project).id if project else None,
        workspace_id=workspace,
        parent_ids=parents,
        metadata=metadata,
    )
    return row, path


def generation(case, project):
    with case.database.immediate_session() as session:
        plan = GenerationPlan(session_id=getattr(case, project).id)
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(
            plan_id=plan.id, revision_number=1, content_hash="synthetic"
        )
        session.add(revision)
        session.flush()
        run = GenerationRun(session_id=getattr(case, project).id, plan_revision_id=revision.id)
        session.add(run)
        session.flush()
    return run, revision


def usage(case, project, *, artifact_id=None, generation_run_id=None, tokens=3):
    with case.database.immediate_session() as session:
        row = UsageEvent(
            session_id=getattr(case, project).id,
            artifact_id=artifact_id,
            generation_run_id=generation_run_id,
            provider_key="synthetic",
            model_id=f"{project}-model",
            input_tokens=tokens,
        )
        bind_usage_ownership(session, row)
        session.add(row)


@pytest.mark.parametrize("view", ["full", "summary", "compact"])
def test_artifact_visibility_precedes_pagination_counts_and_json_decode(case, view):
    viewer = credential(case, "viewer")
    allowed = [artifact(case)[0] for _ in range(2)]
    hidden = [artifact(case, project="b")[0] for _ in range(8)]
    with case.database.immediate_session() as session:
        for index, row in enumerate(allowed):
            session.get(Artifact, row.id).created_at = utcnow() - timedelta(days=2, seconds=index)
        # Invalid JSON in a hidden row proves it never reaches ORM decoding.
        session.execute(
            text("UPDATE artifacts SET metadata_json = :value WHERE id = :id"),
            {"value": "hidden invalid JSON sentinel", "id": hidden[0].id},
        )
    response = viewer.client.get(
        f"/api/v1/artifacts?view={view}&limit=1&role=export", headers=viewer.headers
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert [row["id"] for row in body["items"]] == [allowed[0].id]
    assert all(row.id not in response.get_data(as_text=True) for row in hidden)
    assert case.b.name not in response.get_data(as_text=True)
    if view == "compact":
        assert body["total"] == 2
        assert body["next_offset"] == 1
    second = viewer.client.get(
        f"/api/v1/artifacts?view={view}&limit=1&offset=1", headers=viewer.headers
    )
    assert [row["id"] for row in second.get_json()["items"]] == [allowed[1].id]
    filtered = viewer.client.get(
        f"/api/v1/artifacts?view={view}&session_id={case.b.id}", headers=viewer.headers
    )
    assert filtered.status_code == 200
    assert filtered.get_json()["items"] == []
    if view == "compact":
        assert filtered.get_json()["total"] == 0


@pytest.mark.parametrize("suffix", ["context", "content", "pdf"])
@pytest.mark.parametrize("method", ["get", "head"])
def test_hidden_and_unknown_artifact_reads_deny_before_file_effects(
    case, monkeypatch, suffix, method
):
    viewer = credential(case, "viewer")
    hidden, _ = artifact(case, project="b")
    reject = Mock(side_effect=AssertionError("Denied reads must not reach managed files."))
    monkeypatch.setattr(type(case.services.paths), "managed_path", reject)
    monkeypatch.setattr("pandrator.web.artifact_routes.send_file", reject)
    monkeypatch.setattr("pandrator.web.pdf_editor.inspect_pdf", reject)
    monkeypatch.setattr(case.services.artifacts, "resolve", reject)
    call = getattr(viewer.client, method)
    denied = call(f"/api/v1/artifacts/{hidden.id}/{suffix}", headers=viewer.headers)
    missing = call(f"/api/v1/artifacts/{new_id()}/{suffix}", headers=viewer.headers)
    assert denied.status_code == missing.status_code == 404
    if method == "get":
        assert error(denied) == error(missing)
    else:
        assert denied.data == missing.data == b""
    reject.assert_not_called()


def test_instance_admin_and_team_membership_do_not_grant_private_artifacts(case):
    viewer = credential(case, "viewer")
    outsider = credential(case, "outsider", scopes={"app.read", "app.admin"})
    owner = credential(case, "owner")
    project_row, _ = artifact(case)
    workspace_row, _ = artifact(case, project=None, workspace=case.team)
    for row in (project_row, workspace_row):
        assert (
            outsider.client.get(
                f"/api/v1/artifacts/{row.id}/content", headers=outsider.headers
            ).status_code
            == 404
        )
    assert (
        viewer.client.get(
            f"/api/v1/artifacts/{project_row.id}/content", headers=viewer.headers
        ).status_code
        == 200
    )
    assert (
        viewer.client.get(
            f"/api/v1/artifacts/{workspace_row.id}/content", headers=viewer.headers
        ).status_code
        == 404
    )
    assert (
        owner.client.get(
            f"/api/v1/artifacts/{workspace_row.id}/content", headers=owner.headers
        ).status_code
        == 200
    )


def test_actual_noninitial_personal_owner_reads_only_their_own_rootless_artifact(case):
    caller = credential(case, "editor", grant_team=False)
    own, _ = artifact(case, project=None, workspace=case.personal[case.users["editor"]])
    initial, _ = artifact(case, project=None, workspace=case.personal[case.users["owner"]])
    assert (
        caller.client.get(f"/api/v1/artifacts/{own.id}/content", headers=caller.headers).data
        == b"0123456789"
    )
    assert (
        caller.client.get(
            f"/api/v1/artifacts/{initial.id}/content", headers=caller.headers
        ).status_code
        == 404
    )


def test_context_filters_each_lineage_edge_and_preserves_visible_usage(case):
    viewer = credential(case, "viewer")
    parent, _ = artifact(case)
    hidden_parent, _ = artifact(case, project="b", metadata={"secret": "private parent"})
    root, _ = artifact(case, parents=[parent.id, hidden_parent.id])
    child, _ = artifact(case, parents=[root.id])
    hidden_child, _ = artifact(case, project="b", parents=[root.id])
    usage(case, "a", artifact_id=root.id)
    usage(case, "a", artifact_id=parent.id, tokens=5)
    usage(case, "b", artifact_id=hidden_parent.id, tokens=100)
    response = viewer.client.get(f"/api/v1/artifacts/{root.id}/context", headers=viewer.headers)
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert [row["id"] for row in result["parents"]] == [parent.id]
    assert [row["id"] for row in result["children"]] == [child.id]
    assert result["usage"]["input_tokens"] == 8
    assert result["usage"]["event_count"] == 2
    assert result["usage"]["model_ids"] == ["a-model"]
    body = response.get_data(as_text=True)
    assert hidden_parent.id not in body and hidden_child.id not in body
    assert "private parent" not in body


@pytest.mark.parametrize("pointer", ["generation_run_id", "output_assembly_id"])
def test_context_metadata_pointers_must_match_actual_session_before_usage_expansion(case, pointer):
    viewer = credential(case, "viewer")
    root, _ = artifact(case)
    hidden, _ = artifact(case, project="b")
    own_run, _ = generation(case, "a")
    foreign_run, _ = generation(case, "b")
    usage(case, "a", generation_run_id=own_run.id, tokens=7)
    usage(case, "b", generation_run_id=foreign_run.id, tokens=100)
    with case.database.immediate_session() as session:
        if pointer == "output_assembly_id":
            assembly = OutputAssembly(
                session_id=case.b.id, generation_run_id=foreign_run.id, artifact_id=hidden.id
            )
            session.add(assembly)
            session.flush()
            identifier = assembly.id
        else:
            identifier = foreign_run.id
        session.get(Artifact, root.id).metadata_json = {pointer: identifier}
    response = viewer.client.get(f"/api/v1/artifacts/{root.id}/context", headers=viewer.headers)
    assert response.status_code == 200
    assert response.get_json()["usage"]["event_count"] == 0
    with case.database.immediate_session() as session:
        if pointer == "output_assembly_id":
            assembly = OutputAssembly(
                session_id=case.a.id, generation_run_id=own_run.id, artifact_id=root.id
            )
            session.add(assembly)
            session.flush()
            identifier = assembly.id
        else:
            identifier = own_run.id
        session.get(Artifact, root.id).metadata_json = {pointer: identifier}
    result = viewer.client.get(
        f"/api/v1/artifacts/{root.id}/context", headers=viewer.headers
    ).get_json()
    assert result["usage"]["input_tokens"] == 7
    assert result["usage"]["model_ids"] == ["a-model"]


def test_compact_labels_do_not_follow_a_take_into_another_project(case):
    viewer = credential(case, "viewer")
    row, _ = artifact(case)
    _, revision = generation(case, "b")
    with case.database.immediate_session() as session:
        segment = GenerationSegment(
            plan_revision_id=revision.id, ordinal=1, text="Private foreign segment sentinel"
        )
        session.add(segment)
        session.flush()
        session.add(AudioTake(generation_segment_id=segment.id, artifact_id=row.id))
    response = viewer.client.get("/api/v1/artifacts?view=compact", headers=viewer.headers)
    assert response.status_code == 200
    assert "Private foreign segment sentinel" not in response.get_data(as_text=True)
    assert "segment_text" not in response.get_json()["items"][0]


def test_current_membership_is_rechecked_after_transport_authentication(case, monkeypatch):
    viewer = credential(case, "viewer")
    row, _ = artifact(case)
    real_context = ApiGuards.access_context

    def revoke_before_policy(guards):
        access = real_context(guards)
        with case.database.immediate_session() as session:
            session.execute(
                delete(ProjectMembership).where(
                    ProjectMembership.project_id == case.a.project_id,
                    ProjectMembership.user_id == case.users["viewer"],
                )
            )
        return access

    monkeypatch.setattr(ApiGuards, "access_context", revoke_before_policy)
    assert (
        viewer.client.get(f"/api/v1/artifacts/{row.id}/content", headers=viewer.headers).status_code
        == 404
    )


def test_archived_workspace_and_deleted_artifact_lifecycle_are_distinct(case):
    viewer = credential(case, "viewer")
    row, path = artifact(case)
    with case.database.immediate_session() as session:
        session.get(Artifact, row.id).state = "deleted"
    assert path.is_file()
    assert (
        viewer.client.get(f"/api/v1/artifacts/{row.id}/context", headers=viewer.headers).status_code
        == 200
    )
    for suffix in ("content", "pdf"):
        assert (
            viewer.client.get(
                f"/api/v1/artifacts/{row.id}/{suffix}", headers=viewer.headers
            ).status_code
            == 404
        )
    assert viewer.client.get("/api/v1/artifacts", headers=viewer.headers).get_json()["items"] == []
    assert (
        len(
            viewer.client.get(
                "/api/v1/artifacts?include_deleted=true", headers=viewer.headers
            ).get_json()["items"]
        )
        == 1
    )
    with case.database.immediate_session() as session:
        session.get(Workspace, case.team).state = "archived"
    assert (
        viewer.client.get(f"/api/v1/artifacts/{row.id}/context", headers=viewer.headers).status_code
        == 404
    )


def test_authorized_content_retains_native_conditional_range_head_and_missing_file(case):
    viewer = credential(case, "viewer")
    row, path = artifact(case)
    url = f"/api/v1/artifacts/{row.id}/content"
    with viewer.client.get(url, headers=viewer.headers) as response:
        assert response.status_code == 200 and response.data == b"0123456789"
        etag = response.headers["ETag"]
    with viewer.client.get(url, headers={**viewer.headers, "If-None-Match": etag}) as response:
        assert response.status_code == 304 and response.data == b""
    with viewer.client.get(url, headers={**viewer.headers, "Range": "bytes=0-3"}) as response:
        assert response.status_code == 206 and response.data == b"0123"
        assert response.headers["Content-Range"] == "bytes 0-3/10"
    with viewer.client.head(url, headers=viewer.headers) as response:
        assert response.status_code == 200 and response.data == b""
        assert response.content_length == 10
    path.unlink()
    assert viewer.client.get(url, headers=viewer.headers).status_code == 410


def test_authorized_pdf_inspection_retains_geometry_and_validation(case):
    viewer = credential(case, "viewer")
    path = case.services.paths.artifacts / f"{new_id()}.pdf"
    with fitz.open() as document:
        document.new_page(width=200, height=300)
        document.save(path)
    row = case.services.artifacts.register(path, kind="pdf", session_id=case.a.id)
    url = f"/api/v1/artifacts/{row.id}/pdf"
    response = viewer.client.get(url + "?first_page_side=left", headers=viewer.headers)
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result["page_count"] == 1
    assert result["pages"][0]["width"] == 200 and result["pages"][0]["height"] == 300
    assert result["pages"][0]["side"] == "left"
    assert (
        viewer.client.get(url + "?first_page_side=invalid", headers=viewer.headers).status_code
        == 422
    )
    path.unlink()
    assert viewer.client.get(url, headers=viewer.headers).status_code == 422
