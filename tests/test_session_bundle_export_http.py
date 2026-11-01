"""Registered archive workers retain current workspace and immutable output rights."""

from __future__ import annotations

import json
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from pandrator.web import bundles
from pandrator.web import models as m
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
)
from pandrator.web.jobs import Worker
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_source_cleaning_workspace_http import book, source
from tests.test_source_preparation_workspace_access import _attach


def _admit(client, auth, session_id, *, include_sources=True):
    return body(
        client.post(
            f"/api/v1/sessions/{session_id}/bundle",
            json={"include_sources": include_sources},
            headers={**auth, "Idempotency-Key": str(uuid4())},
        ),
        202,
    )


def _execute(f, receipt):
    assert Worker(
        f.services.jobs, "current-archive-export", f.services.workflow_handlers.handlers()
    ).run_once()
    return f.services.jobs.get(receipt["id"])


def _output(f, job):
    assert job.status == "succeeded", job.error_message
    with f.case.database.snapshot_session() as unit:
        artifact = unit.get_one(m.Artifact, job.result_json["artifact_id"])
        assert artifact.role == "session_bundle"
        return artifact.id, f.services.paths.managed_path(artifact.relative_path)


@pytest.mark.parametrize("mode", ["solo", "team"])
def test_real_archive_worker_repeated_export_and_shared_creator_departure(case, monkeypatch, mode):
    f = SimpleNamespace(case=case, services=case.app.extensions["pandrator"]["services"])
    people = team(case, monkeypatch) if mode == "team" else None
    client, auth, user_id, workspace = (
        (people.alice, people.ah, people.alice_id, people.workspace_id)
        if people is not None
        else (case.client, {"X-CSRF-Token": case.csrf}, case.actor.user_id, None)
    )
    record = book(f, client, auth, workspace)
    original, original_path = source(f, record)
    original_bytes = original_path.read_bytes()
    _, run_auth = scoped_token(case, user_id, {"app.read", "app.run"}, project=record["project_id"])
    api = case.app.test_client()
    first = _execute(f, _admit(api, run_auth, record["id"]))
    first_id, first_path = _output(f, first)
    first_bytes = first_path.read_bytes()
    with zipfile.ZipFile(first_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        entry = next(item for item in manifest["artifacts"] if item["source_id"] == original.id)
        assert archive.read(entry["archive_path"]) == original_bytes
        assert entry["metadata"]["original_filename"] == "narration.txt"
    if people is not None:
        with case.database.immediate_session() as unit:
            unit.delete(unit.get_one(WorkspaceMembership, (workspace, people.alice_id)))
        assert people.alice.get(f"/api/v1/artifacts/{first_id}/content").status_code == 404
        _, run_auth = scoped_token(
            case, people.bob_id, {"app.read", "app.run"}, project=record["project_id"]
        )
        user_id = people.bob_id
    second = _execute(f, _admit(api, run_auth, record["id"]))
    second_id, second_path = _output(f, second)
    assert second_id != first_id and second_path != first_path
    assert first_path.read_bytes() == first_bytes and original_path.read_bytes() == original_bytes
    with case.database.snapshot_session() as unit:
        assert unit.get_one(m.JobActorRecord, second.id).user_id == user_id
        root = unit.get_one(m.Artifact, second_id)
        assert root.project_id == record["project_id"]
    response = api.get(f"/api/v1/artifacts/{second_id}/content", headers=run_auth)
    assert response.status_code == 200 and response.data == second_path.read_bytes()


def test_registered_archive_exports_attached_bytes_without_origin_metadata(case, monkeypatch):
    f = SimpleNamespace(case=case, services=case.app.extensions["pandrator"]["services"])
    people = team(case, monkeypatch)
    origin = book(f, people.alice, people.ah, people.workspace_id)
    target = book(f, people.alice, people.ah, people.workspace_id)
    original, original_path = source(f, origin)
    _attach(f, target["id"], original)
    with case.database.immediate_session() as unit:
        unit.execute(
            text(
                "UPDATE artifacts SET metadata_json = :invalid, settings_hash = :private WHERE id = :id"
            ),
            {"invalid": "not-json", "private": "origin-settings-fingerprint", "id": original.id},
        )
    _, run_auth = scoped_token(
        case, people.alice_id, {"app.read", "app.run"}, project=target["project_id"]
    )
    api = case.app.test_client()
    _artifact_id, output = _output(f, _execute(f, _admit(api, run_auth, target["id"])))
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        entry = next(item for item in manifest["artifacts"] if item["source_id"] == original.id)
        assert entry["metadata"] == {} and entry["settings_hash"] is None
        assert archive.read(entry["archive_path"]) == original_path.read_bytes()


def test_sources_excluded_from_archive_do_not_load_source_metadata(case, monkeypatch):
    f = SimpleNamespace(case=case, services=case.app.extensions["pandrator"]["services"])
    people = team(case, monkeypatch)
    target = book(f, people.alice, people.ah, people.workspace_id)
    original, original_path = source(f, target)
    source_bytes = original_path.read_bytes()
    with case.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE artifacts SET metadata_json = :invalid WHERE id = :id"),
            {"invalid": "not-json", "id": original.id},
        )
    _, run_auth = scoped_token(
        case, people.alice_id, {"app.read", "app.run"}, project=target["project_id"]
    )
    receipt = _admit(case.app.test_client(), run_auth, target["id"], include_sources=False)
    _artifact_id, output = _output(f, _execute(f, receipt))
    with zipfile.ZipFile(output) as archive:
        assert json.loads(archive.read("manifest.json"))["artifacts"] == []
    assert original_path.read_bytes() == source_bytes


