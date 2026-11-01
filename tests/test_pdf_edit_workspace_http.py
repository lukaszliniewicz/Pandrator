"""PDF edits use current project/source rights through real native workers."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pymupdf as fitz
import pytest
from flask.wrappers import Request
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.artifacts import ArtifactService
from pandrator.web.jobs import Worker
from pandrator.web.openapi import build_openapi_document
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_purge_workspace_http import project


def _fixture(case):
    return SimpleNamespace(case=case, services=case.app.extensions["pandrator"]["services"])


def _source(f, record):
    path = f.services.paths.uploads / f"{uuid4()}.pdf"
    with fitz.open() as document:
        for index in range(2):
            page = document.new_page()
            page.insert_text((72, 72), f"Original page {index + 1}")
        document.save(path)
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path, kind="pdf", role="upload", session_id=record["id"]
        )
    return artifact.id, path


def _counts(f):
    with f.case.database.snapshot_session() as unit:
        return tuple(
            unit.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.Artifact, m.ArtifactEdge)
        )


def _apply(client, auth, record, source_id):
    return client.post(
        f"/api/v1/sessions/{record['id']}/pdf/apply",
        json={"source_artifact_id": source_id, "deleted_pages": [1]},
        headers=auth,
    )


def _worker(f, receipt):
    assert Worker(
        f.services.jobs, "current-pdf-edit", f.services.workflow_handlers.handlers()
    ).run_once()
    return f.services.jobs.get(receipt["id"])


@pytest.mark.parametrize("mode", ["solo", "team"])
def test_native_pdf_edit_worker_keeps_source_and_current_caller_after_creator_leaves(
    case, monkeypatch, mode
):
    f = _fixture(case)
    people = team(case, monkeypatch) if mode == "team" else None
    client, auth, user_id, workspace = (
        (people.alice, people.ah, people.alice_id, people.workspace_id)
        if people is not None
        else (case.client, {"X-CSRF-Token": case.csrf}, case.actor.user_id, None)
    )
    record = project(client, auth, workspace)
    source_id, path = _source(f, record)
    original = path.read_bytes()
    if people is not None:
        with case.database.immediate_session() as unit:
            unit.delete(unit.get_one(WorkspaceMembership, (workspace, people.alice_id)))
        _, auth = scoped_token(
            case, people.bob_id, {"app.read", "app.run"}, project=record["project_id"]
        )
        client, user_id = case.app.test_client(), people.bob_id
    receipt = body(_apply(client, auth, record, source_id), 202)
    completed = _worker(f, receipt)
    assert completed.status == "succeeded", completed.error_message
    with case.database.snapshot_session() as unit:
        job = unit.get_one(m.Job, receipt["id"])
        edited = unit.get_one(m.Artifact, completed.result_json["artifact_id"])
        provenance = unit.get_one(m.Artifact, completed.result_json["manifest_artifact_id"])
        root = unit.get_one(m.Project, record["project_id"])
        assert job.session_id == record["id"] and job.project_id == root.id
        assert job.workspace_id == root.workspace_id == edited.workspace_id
        assert edited.project_id == provenance.project_id == root.id
        assert unit.get_one(m.JobActorRecord, receipt["id"]).user_id == user_id
        assert unit.get(m.ArtifactEdge, (source_id, edited.id)) is not None
        edited_path = f.services.paths.managed_path(edited.relative_path)
        if people is None:
            assert unit.scalar(select(func.count()).select_from(m.User)) == 1
    assert path.read_bytes() == original
    original_download = client.get(f"/api/v1/artifacts/{source_id}/content", headers=auth)
    assert original_download.status_code == 200 and original_download.data == original
    with fitz.open(edited_path) as document:
        assert document.page_count == 1 and "Original page 1" in document[0].get_text()
    downloaded = client.get(
        f"/api/v1/artifacts/{completed.result_json['artifact_id']}/content", headers=auth
    )
    assert downloaded.status_code == 200 and downloaded.data == edited_path.read_bytes()


@pytest.mark.parametrize("caller", ["foreign", "administrator", "viewer"])
def test_pdf_target_permission_precedes_json(case, monkeypatch, caller):
    f, people = _fixture(case), team(case, monkeypatch)
    record = project(people.alice, people.ah)
    client, auth, expected = people.bob, people.bh, 404
    if caller == "administrator":
        client, auth = people.owner, people.oh
    elif caller == "viewer":
        root = f"/api/v1/projects/{record['project_id']}/viewers"
        current = body(people.alice.get(root))
        body(
            people.alice.put(
                f"{root}/{people.bob_id}",
                json={"expected_revision": current["revision"]},
                headers=people.ah,
            )
        )
        expected = 403
    before = _counts(f)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Denied PDF edit reached request JSON")

    monkeypatch.setattr(Request, "get_json", forbidden)
    assert _apply(client, auth, record, "unread-source").status_code == expected
    assert _counts(f) == before


def test_pdf_foreign_source_is_opaque_before_body_or_enqueue(case, monkeypatch):
    f, people = _fixture(case), team(case, monkeypatch)
    target = project(people.alice, people.ah, people.workspace_id)
    private = project(people.bob, people.bh)
    source_id, source_path = _source(f, private)
    original = source_path.read_bytes()
    before = _counts(f)

    def forbidden(_artifact, _context):
        pytest.fail("Denied PDF edit hydrated an artifact body")

    event.listen(m.Artifact, "load", forbidden)
    try:
        body(_apply(people.alice, people.ah, target, source_id), 404)
    finally:
        event.remove(m.Artifact, "load", forbidden)
    assert _counts(f) == before and source_path.read_bytes() == original


@pytest.mark.parametrize("scopes", [{"app.read", "app.write"}, {"app.run"}])
def test_pdf_processing_scope_ceiling_precedes_json(case, monkeypatch, scopes):
    f, people = _fixture(case), team(case, monkeypatch)
    record = project(people.alice, people.ah, people.workspace_id)
    _, auth = scoped_token(case, people.bob_id, scopes, project=record["project_id"])
    before = _counts(f)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Narrow PDF credential reached JSON")

    monkeypatch.setattr(Request, "get_json", forbidden)
    assert _apply(case.app.test_client(), auth, record, "unread-source").status_code == 403
    assert _counts(f) == before


def test_pdf_final_admission_writer_rechecks_membership(case, monkeypatch):
    f, people = _fixture(case), team(case, monkeypatch)
    record = project(people.alice, people.ah, people.workspace_id)
    source_id, path = _source(f, record)
    original = path.read_bytes()
    before = _counts(f)
    parse = Request.get_json
    changed = []

    def leave_after_json(request, *args, **kwargs):
        value = parse(request, *args, **kwargs)
        if not changed:
            with case.database.immediate_session() as unit:
                unit.delete(
                    unit.get_one(WorkspaceMembership, (people.workspace_id, people.alice_id))
                )
            changed.append(True)
        return value

    monkeypatch.setattr(Request, "get_json", leave_after_json)
    body(_apply(people.alice, people.ah, record, source_id), 404)
    assert changed and _counts(f) == before and path.read_bytes() == original


def test_pdf_worker_registration_failure_rolls_back_both_outputs(case, monkeypatch):
    f, people = _fixture(case), team(case, monkeypatch)
    record = project(people.alice, people.ah, people.workspace_id)
    source_id, path = _source(f, record)
    original = path.read_bytes()
    receipt = body(_apply(people.bob, people.bh, record, source_id), 202)
    before = _counts(f)
    existing = set(f.services.paths.sessions.rglob("*"))
    register = ArtifactService.register_in_session
    registered = []

    def fail_after_manifest(self, unit, output, **kwargs):
        artifact = register(self, unit, output, **kwargs)
        registered.append(artifact.id)
        if len(registered) == 2:
            raise RuntimeError("Synthetic PDF manifest publication failure")
        return artifact

    monkeypatch.setattr(ArtifactService, "register_in_session", fail_after_manifest)
    completed = _worker(f, receipt)
    assert completed.status == "failed" and len(registered) == 2
    assert _counts(f) == before and path.read_bytes() == original
    assert {p for p in f.services.paths.sessions.rglob("*") if p.is_file()} == {
        p for p in existing if p.is_file()
    }


def test_pdf_openapi_processing_scopes_and_safe_denials():
    operation = build_openapi_document()["paths"]["/api/v1/sessions/{sessionId}/pdf/apply"]["post"]
    assert operation["x-required-scopes"] == ["app.read", "app.run"]
    assert set(operation["responses"]) == {"202", "403", "404"}
