"""Session archive submission proves current sources and destination creation."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from flask.wrappers import Request
from sqlalchemy import event, func, select, text

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.openapi import build_openapi_document
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_source_cleaning_workspace_http import book
from tests.test_source_library_workspace_http import library as library  # noqa: F401


def _bundle(f, workspace, *, session_id=None):
    path = f.services.paths.uploads / f"{uuid4()}.pandrator-session"
    # This packet qualifies admission; archive contents are read by the next phase.
    path.write_bytes(b"Archive admission fixture; do not read or extract")
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path,
            kind="bundle",
            role="upload",
            workspace_id=workspace,
            session_id=session_id,
        )
    with f.case.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE artifacts SET metadata_json = :invalid WHERE id = :id"),
            {"invalid": "not-json", "id": artifact.id},
        )
    return artifact.id, path


def _jobs(f):
    with f.case.database.snapshot_session() as unit:
        return unit.scalar(select(func.count()).select_from(m.Job))


@contextmanager
def _no_artifact_body():
    def denied(_artifact, _context):
        raise AssertionError("Bundle admission must not hydrate the artifact body")

    event.listen(m.Artifact, "load", denied)
    try:
        yield
    finally:
        event.remove(m.Artifact, "load", denied)


@pytest.mark.parametrize("caller", ["peer", "administrator"])
def test_foreign_bundle_is_opaque_before_source_body_path_or_enqueue(library, monkeypatch, caller):
    f, u = library, library.u
    source_id, path = _bundle(f, f.homes[u.alice_id])
    client, auth = (u.bob, u.bh) if caller == "peer" else (u.owner, u.oh)
    before = _jobs(f)
    resolver = Mock(side_effect=AssertionError("Denied import reached path resolution"))
    monkeypatch.setattr(f.services.artifacts, "resolve", resolver)
    with _no_artifact_body():
        body(
            client.post(
                "/api/v1/session-bundles/import",
                json={"source_artifact_id": source_id},
                headers=auth,
            ),
            404,
        )
    resolver.assert_not_called()
    assert _jobs(f) == before and path.read_bytes().startswith(b"Archive admission")


@pytest.mark.parametrize("mode", ["solo", "team"])
def test_solo_and_team_archive_admission_use_current_roots_without_source_body(
    case, monkeypatch, mode
):
    services = case.app.extensions["pandrator"]["services"]
    f = SimpleNamespace(case=case, services=services)
    if mode == "team":
        people = team(case, monkeypatch)
        client, auth, workspace, user_id = (
            people.bob,
            people.bh,
            people.workspace_id,
            people.bob_id,
        )
    else:
        client, auth, user_id = case.client, {"X-CSRF-Token": case.csrf}, case.actor.user_id
        with case.database.snapshot_session() as unit:
            assert unit.scalar(select(func.count()).select_from(m.User)) == 1
            workspace = unit.scalar(
                select(m.Workspace.id).where(m.Workspace.personal_owner_user_id == user_id)
            )
    source_id, path = _bundle(f, workspace)
    resolver = Mock(side_effect=AssertionError("Admission must use finite source headers"))
    monkeypatch.setattr(services.artifacts, "resolve", resolver)
    with _no_artifact_body():
        receipt = body(
            client.post(
                "/api/v1/session-bundles/import",
                json={"source_artifact_id": source_id, "name": "Imported admission"},
                headers=auth,
            ),
            202,
        )
    resolver.assert_not_called()
    with case.database.snapshot_session() as unit:
        job = unit.get_one(m.Job, receipt["id"])
        assert job.kind == "session.bundle.import" and job.workspace_id == workspace
        assert job.project_id is None and job.session_id is None
        assert job.payload_json == {"source_artifact_id": source_id, "name": "Imported admission"}
        assert unit.get_one(m.JobActorRecord, job.id).user_id == user_id
    assert path.read_bytes() == b"Archive admission fixture; do not read or extract"
    record = book(f, client, auth, workspace if mode == "team" else None)
    _, run_only = scoped_token(case, user_id, {"app.read", "app.run"}, project=record["project_id"])
    with _no_artifact_body():
        exported = body(
            case.app.test_client().post(
                f"/api/v1/sessions/{record['id']}/bundle",
                json={"include_sources": False},
                headers=run_only,
            ),
            202,
        )
    with case.database.snapshot_session() as unit:
        job = unit.get_one(m.Job, exported["id"])
        assert (job.kind, job.workspace_id, job.project_id, job.session_id) == (
            "session.bundle.export",
            workspace,
            record["project_id"],
            record["id"],
        )
        assert job.payload_json == {"session_id": record["id"], "include_sources": False}
        assert unit.get_one(m.JobActorRecord, job.id).user_id == user_id


def test_source_project_token_does_not_acquire_workspace_creation_rights(library):
    f, u = library, library.u
    record = book(f, u.alice, u.ah, u.workspace_id)
    source_id, _path = _bundle(f, u.workspace_id, session_id=record["id"])
    _, auth = scoped_token(
        f.case,
        u.bob_id,
        {"app.read", "app.write", "app.run"},
        project=record["project_id"],
    )
    before = _jobs(f)
    body(
        f.case.app.test_client().post(
            "/api/v1/session-bundles/import", json={"source_artifact_id": source_id}, headers=auth
        ),
        404,
    )
    assert _jobs(f) == before


@pytest.mark.parametrize("missing", ["app.write", "app.run"])
def test_import_requires_both_creation_and_processing_scopes(library, missing):
    f, u = library, library.u
    source_id, _path = _bundle(f, u.workspace_id)
    _, auth = scoped_token(
        f.case,
        u.bob_id,
        {"app.read", "app.write", "app.run"} - {missing},
        workspace=u.workspace_id,
    )
    before = _jobs(f)
    body(
        f.case.app.test_client().post(
            "/api/v1/session-bundles/import", json={"source_artifact_id": source_id}, headers=auth
        ),
        403,
    )
    assert _jobs(f) == before


@pytest.mark.parametrize("caller", ["peer", "administrator", "viewer"])
def test_bundle_export_authority_precedes_request_json(library, monkeypatch, caller):
    f, u = library, library.u
    record = book(f, u.alice, u.ah)
    if caller == "viewer":
        viewers = f"/api/v1/projects/{record['project_id']}/viewers"
        current = body(u.alice.get(viewers))
        body(
            u.alice.put(
                f"{viewers}/{u.bob_id}",
                json={"expected_revision": current["revision"]},
                headers=u.ah,
            )
        )
    client, auth = (u.owner, u.oh) if caller == "administrator" else (u.bob, u.bh)
    before = _jobs(f)

    def denied_json(*_args, **_kwargs):
        raise AssertionError("Current export authority must precede request JSON")

    monkeypatch.setattr(Request, "get_json", denied_json)
    body(
        client.post(
            f"/api/v1/sessions/{record['id']}/bundle",
            data="{",
            content_type="application/json",
            headers=auth,
        ),
        403 if caller == "viewer" else 404,
    )
    assert _jobs(f) == before


def test_archive_openapi_scopes_distinguish_export_and_import_creation():
    paths = build_openapi_document()["paths"]
    for path, scopes in (
        ("/api/v1/sessions/{sessionId}/bundle", ["app.read", "app.run"]),
        ("/api/v1/session-bundles/import", ["app.read", "app.write", "app.run"]),
    ):
        assert paths[path]["post"]["x-required-scopes"] == scopes
        assert {"nativeOAuth": scopes} in paths[path]["post"]["security"]
