"""Uploaded archives publish complete new workspace roots through real workers."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.artifacts import ArtifactService
from pandrator.web.jobs import Worker
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_cleaning_workspace_http import book, source


def _execute(f, receipt):
    assert Worker(
        f.services.jobs, "current-archive-import", f.services.workflow_handlers.handlers()
    ).run_once()
    return f.services.jobs.get(receipt["id"])


def _export(f, client, auth, session_id):
    queued = body(
        client.post(
            f"/api/v1/sessions/{session_id}/bundle",
            json={},
            headers=headers(auth, str(uuid4())),
        ),
        202,
    )
    completed = _execute(f, queued)
    assert completed.status == "succeeded", completed.error_message
    with f.case.database.snapshot_session() as unit:
        artifact = unit.get_one(m.Artifact, completed.result_json["artifact_id"])
        return f.services.paths.managed_path(artifact.relative_path).read_bytes()


def _upload(f, client, auth, payload, workspace=None):
    values = {"file": (io.BytesIO(payload), f"{uuid4()}.pandrator-session")}
    query = {}
    if workspace is not None:
        values["workspace_id"] = workspace
        query["workspace_id"] = workspace
    receipt = body(
        client.post("/api/v1/uploads", data=values, query_string=query, headers=auth), 201
    )
    with f.case.database.snapshot_session() as unit:
        row = unit.get_one(m.Artifact, receipt["artifact_id"])
        return row.id, f.services.paths.managed_path(row.relative_path)


def _admit(client, auth, artifact_id):
    return body(
        client.post(
            "/api/v1/session-bundles/import",
            json={"source_artifact_id": artifact_id, "name": f"Imported {uuid4()}"},
            headers=auth,
        ),
        202,
    )


def _content_counts(f):
    with f.case.database.snapshot_session() as unit:
        return tuple(
            unit.scalar(select(func.count()).select_from(model))
            for model in (m.SessionRecord, m.Project, m.Artifact, m.ArtifactEdge)
        )


@pytest.mark.parametrize("mode", ["solo", "team"])
def test_real_export_upload_import_has_new_ids_bytes_workspace_and_caller(case, monkeypatch, mode):
    f = SimpleNamespace(case=case, services=case.app.extensions["pandrator"]["services"])
    people = team(case, monkeypatch) if mode == "team" else None
    client, auth, user_id, workspace = (
        (people.alice, people.ah, people.alice_id, people.workspace_id)
        if people is not None
        else (case.client, {"X-CSRF-Token": case.csrf}, case.actor.user_id, None)
    )
    original = book(f, client, auth, workspace)
    source_artifact, source_path = source(f, original)
    source_bytes = source_path.read_bytes()
    archive = _export(f, client, auth, original["id"])
    upload_id, upload_path = _upload(f, client, auth, archive, workspace)
    if people is not None:
        with case.database.immediate_session() as unit:
            unit.delete(unit.get_one(WorkspaceMembership, (workspace, people.alice_id)))
        client, auth, user_id = people.bob, people.bh, people.bob_id
    receipt = _admit(client, auth, upload_id)
    completed = _execute(f, receipt)
    assert completed.status == "succeeded", completed.error_message
    result = completed.result_json
    assert result["session_id"] != original["id"]
    assert result["source_bundle_sha256"] == hashlib.sha256(archive).hexdigest()
    with case.database.snapshot_session() as unit:
        record = unit.get_one(m.SessionRecord, result["session_id"])
        project = unit.get_one(m.Project, record.project_id)
        imported = list(unit.scalars(select(m.Artifact).where(m.Artifact.session_id == record.id)))
        assert project.workspace_id == unit.get_one(m.Artifact, upload_id).workspace_id
        assert project.created_by_user_id == user_id
        assert unit.get_one(m.JobActorRecord, receipt["id"]).user_id == user_id
        assert len(imported) == 1 and imported[0].id != source_artifact.id
        assert imported[0].project_id == project.id
        imported_path = f.services.paths.managed_path(imported[0].relative_path)
        if people is None:
            assert unit.scalar(select(func.count()).select_from(m.User)) == 1
    assert imported_path.read_bytes() == source_bytes
    assert source_path.read_bytes() == source_bytes and upload_path.read_bytes() == archive
    assert client.get(f"/api/v1/sessions/{result['session_id']}").status_code == 200
    assert _export(f, client, auth, result["session_id"])


@pytest.mark.parametrize("failure", ["registration", "membership"])
def test_import_publication_failure_leaves_no_partial_root_or_artifact(case, monkeypatch, failure):
    f = SimpleNamespace(case=case, services=case.app.extensions["pandrator"]["services"])
    people = team(case, monkeypatch)
    original = book(f, people.alice, people.ah, people.workspace_id)
    _artifact, path = source(f, original)
    archive = _export(f, people.alice, people.ah, original["id"])
    upload_id, upload_path = _upload(f, people.alice, people.ah, archive, people.workspace_id)
    before = _content_counts(f)
    before_directories = set(f.services.paths.sessions.iterdir())
    changed = []
    if failure == "registration":
        original_register = ArtifactService.register_in_session

        def fail_after_insert(self, unit, output, **kwargs):
            original_register(self, unit, output, **kwargs)
            changed.append(output)
            raise RuntimeError("Synthetic import registration failure")

        monkeypatch.setattr(ArtifactService, "register_in_session", fail_after_insert)
    else:
        original_prepare = ArtifactService.prepare_registration

        def leave_after_preparation(self, output, **kwargs):
            value = original_prepare(self, output, **kwargs)
            if not changed:
                with case.database.immediate_session() as unit:
                    unit.delete(
                        unit.get_one(WorkspaceMembership, (people.workspace_id, people.alice_id))
                    )
                changed.append(output)
            return value

        monkeypatch.setattr(ArtifactService, "prepare_registration", leave_after_preparation)
    receipt = _admit(people.alice, people.ah, upload_id)
    completed = _execute(f, receipt)
    assert changed and completed.status == "failed"
    assert _content_counts(f) == before
    assert set(f.services.paths.sessions.iterdir()) == before_directories
    assert upload_path.read_bytes() == archive and path.read_text() == "Shared narration."


def test_unsafe_portable_member_fails_through_upload_and_worker_without_publication(
    case, monkeypatch
):
    f = SimpleNamespace(case=case, services=case.app.extensions["pandrator"]["services"])
    people = team(case, monkeypatch)
    member, payload = "..\\outside.txt", b"No escape write"
    manifest = {
        "format": "pandrator-session",
        "version": 1,
        "session": {"name": "Unsafe member", "workflow_kind": "audiobook"},
        "artifacts": [
            {
                "source_id": "old-source",
                "kind": "text",
                "role": "upload",
                "archive_path": member,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "metadata": {},
            }
        ],
        "artifact_edges": [],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(member, payload)
    source_id, source_path = _upload(
        f, people.alice, people.ah, buffer.getvalue(), people.workspace_id
    )
    before = _content_counts(f)
    receipt = _admit(people.alice, people.ah, source_id)
    completed = _execute(f, receipt)
    assert completed.status == "failed" and "Unsafe bundle member" in completed.error_message
    assert _content_counts(f) == before and source_path.read_bytes() == buffer.getvalue()
    assert not (f.services.paths.root / "outside.txt").exists()
