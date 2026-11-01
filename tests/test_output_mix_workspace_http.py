"""Current soundtrack previews through HTTP, native media and registered workers."""

from __future__ import annotations

import hashlib
import wave
from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask.wrappers import Request
from sqlalchemy import func, select, text

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.artifacts import ArtifactService
from pandrator.web.jobs import Worker
from pandrator.web.openapi import build_openapi_document
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token


def _fixture(case):
    return SimpleNamespace(case=case, services=case.app.extensions["pandrator"]["services"])


def _project(client, auth, workspace=None):
    values = {"name": f"Soundtrack {uuid4()}", "workflow_kind": "voiceover"}
    if workspace is not None:
        values["workspace_id"] = workspace
    return body(
        client.post("/api/v1/sessions", json=values, headers=headers(auth, str(uuid4()))), 201
    )


def _wave(f, record, *, role):
    folder = (
        f.services.paths.uploads
        if role == "upload"
        else f.services.paths.sessions / record["storage_key"]
    )
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid4()}.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes((b"\x10\x27" * 20 + b"\xf0\xd8" * 20) * 400)
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path,
            kind="audio",
            role=role,
            session_id=record["id"],
            metadata={"takes": [{"target_start_ms": 0}]} if role != "upload" else {},
        )
    return artifact, path


def _setup(f, client, auth, workspace=None):
    record = _project(client, auth, workspace)
    source, source_path = _wave(f, record, role="upload")
    plan = body(
        client.post(
            f"/api/v1/sessions/{record['id']}/generation-plan",
            json={"segments": [{"text": "A shared voiceover."}]},
            headers=auth,
        ),
        201,
    )
    dubbing, dubbing_path = _wave(f, record, role="output_assembly")
    with f.case.database.immediate_session() as unit:
        run = m.GenerationRun(
            session_id=record["id"],
            plan_revision_id=plan["active_revision_id"],
            sequence_number=1,
            status="completed",
        )
        unit.add(run)
        unit.flush()
        run_id = run.id
        unit.add(
            m.OutputAssembly(
                session_id=record["id"],
                generation_run_id=run.id,
                artifact_id=dubbing.id,
                status="completed",
            )
        )
    return SimpleNamespace(
        record=record,
        source=source,
        source_path=source_path,
        dubbing=dubbing,
        dubbing_path=dubbing_path,
        run_id=run_id,
    )


def _admit(client, auth, inputs, *, start_seconds=None):
    return body(
        client.post(
            f"/api/v1/sessions/{inputs.record['id']}/output-mix-preview",
            json={
                "generation_run_id": inputs.run_id,
                "duration_seconds": 4,
                "start_seconds": start_seconds,
            },
            headers=auth,
        ),
        202,
    )


def _execute(f, receipt):
    assert Worker(
        f.services.jobs, "current-soundtrack-preview", f.services.workflow_handlers.handlers()
    ).run_once()
    return f.services.jobs.get(receipt["id"])


def _file(f, artifact_id):
    with f.case.database.snapshot_session() as unit:
        row = unit.get_one(m.Artifact, artifact_id)
        return f.services.paths.managed_path(row.relative_path), row.content_hash


def _counts(f):
    with f.case.database.snapshot_session() as unit:
        return tuple(
            unit.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.Artifact, m.ArtifactEdge)
        )