@pytest.mark.parametrize("loss", ["membership", "origin_metadata"])
def test_archive_permission_loss_after_final_hash_preserves_prior_output(case, monkeypatch, loss):
    f = SimpleNamespace(case=case, services=case.app.extensions["pandrator"]["services"])
    people = team(case, monkeypatch)
    origin = book(f, people.alice, people.ah, people.workspace_id)
    original, original_path = source(f, origin)
    target = (
        origin if loss == "membership" else book(f, people.alice, people.ah, people.workspace_id)
    )
    if target is not origin:
        _attach(f, target["id"], original)
    token, run_auth = scoped_token(
        case, people.alice_id, {"app.read", "app.run"}, workspace=people.workspace_id
    )
    api = case.app.test_client()
    first_id, first_path = _output(f, _execute(f, _admit(api, run_auth, target["id"])))
    first_bytes, source_bytes = first_path.read_bytes(), original_path.read_bytes()
    before = set()
    with case.database.snapshot_session() as unit:
        before = set(unit.scalars(select(m.Artifact.id)))
    original_hash = bundles.sha256_file
    mutations = []

    def revoke_after_archive_hash(path):
        digest = original_hash(path)
        if zipfile.is_zipfile(path) and not mutations:
            with case.database.immediate_session() as unit:
                if loss == "membership":
                    unit.delete(
                        unit.get_one(WorkspaceMembership, (people.workspace_id, people.alice_id))
                    )
                else:
                    unit.execute(
                        delete(ApiTokenWorkspaceGrant).where(
                            ApiTokenWorkspaceGrant.token_id == token.id
                        )
                    )
                    unit.add(
                        ApiTokenProjectGrant(token_id=token.id, project_id=target["project_id"])
                    )
            mutations.append(path)
        return digest

    receipt = _admit(api, run_auth, target["id"])
    monkeypatch.setattr(bundles, "sha256_file", revoke_after_archive_hash)
    result = _execute(f, receipt)
    assert mutations and result.status == "failed"
    assert first_path.read_bytes() == first_bytes and original_path.read_bytes() == source_bytes
    with case.database.snapshot_session() as unit:
        assert set(unit.scalars(select(m.Artifact.id))) == before
        assert unit.get_one(m.Artifact, first_id).state != "deleted"
    assert not mutations[0].exists()
