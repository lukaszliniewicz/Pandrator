"""Current comparison admission, replay, MCP builders and registered workers."""

from __future__ import annotations

import hashlib
import json
import wave
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from flask import Request
from sqlalchemy import func, select, text

from pandrator.web import comparison_export_render, comparison_export_routes
from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.audio_assembly import assemble_audio_plan
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import Worker
from pandrator.web.openapi import build_openapi_document
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.settings_policy import stable_hash
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.comparison_exports import ComparisonExportInput
from pandrator_mcp.tools.comparison_exports import create_comparison_export
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token


def _services(case):
    return case.app.extensions["pandrator"]["services"]


def _inputs(case, client, auth, workspace=None):
    values = {"name": f"Shared comparison {uuid4()}", "workflow_kind": "audiobook"}
    if workspace is not None:
        values["workspace_id"] = workspace
    record = body(
        client.post("/api/v1/sessions", json=values, headers=headers(auth, str(uuid4()))),
        201,
    )
    plan = body(
        client.post(
            f"/api/v1/sessions/{record['id']}/generation-plan",
            json={"segments": [{"text": "A selected shared speech take."}]},
            headers=auth,
        ),
        201,
    )
    services = _services(case)
    with case.database.snapshot_session() as unit:
        segment = unit.scalar(
            select(m.GenerationSegment).where(
                m.GenerationSegment.plan_revision_id == plan["active_revision_id"]
            )
        )
        assert segment is not None
        segment_id = segment.id
    runs, takes, artifacts, paths = [], [], [], []
    for sequence, sample in ((1, 1000), (2, -2000)):
        path = services.paths.artifacts / f"comparison-input-{uuid4()}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as source:
            source.setnchannels(1)
            source.setsampwidth(2)
            source.setframerate(8000)
            source.writeframes(sample.to_bytes(2, "little", signed=True) * 800)
        with execution_actor_scope(HostActor("pandrator-cli")):
            artifact = services.artifacts.register(
                path, kind="audio", role="generation_take", session_id=record["id"]
            )
        with case.database.immediate_session() as unit:
            settings = {"tts": {"voice": f"synthetic-{sequence}"}}
            run = m.GenerationRun(
                session_id=record["id"],
                plan_revision_id=plan["active_revision_id"],
                sequence_number=sequence,
                status="completed",
                settings_hash=stable_hash(settings),
                settings_snapshot_json=settings,
            )
            unit.add(run)
            unit.flush()
            take = m.AudioTake(
                generation_segment_id=segment_id,
                generation_run_id=run.id,
                artifact_id=artifact.id,
                status="completed",
                kind="tts",
                duration_ms=100,
                is_active=sequence == 2,
            )
            unit.add(take)
            unit.flush()
            runs.append(run.id)
            takes.append(take.id)
        artifacts.append(artifact.id)
        paths.append(path)
    return SimpleNamespace(
        record=record,
        segment_id=segment_id,
        run_ids=runs,
        take_ids=takes,
        artifact_ids=artifacts,
        paths=paths,
        body={
            "clips": [{"generation_run_id": run_id, "segment_id": segment_id} for run_id in runs],
            "gap_ms": 2,
            "output_format": "wav",
        },
    )


def _uri(inputs):
    return f"/api/v1/sessions/{inputs.record['id']}/comparison-exports"


def _admit(client, auth, inputs, key):
    return client.post(_uri(inputs), json=inputs.body, headers=headers(auth, key))


def _run(case, receipt):
    services = _services(case)
    assert Worker(
        services.jobs, "workspace-comparison-http", services.workflow_handlers.handlers()
    ).run_once()
    return services.jobs.get(receipt["id"])


def _counts(case):
    with case.database.snapshot_session() as unit:
        return tuple(
            unit.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.Artifact, m.ArtifactEdge)
        )


def _remove_member(case, people, user_id):
    with case.database.immediate_session() as unit:
        unit.delete(unit.get_one(m.WorkspaceMembership, (people.workspace_id, user_id)))


def _observe_native(case, monkeypatch, user_id):
    original = assemble_audio_plan

    def assemble(*args, **kwargs):
        actor, lease = current_execution_actor.get(), current_job_lease.get()
        assert type(actor) is UserActor and actor.user_id == user_id
        assert lease is not None and callable(kwargs.get("before_io"))
        with case.database.snapshot_session() as unit:
            job = unit.get_one(m.Job, lease["job_id"])
            assert job.kind == "export.comparison"
            assert unit.get_one(m.JobActorRecord, job.id).user_id == user_id
        return original(*args, **kwargs)

    observed = Mock(side_effect=assemble)
    monkeypatch.setattr(comparison_export_render, "assemble_audio_plan", observed)
    return observed


