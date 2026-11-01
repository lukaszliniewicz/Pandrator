"""Named source acquisition admits and publishes only in the current workspace."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from flask.wrappers import Request
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.jobs import Worker
from pandrator.web.source_library import SourceLibraryService
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_source_library_workspace_http import library as library  # noqa: F401

URL = "https://source.invalid/shared-file"
DATA = b"The original shared source remains intact."


def project(client, csrf, workspace=None):
    values = {"name": f"Source ingress {uuid4()}", "workflow_kind": "subtitles"}
    if workspace is not None:
        values["workspace_id"] = workspace
    return body(
        client.post("/api/v1/sessions", json=values, headers=headers(csrf, str(uuid4()))), 201
    )


def counts(f):
    with f.case.database.snapshot_session() as unit:
        return {
            model.__tablename__: unit.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.Artifact, m.SourceRecord, m.SourceAsset, m.SessionSource)
        }


def execute(f, receipt):
    assert Worker(
        f.services.jobs, "named-source-ingress", f.services.workflow_handlers.handlers()
    ).run_once()
    return f.services.jobs.get(receipt["id"])


def downloader(monkeypatch):
    calls = []

    class LocalDownloader:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download):
            calls.append((url, download))
            self.destination = Path(self.options["outtmpl"]).parent / "shared-source.txt"
            self.destination.write_bytes(DATA)
            for hook in self.options["progress_hooks"]:
                hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": len(DATA),
                        "total_bytes": len(DATA),
                    }
                )
                hook({"status": "finished"})
            return {"id": "synthetic", "ext": "txt", "title": "shared-source"}

        def prepare_filename(self, _information):
            return str(self.destination)

    monkeypatch.setattr("yt_dlp.YoutubeDL", LocalDownloader)
    monkeypatch.setattr(
        "pandrator.web.workflow_handlers.WorkflowHandlers._validate_download_url",
        staticmethod(lambda url: url),
    )
    return calls


@pytest.mark.parametrize("actor", ["viewer", "foreign_admin", "read_run", "read_write"])
def test_ingress_denials_precede_json_and_private_bodies(library, monkeypatch, actor):
    f, u = library, library.u
    record = project(u.bob, u.bh, u.workspace_id if actor.startswith("read_") else None)
    if actor == "viewer":
        with f.case.database.immediate_session() as unit:
            unit.add(
                m.ProjectMembership(
                    project_id=record["project_id"], user_id=u.alice_id, role="viewer"
                )
            )
        client, csrf, status = u.alice, u.ah, 403
    elif actor == "foreign_admin":
        client, csrf, status = u.owner, u.oh, 404
    else:
        scopes = {"app.read", "app.run" if actor == "read_run" else "app.write"}
        _, csrf = scoped_token(f.case, u.alice_id, scopes, workspace=u.workspace_id)
        client, status = f.case.app.test_client(), 403
    before = counts(f)

    def forbidden(*_args, **_kwargs):
        pytest.fail("A denied ingress request reached JSON")

    monkeypatch.setattr(Request, "get_json", forbidden)
    for operation in ("url", "reuse"):
        response = client.post(
            f"/api/v1/sessions/{record['id']}/sources/{operation}", json={}, headers=csrf
        )
        assert response.status_code == status, response.get_json()
    assert counts(f) == before


@pytest.mark.parametrize("readable", [False, True])
def test_cross_workspace_reuse_denies_before_artifact_body_or_queue(library, monkeypatch, readable):
    f, u = library, library.u
    client, csrf = (u.owner, u.oh) if readable else (u.alice, u.ah)
    source = f.original if readable else f.private
    record = project(client, csrf, u.workspace_id)
    before, statements = counts(f), []
    resolve = Mock(side_effect=AssertionError("Ingress admission hydrated a private artifact"))
    monkeypatch.setattr(f.services.artifacts, "resolve", resolve)

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        response = client.post(
            f"/api/v1/sessions/{record['id']}/sources/reuse",
            json={"artifact_id": source.artifact_id},
            headers=csrf,
        )
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)
    assert response.status_code == 404, response.get_json()
    resolve.assert_not_called()
    assert all("artifacts.metadata_json" not in sql for sql in statements)
    assert counts(f) == before


@pytest.mark.parametrize("operation", ["url", "reuse"])
def test_enqueue_writer_rechecks_run_scope_after_json(library, monkeypatch, operation):
    f, u = library, library.u
    record = project(u.alice, u.ah, u.workspace_id)
    token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.write", "app.run"}, workspace=u.workspace_id
    )
    client = f.case.app.test_client()
    url = f"/api/v1/sessions/{record['id']}/sources/{operation}"
    values = {"url": URL} if operation == "url" else {"artifact_id": f.shared.artifact_id}
    before, original = counts(f), Request.get_json

    def parse_then_reduce(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.path == url:
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.ApiToken, token.id).scopes_json = ["app.read", "app.write"]
        return result

    monkeypatch.setattr(Request, "get_json", parse_then_reduce)
    response = client.post(url, json=values, headers=bearer)
    assert response.status_code == 403, response.get_json()
    assert counts(f) == before


def test_members_download_and_reuse_after_creator_departure_preserving_original_bytes(
    library, monkeypatch
):
    f, u = library, library.u
    calls = downloader(monkeypatch)
    first = project(u.alice, u.ah, u.workspace_id)
    admitted = body(
        u.alice.post(
            f"/api/v1/sessions/{first['id']}/sources/url", json={"url": URL}, headers=u.ah
        ),
        202,
    )
    completed = execute(f, admitted)
    assert completed.status == "succeeded", completed.error_message
    assert calls == [(URL, True)]
    source_id = completed.result_json["artifact_id"]
    with f.case.database.immediate_session() as unit:
        original = unit.get_one(m.Artifact, source_id)
        original_path = f.services.paths.managed_path(original.relative_path)
        assert (
            original.workspace_id == u.workspace_id and original.project_id == first["project_id"]
        )
        unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    second = project(u.bob, u.bh, u.workspace_id)
    admitted = body(
        u.bob.post(
            f"/api/v1/sessions/{second['id']}/sources/reuse",
            json={"artifact_id": source_id},
            headers=u.bh,
        ),
        202,
    )
    reused = execute(f, admitted)
    assert reused.status == "succeeded", reused.error_message
    output_id = reused.result_json["artifact_id"]
    with f.case.database.snapshot_session() as unit:
        output = unit.get_one(m.Artifact, output_id)
        asset = unit.scalar(select(m.SourceAsset).where(m.SourceAsset.artifact_id == output_id))
        assert output.workspace_id == u.workspace_id and output.project_id == second["project_id"]
        assert asset is not None and asset.workspace_id == u.workspace_id
        assert (
            unit.scalar(
                select(m.ArtifactEdge.parent_artifact_id).where(
                    m.ArtifactEdge.child_artifact_id == output_id
                )
            )
            == source_id
        )
        assert (
            unit.scalar(
                select(func.count())
                .select_from(m.ProjectMembership)
                .where(m.ProjectMembership.project_id == second["project_id"])
            )
            == 0
        )
        assert unit.get_one(m.Project, second["project_id"]).created_by_user_id == u.bob_id
        assert (
            f.services.paths.managed_path(output.relative_path).read_bytes()
            == original_path.read_bytes()
            == DATA
        )
    assert u.bob.get(f"/api/v1/artifacts/{output_id}/content").data == DATA
    assert u.alice.get(f"/api/v1/artifacts/{output_id}/content").status_code == 404


@pytest.mark.parametrize("operation", ["url", "reuse"])
def test_revoked_queued_member_does_no_dns_input_copy_or_publication(
    library, monkeypatch, operation
):
    f, u = library, library.u
    record = project(u.alice, u.ah, u.workspace_id)
    values = {"url": URL} if operation == "url" else {"artifact_id": f.shared.artifact_id}
    receipt = body(
        u.alice.post(
            f"/api/v1/sessions/{record['id']}/sources/{operation}", json=values, headers=u.ah
        ),
        202,
    )
    before = counts(f)
    with f.case.database.immediate_session() as unit:
        unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
        key = unit.get_one(m.SessionRecord, record["id"]).storage_key
    native = Mock(side_effect=AssertionError("A revoked job reached acquisition IO"))
    monkeypatch.setattr(
        "pandrator.web.workflow_handlers.WorkflowHandlers._validate_download_url",
        staticmethod(native),
    )
    monkeypatch.setattr("pandrator.web.workflow_source.shutil.copy2", native)
    completed = execute(f, receipt)
    assert completed.status == "failed"
    native.assert_not_called()
    assert not (f.services.paths.sessions / key / "sources").exists()
    assert counts(f) == before


def test_publication_failure_rolls_back_artifact_source_asset_and_selection(library, monkeypatch):
    f, u = library, library.u
    record = project(u.alice, u.ah, u.workspace_id)
    receipt = body(
        u.alice.post(
            f"/api/v1/sessions/{record['id']}/sources/reuse",
            json={"artifact_id": f.shared.artifact_id},
            headers=u.ah,
        ),
        202,
    )
    before = counts(f)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Synthetic selection publication failure")

    monkeypatch.setattr(SourceLibraryService, "attach_in_session", fail)
    completed = execute(f, receipt)
    assert completed.status == "failed" and "Synthetic selection" in completed.error_message
    assert counts(f) == before
