"""Native speech optimization through authenticated HTTP, MCP builders and workers."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from flask import Request
from sqlalchemy import func, select, text

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import Worker
from pandrator.web.projects.models import ProjectMembership
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.workflow import ExecuteWorkflowPlanInput, PlanWorkflowInput
from pandrator_mcp.tools.workflow import execute_workflow_plan, plan_workflow
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import model, provider, scoped_token
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401

TEXT = "Dr. Jones can continue."
REVISED = "Doctor Jones can continue."


def _project(case, client, csrf, *, workspace=None, srt=False, stages=None):
    values = {
        "name": f"Shared speech {uuid4()}",
        "workflow_kind": "voiceover" if srt else "audiobook",
        "source_language": "en",
        "included_stages": stages or ["optimize_document"],
    }
    if workspace is not None:
        values["workspace_id"] = workspace
    return body(
        client.post("/api/v1/sessions", json=values, headers=headers(csrf, str(uuid4()))), 201
    )


def _source(case, record, *, srt=False):
    services = case.app.extensions["pandrator"]["services"]
    with case.database.snapshot_session() as unit:
        key = unit.get_one(m.SessionRecord, record["id"]).storage_key
    directory = services.paths.sessions / key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("source.srt" if srt else "prepared.json")
    path.write_text(
        f"1\n00:00:00,000 --> 00:00:01,000\n{TEXT}\n"
        if srt
        else json.dumps([{"text": TEXT, "language": "en"}]),
        encoding="utf-8",
    )
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = services.artifacts.register(
            path,
            kind="srt" if srt else "json",
            role="transcription" if srt else "prepared_text",
            session_id=record["id"],
        )
        if srt:
            services.workflow_handlers._store_srt_document(
                record["id"], artifact, "transcription", language="en"
            )
    return artifact.id, path


def _settings(client, csrf, *, workspace=None):
    connection = provider(client, csrf, workspace=workspace)
    model(client, csrf, connection, workspace=workspace)
    return {
        "tts_optimization_model": f"custom:{connection['id']}/synthetic-model",
        "llm_tts_document_optimization": True,
        "llm_tts_optimization": True,
        "llm_tts_document_batch_size": 1,
        "llm_concurrent_calls": 1,
        "speech_optimization_mode": "legacy",
        "apply_reviewed_pronunciations": False,
        "llm_tts_annotation_mode": "off",
        "language": "en",
    }


def _completion(case, monkeypatch, expected_user, *, kind="text.optimize_tts", effect=None):
    from pandrator.logic import llm_handler

    def complete(**kwargs):
        actor, lease = current_execution_actor.get(), current_job_lease.get()
        assert type(actor) is UserActor and actor.user_id == expected_user
        assert lease is not None
        with case.database.snapshot_session() as unit:
            assert unit.get_one(m.Job, lease["job_id"]).kind == kind
        request = json.loads(kwargs["messages"][-1]["content"].rsplit("Input JSON:\n", 1)[1])
        if effect is not None:
            effect()
        return {
            "id": "synthetic-native-speech-response",
            "model": kwargs["model"],
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "items": [
                                    {
                                        "index": row["index"],
                                        "text": row["text"].replace("Dr.", "Doctor"),
                                    }
                                    for row in request["items"]
                                ]
                            }
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 4},
                "private_request": "DO NOT RETAIN",
            },
            "_hidden_params": {"response_cost": 0.25},
        }

    native = Mock(side_effect=complete)
    monkeypatch.setattr(llm_handler, "_get_litellm_clients", lambda: (native, None))
    monkeypatch.setattr(llm_handler, "wait_for_retry", lambda *_args: True)
    return native


def _run(case, receipt):
    services = case.app.extensions["pandrator"]["services"]
    assert Worker(
        services.jobs, "native-speech-journey", services.workflow_handlers.handlers()
    ).run_once()
    return services.jobs.get(receipt["id"])


def _admit(client, csrf, sid, settings):
    return body(
        client.post(
            f"/api/v1/sessions/{sid}/stages/optimize_document/run", json=settings, headers=csrf
        ),
        202,
    )


def _usage(case, job_id, run_id):
    with case.database.snapshot_session() as unit:
        rows = list(unit.scalars(select(m.UsageEvent).where(m.UsageEvent.job_id == job_id)))
        assert len(rows) == 1
        assert rows[0].agent_run_id == run_id and rows[0].stage == "tts_optimization"
        assert (rows[0].input_tokens, rows[0].cached_input_tokens, rows[0].output_tokens) == (
            12,
            4,
            3,
        )
        assert rows[0].cost_usd == 0.25
        assert "DO NOT RETAIN" not in json.dumps(rows[0].raw_usage_json)


@pytest.mark.parametrize("mode,srt", [("solo", False), ("team", False), ("team", True)])
def test_normal_stage_worker_and_remaining_member_retain_owned_speech(case, monkeypatch, mode, srt):
    if mode == "team":
        people = team(case, monkeypatch)
        client, csrf, user, workspace = (
            people.alice,
            people.ah,
            people.alice_id,
            people.workspace_id,
        )
        settings = _settings(people.owner, people.oh, workspace=workspace)
    else:
        people = None
        client, csrf, user, workspace = (
            case.client,
            {"X-CSRF-Token": case.csrf},
            case.actor.user_id,
            None,
        )
        settings = _settings(client, csrf)
        with case.database.snapshot_session() as unit:
            assert unit.scalar(select(func.count()).select_from(m.User)) == 1
    record = _project(case, client, csrf, workspace=workspace, srt=srt)
    source_id, source_path = _source(case, record, srt=srt)
    original = source_path.read_bytes()
    native = _completion(case, monkeypatch, user)
    if people is not None:
        _, credential = scoped_token(case, user, {"app.read", "app.run"}, workspace=workspace)
        client = case.app.test_client()
    else:
        credential = csrf
    completed = _run(case, _admit(client, credential, record["id"], settings))
    assert completed.status == "succeeded", completed.error_message
    assert native.call_count == 1
    result = completed.result_json
    _usage(case, completed.id, result["agent_run_id"])
    with case.database.snapshot_session() as unit:
        output = unit.get_one(m.Artifact, result["artifact_id"])
        run = unit.get_one(m.AgentRun, result["agent_run_id"])
        assert output.project_id == record["project_id"] and output.session_id == record["id"]
        assert output.role == "tts_optimized" and run.result_artifact_id == output.id
        assert run.status == "completed" and run.job_id == completed.id
        assert unit.get(m.ArtifactEdge, (source_id, output.id)) is not None
        assert unit.get_one(m.JobActorRecord, completed.id).user_id == user
        if srt:
            assert unit.get(m.DocumentRevision, output.metadata_json["revision_id"]) is not None
            assert unit.scalar(select(func.count()).select_from(m.SegmentLineage)) == 1
    assert (
        REVISED
        in client.get(f"/api/v1/artifacts/{result['artifact_id']}/content", headers=credential).text
    )
    assert source_path.read_bytes() == original
    if people is not None:
        with case.database.immediate_session() as unit:
            unit.delete(unit.get_one(m.WorkspaceMembership, (workspace, user)))
        _completion(case, monkeypatch, people.bob_id)
        second = _run(case, _admit(people.bob, people.bh, record["id"], settings))
        assert second.status == "succeeded", second.error_message
        assert second.result_json["artifact_id"] != result["artifact_id"]
        assert (
            people.bob.get(f"/api/v1/artifacts/{result['artifact_id']}/content").status_code == 200
        )
        assert (
            people.alice.get(f"/api/v1/artifacts/{result['artifact_id']}/content").status_code
            == 404
        )
        assert source_path.read_bytes() == original


@pytest.mark.parametrize("viewer", [False, True])
def test_stage_denies_before_json_or_resolution(case, monkeypatch, viewer):
    people = team(case, monkeypatch)
    record = _project(case, people.alice, people.ah)
    if viewer:
        with case.database.immediate_session() as unit:
            unit.add(
                ProjectMembership(
                    project_id=record["project_id"], user_id=people.bob_id, role="viewer"
                )
            )
    services = case.app.extensions["pandrator"]["services"]
    parse = Mock(side_effect=AssertionError("Denied stage parsed its body"))
    resolve = Mock(side_effect=AssertionError("Denied stage resolved content"))
    with monkeypatch.context() as scoped:
        scoped.setattr(Request, "get_json", parse)
        scoped.setattr(services.workflows, "resolve_stage", resolve)
        response = people.bob.post(
            f"/api/v1/sessions/{record['id']}/stages/optimize_document/run",
            data="{not-json",
            content_type="application/json",
            headers=people.bh,
        )
    assert response.status_code == (403 if viewer else 404)
    parse.assert_not_called()
    resolve.assert_not_called()


def test_stage_writer_rechecks_actual_source_before_enqueue(case, monkeypatch):
    services = case.app.extensions["pandrator"]["services"]
    csrf = {"X-CSRF-Token": case.csrf}
    record = _project(case, case.client, csrf)
    source_id, _path = _source(case, record)
    settings = _settings(case.client, csrf)
    resolve = services.workflows.resolve_stage

    def change_after_resolution(*args, **kwargs):
        resolved = resolve(*args, **kwargs)
        with case.database.immediate_session() as unit:
            unit.get_one(m.Artifact, source_id).state = "deleted"
        return resolved

    monkeypatch.setattr(services.workflows, "resolve_stage", change_after_resolution)
    response = case.client.post(
        f"/api/v1/sessions/{record['id']}/stages/optimize_document/run", json=settings, headers=csrf
    )
    assert response.status_code == 404
    with case.database.snapshot_session() as unit:
        assert unit.scalar(select(func.count()).select_from(m.Job)) == 0
        assert unit.scalar(select(func.count()).select_from(m.AgentRun)) == 0


def test_project_credential_uses_attached_source_bytes_without_origin_metadata(case, monkeypatch):
    people = team(case, monkeypatch)
    services = case.app.extensions["pandrator"]["services"]
    origin = _project(case, people.alice, people.ah, workspace=people.workspace_id)
    target = _project(case, people.alice, people.ah, workspace=people.workspace_id)
    source_id, path = _source(case, origin)
    original = path.read_bytes()
    settings = _settings(people.owner, people.oh, workspace=people.workspace_id)
    with execution_actor_scope(HostActor("pandrator-cli")):
        asset = services.source_library.ensure_for_artifact(
            source_id, display_name="Attached narration", kind="text"
        )
        services.source_library.attach(target["id"], asset.id)
    with case.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE artifacts SET metadata_json = '{broken' WHERE id = :id"),
            {"id": source_id},
        )
        unit.execute(
            text("UPDATE source_assets SET metadata_json = '{broken' WHERE id = :id"),
            {"id": asset.id},
        )
    _, credential = scoped_token(
        case, people.bob_id, {"app.read", "app.run"}, project=target["project_id"]
    )
    client = case.app.test_client()
    assert (
        client.get(f"/api/v1/artifacts/{source_id}/context", headers=credential).status_code == 404
    )
    assert client.get(f"/api/v1/sessions/{origin['id']}", headers=credential).status_code == 404
    assert client.get(f"/api/v1/artifacts/{source_id}/content", headers=credential).data == original
    assert (
        client.get(
            f"/api/v1/sessions/{target['id']}/workflow?view=summary", headers=credential
        ).status_code
        == 200
    )
    native = _completion(case, monkeypatch, people.bob_id)
    completed = _run(case, _admit(client, credential, target["id"], settings))
    assert completed.status == "succeeded", completed.error_message
    assert native.call_count == 1
    output_id = completed.result_json["artifact_id"]
    assert REVISED in client.get(f"/api/v1/artifacts/{output_id}/content", headers=credential).text
    assert path.read_bytes() == original
    application = _HttpApplication(client, credential)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    planned = plan_workflow(
        runtime,
        PlanWorkflowInput(
            session_id=target["id"],
            target_stage="optimize_document",
            overrides={"text": settings},
            continuation=False,
        ),
    )
    assert planned.next_actions
    executed = execute_workflow_plan(
        runtime, ExecuteWorkflowPlanInput(**planned.next_actions[0].arguments)
    )
    assert executed.work is not None
    via_mcp = _run(case, {"id": executed.work.id})
    assert via_mcp.status == "succeeded", via_mcp.error_message
    assert native.call_count == 2
    _usage(case, via_mcp.id, via_mcp.result_json["agent_run_id"])
    with case.database.snapshot_session() as unit:
        for table, identifier in (("artifacts", source_id), ("source_assets", asset.id)):
            assert (
                unit.scalar(
                    text(f"SELECT metadata_json FROM {table} WHERE id = :id"), {"id": identifier}
                )
                == "{broken"
            )


def test_role_loss_after_response_retains_usage_and_stops_publication(case, monkeypatch):
    people = team(case, monkeypatch)
    record = _project(case, people.alice, people.ah, workspace=people.workspace_id)
    _source_id, path = _source(case, record)
    original = path.read_bytes()
    settings = _settings(people.owner, people.oh, workspace=people.workspace_id)

    def revoke():
        with case.database.immediate_session() as unit:
            unit.delete(unit.get_one(m.WorkspaceMembership, (people.workspace_id, people.alice_id)))

    native = _completion(case, monkeypatch, people.alice_id, effect=revoke)
    completed = _run(case, _admit(people.alice, people.ah, record["id"], settings))
    assert completed.status == "failed" and native.call_count == 1
    with case.database.snapshot_session() as unit:
        run = unit.scalar(select(m.AgentRun).where(m.AgentRun.job_id == completed.id))
        assert run is not None and run.status == "failed" and run.result_artifact_id is None
        run_id = run.id
        assert (
            unit.scalar(
                select(func.count())
                .select_from(m.Artifact)
                .where(m.Artifact.role == "tts_optimized")
            )
            == 0
        )
    _usage(case, completed.id, run_id)
    assert path.read_bytes() == original


class _HttpApplication(ApplicationClient):
    def __init__(self, client, csrf):
        self.client, self.csrf = client, csrf
        self.calls: list[tuple[str, str]] = []

    def _request_json(self, path: str, **options: Any) -> dict[str, Any]:
        method = options.get("method", "GET")
        self.calls.append((method, path))
        credential = dict(self.csrf)
        if options.get("idempotency_key"):
            credential["Idempotency-Key"] = options["idempotency_key"]
        response = self.client.open(
            path,
            method=method,
            query_string=options.get("parameters"),
            json=options.get("body"),
            headers=credential,
        )
        if response.status_code >= 400:
            failure = response.get_json()
            assert isinstance(failure, dict)
            error = failure.get("error")
            assert isinstance(error, dict)
            raise PandratorMcpError(
                "scope_denied",
                f"{error.get('code')}: {error.get('message')}",
            )
        result = response.get_json()
        assert isinstance(result, dict)
        return result


def test_existing_mcp_plan_executes_optimizer_under_actual_continuation_lease(case, monkeypatch):
    people = team(case, monkeypatch)
    record = _project(
        case,
        people.alice,
        people.ah,
        workspace=people.workspace_id,
        stages=["optimize_document"],
    )
    body(
        people.alice.post(
            "/api/v1/uploads",
            data={"session_id": record["id"], "file": (io.BytesIO(TEXT.encode()), "chapter.txt")},
            headers=headers(people.ah, str(uuid4())),
        ),
        201,
    )
    services = case.app.extensions["pandrator"]["services"]
    with case.database.snapshot_session() as unit:
        source = unit.scalar(
            select(m.Artifact).where(
                m.Artifact.session_id == record["id"],
                m.Artifact.role == "upload",
            )
        )
        assert source is not None
        path = services.paths.managed_path(source.relative_path)
    original = path.read_bytes()
    settings = _settings(people.owner, people.oh, workspace=people.workspace_id)
    with case.database.immediate_session() as unit:
        unit.delete(unit.get_one(m.WorkspaceMembership, (people.workspace_id, people.alice_id)))
    application = _HttpApplication(people.bob, people.bh)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    monkeypatch.setattr(
        "pandrator.logic.text_preprocessor.preprocess_text",
        lambda *_args, **_kwargs: [{"text": TEXT}],
    )
    for stage, values in (
        ("clean_source", {"agentic": False}),
        ("prepare_text", {"enable_nemo_normalization": False}),
    ):
        admitted = body(
            people.bob.post(
                f"/api/v1/sessions/{record['id']}/stages/{stage}/run",
                json=values,
                headers=people.bh,
            ),
            202,
        )
        prepared = _run(case, admitted)
        assert prepared.status == "succeeded", prepared.error_message
    planned = plan_workflow(
        runtime,
        PlanWorkflowInput(
            session_id=record["id"],
            target_stage="optimize_document",
            overrides={
                "text": settings,
                "stage_settings": {
                    "clean_source": {"agentic": False},
                    "prepare_text": {"enable_nemo_normalization": False},
                },
            },
        ),
    )
    assert planned.next_actions
    assert isinstance(planned.result, dict)
    native = _completion(case, monkeypatch, people.bob_id, kind="workflow.continue")
    executed = execute_workflow_plan(
        runtime, ExecuteWorkflowPlanInput(**planned.next_actions[0].arguments)
    )
    assert executed.work is not None
    completed = _run(case, {"id": executed.work.id})
    assert completed.kind == "workflow.continue" and completed.status == "succeeded", (
        completed.error_message
    )
    assert native.call_count == 1
    artifacts = completed.result_json["artifacts"]
    assert [item["stage"] for item in artifacts] == ["optimize_document"]
    output = artifacts[-1]
    assert REVISED in people.bob.get(f"/api/v1/artifacts/{output['artifact_id']}/content").text
    _usage(case, completed.id, output["agent_run_id"])
    assert (
        "POST",
        f"/api/v1/workflow-plans/{planned.result['plan_id']}/execute",
    ) in application.calls
    assert path.read_bytes() == original