def _check_pair(case, client, auth, inputs, completed):
    assert completed.status == "succeeded", completed.error_message
    result = completed.result_json
    audio_id, manifest_id = result["output_artifact_id"], result["manifest_artifact_id"]
    audio_bytes = client.get(f"/api/v1/artifacts/{audio_id}/content", headers=auth)
    assert audio_bytes.status_code == 200
    manifest_response = client.get(f"/api/v1/artifacts/{manifest_id}/content", headers=auth)
    assert manifest_response.status_code == 200
    manifest = json.loads(manifest_response.data)
    assert manifest["output_hash"] == hashlib.sha256(audio_bytes.data).hexdigest()
    assert [clip["take_id"] for clip in manifest["clips"]] == inputs.take_ids
    assert manifest["audio_source"] == "speech_take" and not manifest["sound_mix_included"]
    with case.database.snapshot_session() as unit:
        for artifact_id, role in (
            (audio_id, "comparison_export.audio"),
            (manifest_id, "comparison_export.manifest"),
        ):
            row = unit.get_one(m.Artifact, artifact_id)
            assert row.role == role and row.session_id == inputs.record["id"]
            assert row.project_id == inputs.record["project_id"]
            assert row.workspace_id == completed.workspace_id
            for parent_id in inputs.artifact_ids:
                assert unit.get(m.ArtifactEdge, (parent_id, artifact_id)) is not None
        assert unit.get(m.ArtifactEdge, (audio_id, manifest_id)) is not None
    return audio_bytes.data, manifest_response.data


@pytest.mark.parametrize("mode,output_format", [("solo", "wav"), ("team", "wav"), ("team", "mp3")])
def test_actual_native_solo_and_peer_after_creator_departure(
    case, monkeypatch, mode, output_format
):
    people = team(case, monkeypatch) if mode == "team" else None
    client, auth, user, workspace = (
        (people.alice, people.ah, people.alice_id, people.workspace_id)
        if people is not None
        else (case.client, {"X-CSRF-Token": case.csrf}, case.actor.user_id, None)
    )
    inputs = _inputs(case, client, auth, workspace)
    inputs.body["output_format"] = output_format
    original_bytes = [path.read_bytes() for path in inputs.paths]
    with case.database.immediate_session() as unit:
        for artifact_id in inputs.artifact_ids:
            unit.execute(
                text("UPDATE artifacts SET metadata_json = :invalid WHERE id = :id"),
                {"invalid": "{private-invalid", "id": artifact_id},
            )
    native = _observe_native(case, monkeypatch, user)
    key = str(uuid4())
    receipt = body(_admit(client, auth, inputs, key), 202)
    replay = _admit(client, auth, inputs, key)
    assert body(replay, 202) == receipt and replay.headers["Idempotency-Replayed"] == "true"
    completed = _run(case, receipt)
    first_bytes = _check_pair(case, client, auth, inputs, completed)
    assert native.call_count == 1
    if people is None:
        with case.database.snapshot_session() as unit:
            assert unit.scalar(select(func.count()).select_from(m.User)) == 1
    else:
        _remove_member(case, people, people.alice_id)
        _, credential = scoped_token(
            case,
            people.bob_id,
            {"app.read", "app.run"},
            project=inputs.record["project_id"],
        )
        peer = case.app.test_client()
        _observe_native(case, monkeypatch, people.bob_id)
        second = _run(case, body(_admit(peer, credential, inputs, str(uuid4())), 202))
        _check_pair(case, peer, credential, inputs, second)
        assert (
            second.result_json["output_artifact_id"] != completed.result_json["output_artifact_id"]
        )
        assert _check_pair(case, peer, credential, inputs, completed) == first_bytes
    assert [path.read_bytes() for path in inputs.paths] == original_bytes
    with case.database.snapshot_session() as unit:
        for artifact_id in inputs.artifact_ids:
            assert (
                unit.scalar(
                    text("SELECT metadata_json FROM artifacts WHERE id = :id"), {"id": artifact_id}
                )
                == "{private-invalid"
            )


class _HttpApplication(ApplicationClient):
    def __init__(self, client, auth):
        self.client, self.auth = client, auth
        self.calls: list[tuple[str, str]] = []

    def _request_json(self, path: str, **options: Any) -> dict[str, Any]:
        method = options.get("method", "GET")
        self.calls.append((method, path))
        credential = dict(self.auth)
        if options.get("idempotency_key"):
            credential["Idempotency-Key"] = options["idempotency_key"]
        response = self.client.open(
            path, method=method, json=options.get("body"), headers=credential
        )
        value = response.get_json()
        assert isinstance(value, dict)
        if response.status_code >= 400:
            raise PandratorMcpError("scope_denied", str(value.get("error")))
        return value