@pytest.mark.parametrize("mode", ["solo", "team"])
def test_native_solo_and_remaining_member_previews_keep_prior_bytes_and_current_actor(
    case, monkeypatch, mode
):
    f = _fixture(case)
    people = team(case, monkeypatch) if mode == "team" else None
    client, auth, user_id, workspace = (
        (people.alice, people.ah, people.alice_id, people.workspace_id)
        if people is not None
        else (case.client, {"X-CSRF-Token": case.csrf}, case.actor.user_id, None)
    )
    inputs = _setup(f, client, auth, workspace)
    source_bytes, dubbing_bytes = inputs.source_path.read_bytes(), inputs.dubbing_path.read_bytes()
    if people is None:
        with case.database.immediate_session() as unit:
            unit.execute(
                text("UPDATE artifacts SET metadata_json = :invalid WHERE id = :id"),
                {"invalid": "not-json", "id": inputs.dubbing.id},
            )
    if people is not None:
        with case.database.immediate_session() as unit:
            unit.delete(unit.get_one(WorkspaceMembership, (workspace, people.alice_id)))
        _, auth = scoped_token(
            case, people.bob_id, {"app.read", "app.run"}, project=inputs.record["project_id"]
        )
        client, user_id = case.app.test_client(), people.bob_id
    outputs = []
    for _ in range(2):
        receipt = _admit(client, auth, inputs, start_seconds=0 if people is None else None)
        completed = _execute(f, receipt)
        assert completed.status == "succeeded", completed.error_message
        artifact_id = completed.result_json["artifact_id"]
        path, digest = _file(f, artifact_id)
        value = path.read_bytes()
        assert hashlib.sha256(value).hexdigest() == digest
        assert completed.result_json["automatic_start_method"] == (
            "manual" if people is None else "assembly_timeline"
        )
        assert client.get(f"/api/v1/artifacts/{artifact_id}/content", headers=auth).data == value
        with wave.open(str(path)) as audio:
            assert audio.getnframes() > 0
        with case.database.snapshot_session() as unit:
            artifact = unit.get_one(m.Artifact, artifact_id)
            assert artifact.project_id == inputs.record["project_id"]
            assert artifact.workspace_id == unit.get_one(m.Job, receipt["id"]).workspace_id
            assert unit.get_one(m.JobActorRecord, receipt["id"]).user_id == user_id
            assert unit.get(m.ArtifactEdge, (inputs.source.id, artifact_id)) is not None
            assert unit.get(m.ArtifactEdge, (inputs.dubbing.id, artifact_id)) is not None
            if people is None:
                assert unit.scalar(select(func.count()).select_from(m.User)) == 1
        outputs.append((artifact_id, path, value))
    assert outputs[0][0] != outputs[1][0] and outputs[0][1] != outputs[1][1]
    assert all(path.read_bytes() == value for _, path, value in outputs)
    assert inputs.source_path.read_bytes() == source_bytes
    assert inputs.dubbing_path.read_bytes() == dubbing_bytes


def test_attached_source_with_invalid_origin_metadata_works_for_project_only_token(
    case, monkeypatch
):
    f, people = _fixture(case), team(case, monkeypatch)
    inputs = _setup(f, people.alice, people.ah, people.workspace_id)
    origin = _project(people.bob, people.bh, people.workspace_id)
    selected, source_path = _wave(f, origin, role="upload")
    source_bytes = source_path.read_bytes()
    with execution_actor_scope(HostActor("pandrator-cli")):
        asset = f.services.source_library.ensure_for_artifact(
            selected.id, display_name=source_path.name, kind="audio"
        )
        f.services.source_library.attach(inputs.record["id"], asset.id)
    with case.database.immediate_session() as unit:
        for table, identity in (("artifacts", selected.id), ("source_assets", asset.id)):
            unit.execute(
                text(f"UPDATE {table} SET metadata_json = :invalid WHERE id = :id"),
                {"invalid": "not-json", "id": identity},
            )
    _, auth = scoped_token(
        case, people.alice_id, {"app.read", "app.run"}, project=inputs.record["project_id"]
    )
    client = case.app.test_client()
    receipt = _admit(client, auth, inputs)
    with case.database.snapshot_session() as unit:
        assert unit.get_one(m.Job, receipt["id"]).payload_json["source_artifact_id"] == selected.id
    completed = _execute(f, receipt)
    assert completed.status == "succeeded", completed.error_message
    path, _digest = _file(f, completed.result_json["artifact_id"])
    assert path.is_file() and source_path.read_bytes() == source_bytes


