"""Registered source workers retain team ownership, accounting and atomic output."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import (
    HostActor,
    actor_from_principal,
    execution_actor_scope,
)
from pandrator.web.jobs import Worker
from pandrator.web.settings_policy import stable_hash
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import model, provider, scoped_token
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401

TEXT = "Shared narration."


@pytest.fixture
def cleaning(case, monkeypatch):
    return SimpleNamespace(
        case=case, u=team(case, monkeypatch), services=case.app.extensions["pandrator"]["services"]
    )


def book(f, client, csrf, workspace=None):
    values = {
        "name": f"Source cleaning {uuid4()}",
        "workflow_kind": "audiobook",
        "source_language": "en",
    }
    if workspace is not None:
        values["workspace_id"] = workspace
    return body(
        client.post("/api/v1/sessions", json=values, headers=headers(csrf, str(uuid4()))), 201
    )


def source(f, record):
    path = f.services.paths.uploads / f"{uuid4()}.txt"
    path.write_text(TEXT)
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path,
            kind="text",
            role="upload",
            session_id=record["id"],
            metadata={"original_filename": "narration.txt"},
        )
        asset = f.services.source_library.ensure_for_artifact(
            artifact.id, display_name="Narration", kind="text"
        )
        f.services.source_library.attach(record["id"], asset.id)
    return artifact, path


def execute(f, receipt):
    assert Worker(
        f.services.jobs, "named-source-cleaning", f.services.workflow_handlers.handlers()
    ).run_once()
    return f.services.jobs.get(receipt.get("job_id", receipt.get("id")))


def artifact_count(f):
    with f.case.database.snapshot_session() as unit:
        return unit.scalar(select(func.count()).select_from(m.Artifact))


def approved_model(f):
    p = provider(f.u.owner, f.u.oh, workspace=f.u.workspace_id)
    model(f.u.owner, f.u.oh, p, workspace=f.u.workspace_id)
    return p, f"custom:{p['id']}/synthetic-model"


def agentic_job(f, record, artifact, selected):
    # This case qualifies native execution. Agent-run management route/read
    # authority has its own remaining operation-map entry.
    return body(
        f.u.alice.post(
            f"/api/v1/sessions/{record['id']}/agent-runs",
            json={
                "source_artifact_id": artifact.id,
                "settings": {
                    "model_name": selected,
                    "phase_names": ["metadata"],
                    "max_iterations": 1,
                },
            },
            headers=f.u.ah,
        ),
        202,
    )


def native_pipeline(monkeypatch, *, on_response=None, empty=False):
    from pandrator.logic import llm_handler, source_cleaning
    from pandrator.logic.source_cleaning.models import PipelineResult

    def completion(**kwargs):
        if on_response is not None:
            on_response()
        return {
            "id": "synthetic-cleaning-response",
            "model": kwargs["model"],
            "choices": [
                {
                    "message": {"content": "" if empty else '{"action":"finish"}'},
                    "finish_reason": "length" if empty else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 5},
            },
            "_hidden_params": {"response_cost": 0.25},
        }

    native = Mock(side_effect=completion)
    monkeypatch.setattr(llm_handler, "_get_litellm_clients", lambda: (native, None))
    monkeypatch.setattr(llm_handler, "wait_for_retry", lambda *_args: True)

    def pipeline(_document, *, llm_settings, config, completion_func=None, **_kwargs):
        assert completion_func is not None
        response = completion_func(
            messages=[{"role": "user", "content": "Review shared source."}],
            model_name=config.model_name,
            llm_settings=llm_settings,
        )
        return PipelineResult(
            llm_usage={
                "models": [response.model],
                "prompt_tokens": 12,
                "completion_tokens": 2,
                "cost_usd": 0.25,
            }
        )

    monkeypatch.setattr(source_cleaning, "run_cleaning_pipeline", pipeline)
    return native


def assert_usage(f, job_id):
    with f.case.database.snapshot_session() as unit:
        events = list(unit.scalars(select(m.UsageEvent).where(m.UsageEvent.job_id == job_id)))
        assert len(events) == 1
        usage = events[0]
        assert usage.workspace_id == f.u.workspace_id
        assert usage.stage == "source_cleaning" and usage.provider_key == "openai"
        assert usage.input_tokens == 12 and usage.output_tokens == 2
        assert usage.cached_input_tokens == 5 and usage.cost_usd == 0.25
        assert "messages" not in usage.raw_usage_json


def test_member_clean_then_peer_prepare_after_creator_leaves(cleaning, monkeypatch):
    f, u = cleaning, cleaning.u
    record = book(f, u.alice, u.ah, u.workspace_id)
    original, path = source(f, record)
    receipt = body(
        u.alice.post(
            f"/api/v1/sessions/{record['id']}/stages/clean_source/run",
            json={"agentic": False},
            headers=u.ah,
        ),
        202,
    )
    completed = execute(f, receipt)
    assert completed.status == "succeeded", completed.error_message
    assert set(completed.result_json) == {"artifact_id", "path", "characters", "report"}
    output_id = completed.result_json["artifact_id"]
    with f.case.database.immediate_session() as unit:
        clean = unit.get_one(m.Artifact, output_id)
        assert clean.workspace_id == u.workspace_id and clean.project_id == record["project_id"]
        baseline = unit.scalar(
            select(m.Artifact).where(
                m.Artifact.session_id == record["id"], m.Artifact.role == "extracted_text"
            )
        )
        assert baseline is not None
        assert f.services.paths.managed_path(baseline.relative_path).read_text() == TEXT
        assert unit.get(m.ArtifactEdge, (original.id, baseline.id)) is not None
        assert unit.get(m.ArtifactEdge, (baseline.id, output_id)) is not None
        unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    assert u.bob.get(f"/api/v1/artifacts/{output_id}/content").data.decode() == TEXT
    assert u.alice.get(f"/api/v1/artifacts/{output_id}/content").status_code == 404
    monkeypatch.setattr(
        "pandrator.logic.text_preprocessor.preprocess_text",
        lambda *_args, **_kwargs: [{"text": TEXT}],
    )
    prepared = execute(
        f,
        body(
            u.bob.post(
                f"/api/v1/sessions/{record['id']}/stages/prepare_text/run",
                json={"enable_nemo_normalization": False},
                headers=u.bh,
            ),
            202,
        ),
    )
    assert prepared.status == "succeeded", prepared.error_message
    assert prepared.result_json["segments"] == 1
    assert path.read_text() == TEXT


def test_agentic_worker_completes_owned_run_and_records_one_response(cleaning, monkeypatch):
    f, u = cleaning, cleaning.u
    record = book(f, u.alice, u.ah, u.workspace_id)
    original, path = source(f, record)
    _provider, selected = approved_model(f)
    native = native_pipeline(monkeypatch)
    receipt = agentic_job(f, record, original, selected)
    completed = execute(f, receipt)
    assert completed.status == "succeeded", completed.error_message
    assert native.call_count == 1
    assert_usage(f, completed.id)
    with f.case.database.snapshot_session() as unit:
        run = unit.get_one(m.AgentRun, receipt["id"])
        assert run.status == "completed" and run.job_id == completed.id
        assert run.result_artifact_id == completed.result_json["artifact_id"]
    assert (
        TEXT
        in u.bob.get(
            f"/api/v1/artifacts/{completed.result_json['artifact_id']}/content"
        ).data.decode()
    )
    assert path.read_text() == TEXT


@pytest.mark.parametrize("loss", ["membership", "provider"])
def test_retry_loss_preserves_incurred_usage_and_publishes_no_output(cleaning, monkeypatch, loss):
    f, u = cleaning, cleaning.u
    record = book(f, u.alice, u.ah, u.workspace_id)
    original, path = source(f, record)
    connection, selected = approved_model(f)

    def revoke():
        with f.case.database.immediate_session() as unit:
            if loss == "membership":
                unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
            else:
                unit.get_one(m.Provider, connection["id"]).enabled = False

    native = native_pipeline(monkeypatch, on_response=revoke, empty=True)
    receipt = agentic_job(f, record, original, selected)
    before = artifact_count(f)
    completed = execute(f, receipt)
    assert completed.status == "failed"
    assert native.call_count == 1
    assert_usage(f, completed.id)
    assert artifact_count(f) == before and path.read_text() == TEXT
    with f.case.database.snapshot_session() as unit:
        run = unit.get_one(m.AgentRun, receipt["id"])
        assert run.status == "failed" and run.result_artifact_id is None


def test_clean_output_registration_fault_rolls_back_both_artifacts_and_run_result(
    cleaning, monkeypatch
):
    f, u = cleaning, cleaning.u
    record = book(f, u.alice, u.ah, u.workspace_id)
    original, path = source(f, record)
    _connection, selected = approved_model(f)
    native_pipeline(monkeypatch)
    receipt = agentic_job(f, record, original, selected)
    before = artifact_count(f)
    artifacts = f.services.workflow_handlers.artifacts
    register = artifacts.register_in_session

    def register_then_fail(*args, **kwargs):
        result = register(*args, **kwargs)
        if kwargs.get("role") == "clean_text":
            raise RuntimeError("Synthetic clean publication failure")
        return result

    monkeypatch.setattr(artifacts, "register_in_session", register_then_fail)
    completed = execute(f, receipt)
    assert completed.status == "failed" and "Synthetic clean" in completed.error_message
    assert artifact_count(f) == before and path.read_text() == TEXT
    assert_usage(f, completed.id)
    with f.case.database.snapshot_session() as unit:
        run = unit.get_one(m.AgentRun, receipt["id"])
        assert run.status == "failed" and run.result_artifact_id is None
        assert (
            unit.scalar(
                select(func.count())
                .select_from(m.AgentStep)
                .where(m.AgentStep.agent_run_id == run.id)
            )
            == 0
        )


def test_cancel_during_hash_keeps_incurred_usage_without_publishing_output(cleaning, monkeypatch):
    f, u = cleaning, cleaning.u
    record = book(f, u.alice, u.ah, u.workspace_id)
    original, path = source(f, record)
    _connection, selected = approved_model(f)
    native_pipeline(monkeypatch)
    receipt = agentic_job(f, record, original, selected)
    before = artifact_count(f)
    artifacts = f.services.workflow_handlers.artifacts
    prepare = artifacts.prepare_registration
    hash_calls = []

    def hash_then_cancel(*args, **kwargs):
        result = prepare(*args, **kwargs)
        hash_calls.append(result)
        f.services.jobs.request_cancel(receipt["job_id"])
        return result

    monkeypatch.setattr(artifacts, "prepare_registration", hash_then_cancel)
    completed = execute(f, receipt)
    assert completed.status == "canceled" and len(hash_calls) == 1
    assert artifact_count(f) == before and path.read_text() == TEXT
    assert_usage(f, completed.id)
    with f.case.database.snapshot_session() as unit:
        run = unit.get_one(m.AgentRun, receipt["id"])
        assert run.status == "canceled" and run.result_artifact_id is None


def test_terminal_bookkeeping_ignores_foreign_payload_run_and_closes_own_active_run(cleaning):
    f, u = cleaning, cleaning.u
    target = book(f, u.alice, u.ah, u.workspace_id)
    original, _path = source(f, target)
    foreign = book(f, u.bob, u.bh)
    foreign_source, _foreign_path = source(f, foreign)
    with execution_actor_scope(HostActor("pandrator-cli")):
        foreign_job = f.services.jobs.enqueue("source.clean", {}, session_id=foreign["id"])
        claimed = f.services.jobs.claim("other-live-worker", lease_seconds=900)
    assert claimed is not None and claimed.id == foreign_job.id
    with f.case.database.immediate_session() as unit:
        foreign_run = m.AgentRun(
            kind="source_cleaning",
            session_id=foreign["id"],
            source_artifact_id=foreign_source.id,
            job_id=foreign_job.id,
            status="running",
            source_content_hash=foreign_source.content_hash or "",
            settings_hash=stable_hash({}),
            settings_json={},
        )
        unit.add(foreign_run)
        unit.flush()
        foreign_run_id = foreign_run.id
    _token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.run"}, workspace=u.workspace_id
    )
    principal = f.case.auth.resolve_api_token(
        bearer["Authorization"].split(" ", 1)[1], network_zone="loopback", target_instance_id=None
    )
    assert principal is not None
    with execution_actor_scope(actor_from_principal(principal)):
        job = f.services.jobs.enqueue(
            "source.clean",
            {
                "session_id": target["id"],
                "source_artifact_id": original.id,
                "agent_run_id": foreign_run_id,
                "settings": {"agentic": False},
            },
            session_id=target["id"],
        )
    with f.case.database.immediate_session() as unit:
        own = m.AgentRun(
            kind="source_cleaning",
            session_id=target["id"],
            source_artifact_id=original.id,
            job_id=job.id,
            status="queued",
            source_content_hash=original.content_hash or "",
            settings_hash=stable_hash({}),
            settings_json={},
        )
        unit.add(own)
        unit.flush()
        own_id = own.id
    completed = execute(f, {"job_id": job.id})
    assert completed.status == "failed"
    with f.case.database.snapshot_session() as unit:
        assert unit.get_one(m.AgentRun, foreign_run_id).status == "running"
        assert unit.get_one(m.AgentRun, own_id).status == "failed"