@pytest.mark.parametrize("selection", ["canonical", "convenience"])
def test_existing_mcp_builder_replays_one_actual_member_job(case, monkeypatch, selection):
    people = team(case, monkeypatch)
    inputs = _inputs(case, people.alice, people.ah, people.workspace_id)
    _, credential = scoped_token(
        case, people.bob_id, {"app.read", "app.run"}, project=inputs.record["project_id"]
    )
    client = case.app.test_client()
    application = _HttpApplication(client, credential)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    values = (
        {"clips": inputs.body["clips"]}
        if selection == "canonical"
        else {"generation_run_ids": inputs.run_ids, "segment_ids": [inputs.segment_id]}
    )
    arguments = ComparisonExportInput.model_validate(
        {"session_id": inputs.record["id"], "idempotency_key": str(uuid4()), "gap_ms": 2, **values}
    )
    first = create_comparison_export(runtime, arguments)
    second = create_comparison_export(runtime, arguments)
    assert first.result == second.result and first.work is not None
    assert application.calls == [("POST", _uri(inputs)), ("POST", _uri(inputs))]
    with case.database.snapshot_session() as unit:
        assert (
            unit.scalar(
                select(func.count()).select_from(m.Job).where(m.Job.kind == "export.comparison")
            )
            == 1
        )
    native = _observe_native(case, monkeypatch, people.bob_id)
    completed = _run(case, first.result)
    _check_pair(case, client, credential, inputs, completed)
    assert native.call_count == 1


@pytest.mark.parametrize("access", ["private", "viewer"])
def test_target_denial_precedes_json_and_replay(case, monkeypatch, access):
    people = team(case, monkeypatch)
    inputs = _inputs(case, people.alice, people.ah)
    if access == "viewer":
        with case.database.immediate_session() as unit:
            unit.add(
                ProjectMembership(
                    project_id=inputs.record["project_id"], user_id=people.bob_id, role="viewer"
                )
            )
    before = _counts(case)
    parse = Mock(side_effect=AssertionError("Denied comparison parsed its body"))
    monkeypatch.setattr(Request, "get_json", parse)
    response = _admit(people.bob, people.bh, inputs, str(uuid4()))
    assert response.status_code == (404 if access == "private" else 403)
    parse.assert_not_called()
    assert _counts(case) == before


@pytest.mark.parametrize("scopes", [{"app.read", "app.write"}, {"app.run"}])
def test_required_processing_scopes_precede_json(case, monkeypatch, scopes):
    people = team(case, monkeypatch)
    inputs = _inputs(case, people.alice, people.ah, people.workspace_id)
    _, credential = scoped_token(case, people.bob_id, scopes, project=inputs.record["project_id"])
    before = _counts(case)
    parse = Mock(side_effect=AssertionError("Missing comparison scope parsed its body"))
    monkeypatch.setattr(Request, "get_json", parse)
    response = _admit(case.app.test_client(), credential, inputs, str(uuid4()))
    assert response.status_code == 403
    parse.assert_not_called()
    assert _counts(case) == before


@pytest.mark.parametrize("boundary", ["after_json", "replay_after_json", "after_preparation"])
def test_current_member_is_rechecked_at_replay_and_admission_writer(case, monkeypatch, boundary):
    people = team(case, monkeypatch)
    inputs = _inputs(case, people.alice, people.ah, people.workspace_id)
    key = str(uuid4())
    if boundary == "replay_after_json":
        body(_admit(people.bob, people.bh, inputs, key), 202)
    before = _counts(case)
    if boundary == "after_preparation":
        original = comparison_export_routes.prepare_comparison_export

        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            _remove_member(case, people, people.bob_id)
            return result

        monkeypatch.setattr(comparison_export_routes, "prepare_comparison_export", prepare)
    else:
        original_json = Request.get_json

        def parse(request, *args, **kwargs):
            result = original_json(request, *args, **kwargs)
            _remove_member(case, people, people.bob_id)
            return result

        monkeypatch.setattr(Request, "get_json", parse)
    response = _admit(people.bob, people.bh, inputs, key)
    assert response.status_code == 404, response.get_json()
    assert _counts(case) == before


def test_queued_member_revocation_stops_registered_worker_before_native(case, monkeypatch):
    people = team(case, monkeypatch)
    inputs = _inputs(case, people.alice, people.ah, people.workspace_id)
    original = [path.read_bytes() for path in inputs.paths]
    receipt = body(_admit(people.alice, people.ah, inputs, str(uuid4())), 202)
    before = _counts(case)
    _remove_member(case, people, people.alice_id)
    native = Mock(side_effect=AssertionError("Revoked comparison entered native assembly"))
    monkeypatch.setattr(comparison_export_render, "assemble_audio_plan", native)
    completed = _run(case, receipt)
    assert completed.status == "failed"
    native.assert_not_called()
    assert _counts(case) == before and [path.read_bytes() for path in inputs.paths] == original


def test_existing_openapi_contract_describes_current_processing_scopes():
    operation = build_openapi_document()["paths"][
        "/api/v1/sessions/{sessionId}/comparison-exports"
    ]["post"]
    assert {"nativeOAuth": ["app.read", "app.run"]} in operation["security"]
    assert {"202", "403", "404", "409", "422"} <= operation["responses"].keys()