@pytest.mark.parametrize("caller", ["foreign", "administrator", "viewer"])
def test_preview_target_denial_precedes_json(case, monkeypatch, caller):
    f, people = _fixture(case), team(case, monkeypatch)
    record = _project(people.alice, people.ah)
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
        pytest.fail("Denied preview reached request JSON")

    monkeypatch.setattr(Request, "get_json", forbidden)
    assert (
        client.post(
            f"/api/v1/sessions/{record['id']}/output-mix-preview", json={}, headers=auth
        ).status_code
        == expected
    )
    assert _counts(f) == before


def test_preview_processing_scope_denial_precedes_json(case, monkeypatch):
    f, people = _fixture(case), team(case, monkeypatch)
    record = _project(people.alice, people.ah, people.workspace_id)
    _, auth = scoped_token(
        case, people.bob_id, {"app.read", "app.write"}, project=record["project_id"]
    )
    before = _counts(f)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Narrow preview credential reached JSON")

    monkeypatch.setattr(Request, "get_json", forbidden)
    assert (
        case.app.test_client()
        .post(f"/api/v1/sessions/{record['id']}/output-mix-preview", json={}, headers=auth)
        .status_code
        == 403
    )
    assert _counts(f) == before


def test_preview_admission_rechecks_membership_after_json(case, monkeypatch):
    f, people = _fixture(case), team(case, monkeypatch)
    inputs = _setup(f, people.alice, people.ah, people.workspace_id)
    before = _counts(f)
    parse, changed = Request.get_json, []

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
    response = people.alice.post(
        f"/api/v1/sessions/{inputs.record['id']}/output-mix-preview",
        json={"generation_run_id": inputs.run_id},
        headers=people.ah,
    )
    assert response.status_code == 404 and changed and _counts(f) == before


@pytest.mark.parametrize("failure", ["registration", "membership"])
def test_native_preview_failure_preserves_prior_output_and_inputs(case, monkeypatch, failure):
    f, people = _fixture(case), team(case, monkeypatch)
    inputs = _setup(f, people.alice, people.ah, people.workspace_id)
    prior = _execute(f, _admit(people.bob, people.bh, inputs))
    assert prior.status == "succeeded", prior.error_message
    old_path, _digest = _file(f, prior.result_json["artifact_id"])
    old_bytes = old_path.read_bytes()
    receipt = _admit(people.bob, people.bh, inputs)
    before = _counts(f)
    before_files = {path for path in f.services.paths.sessions.rglob("*") if path.is_file()}
    changed = []
    if failure == "registration":
        register = ArtifactService.register_in_session

        def fail_after_insert(self, unit, output, **kwargs):
            register(self, unit, output, **kwargs)
            changed.append(output)
            raise RuntimeError("Synthetic mix preview publication failure")

        monkeypatch.setattr(ArtifactService, "register_in_session", fail_after_insert)
    else:
        prepare = ArtifactService.prepare_registration

        def leave_after_preparation(self, output, **kwargs):
            value = prepare(self, output, **kwargs)
            if not changed:
                with case.database.immediate_session() as unit:
                    unit.delete(
                        unit.get_one(WorkspaceMembership, (people.workspace_id, people.bob_id))
                    )
                changed.append(output)
            return value

        monkeypatch.setattr(ArtifactService, "prepare_registration", leave_after_preparation)
    completed = _execute(f, receipt)
    assert completed.status == "failed" and changed and _counts(f) == before
    assert old_path.read_bytes() == old_bytes
    assert {path for path in f.services.paths.sessions.rglob("*") if path.is_file()} == before_files


def test_mix_preview_openapi_describes_processing_scopes_and_safe_denials():
    operation = build_openapi_document()["paths"][
        "/api/v1/sessions/{sessionId}/output-mix-preview"
    ]["post"]
    assert operation["x-required-scopes"] == ["app.read", "app.run"]
    assert operation["security"][-1] == {"nativeOAuth": ["app.read", "app.run"]}
    assert set(operation["responses"]) == {"202", "403", "404", "409"}
