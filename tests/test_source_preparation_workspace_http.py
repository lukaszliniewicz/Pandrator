"""Named narration preparation publishes its artifact and editable plan together."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.jobs import Worker
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token

TEXT = "Original narration.\nThe remaining member can continue."
RECORDS = [
    {"original_sentence": "Original narration.", "paragraph_break_after": True},
    {"original_sentence": "The remaining member can continue."},
]


@pytest.fixture
def preparation(case, monkeypatch):
    return SimpleNamespace(
        case=case, u=team(case, monkeypatch), services=case.app.extensions["pandrator"]["services"]
    )


def project(f):
    return body(
        f.u.alice.post(
            "/api/v1/sessions",
            json={
                "name": f"Narration preparation {uuid4()}",
                "workflow_kind": "audiobook",
                "workspace_id": f.u.workspace_id,
                "source_language": "en",
            },
            headers=headers(f.u.ah, str(uuid4())),
        ),
        201,
    )


def cleaned_source(f, record):
    with f.case.database.snapshot_session() as unit:
        key = unit.get_one(m.SessionRecord, record["id"]).storage_key
    directory = f.services.paths.sessions / key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cleaned-narration.txt"
    path.write_text(TEXT)
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path, kind="text", role="clean_text", session_id=record["id"]
        )
    return artifact.id, path


def preprocess(monkeypatch):
    calls = []

    def local(text, settings, progress_callback=None):
        calls.append((text, settings))
        if progress_callback is not None:
            progress_callback(0.5, "Synthetic segmentation")
        return [dict(record) for record in RECORDS]

    monkeypatch.setattr("pandrator.logic.text_preprocessor.preprocess_text", local)
    return calls


def admit(client, csrf, sid):
    return body(
        client.post(
            f"/api/v1/sessions/{sid}/stages/prepare_text/run",
            json={"enable_nemo_normalization": False},
            headers=csrf,
        ),
        202,
    )


def execute(f, receipt):
    assert Worker(
        f.services.jobs, "named-text-preparation", f.services.workflow_handlers.handlers()
    ).run_once()
    return f.services.jobs.get(receipt["id"])


def counts(f):
    with f.case.database.snapshot_session() as unit:
        return {
            model.__tablename__: unit.scalar(select(func.count()).select_from(model))
            for model in (
                m.Artifact,
                m.ArtifactEdge,
                m.GenerationPlan,
                m.GenerationPlanRevision,
                m.GenerationSegment,
            )
        }


def active_revision(f, sid):
    with f.case.database.snapshot_session() as unit:
        return unit.scalar(
            select(m.GenerationPlan.active_revision_id).where(m.GenerationPlan.session_id == sid)
        )


def seed_plan(f, sid, source_id):
    return f.services.workflow_handlers._store_generation_plan(
        sid,
        [{"text": "Retain this existing plan on failure."}],
        settings={},
        source_artifact_id=source_id,
    )[0]


def test_remaining_member_prepares_through_normal_stage_with_read_run_credential(
    preparation, monkeypatch
):
    f, u = preparation, preparation.u
    record = project(f)
    source_id, path = cleaned_source(f, record)
    calls = preprocess(monkeypatch)
    _, bearer = scoped_token(f.case, u.alice_id, {"app.read", "app.run"}, workspace=u.workspace_id)
    receipt = admit(f.case.app.test_client(), bearer, record["id"])
    first = execute(f, receipt)
    assert first.status == "succeeded", first.error_message
    first_revision = first.result_json["generation_plan_revision_id"]
    assert first.result_json["segments"] == 2 and calls[0][0] == TEXT
    with f.case.database.immediate_session() as unit:
        assert unit.get_one(m.JobActorRecord, first.id).user_id == u.alice_id
        unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    second = execute(f, admit(u.bob, u.bh, record["id"]))
    assert second.status == "succeeded", second.error_message
    result = second.result_json
    assert set(result) == {
        "artifact_id",
        "path",
        "segments",
        "generation_plan_revision_id",
        "segmentation",
        "segmentation_budget",
    }
    assert result["generation_plan_revision_id"] != first_revision
    assert active_revision(f, record["id"]) == result["generation_plan_revision_id"]
    with f.case.database.snapshot_session() as unit:
        artifact = unit.get_one(m.Artifact, result["artifact_id"])
        assert (
            artifact.workspace_id == u.workspace_id and artifact.project_id == record["project_id"]
        )
        assert artifact.session_id == record["id"] and artifact.role == "prepared_text"
        revision = unit.get_one(m.GenerationPlanRevision, result["generation_plan_revision_id"])
        assert revision.settings_json["_source_artifact_id"] == artifact.id
        assert unit.get_one(m.GenerationPlanRevision, first_revision).id == first_revision
        assert (
            unit.scalar(
                select(m.ArtifactEdge.parent_artifact_id).where(
                    m.ArtifactEdge.child_artifact_id == artifact.id
                )
            )
            == source_id
        )
        assert unit.get_one(m.JobActorRecord, second.id).user_id == u.bob_id
        assert (
            unit.scalar(
                select(func.count())
                .select_from(m.ProjectMembership)
                .where(m.ProjectMembership.project_id == record["project_id"])
            )
            == 0
        )
    assert (
        json.loads(u.bob.get(f"/api/v1/artifacts/{result['artifact_id']}/content").data) == RECORDS
    )
    assert u.bob.get(f"/api/v1/sessions/{record['id']}/generation/summary").status_code == 200
    assert u.alice.get(f"/api/v1/artifacts/{result['artifact_id']}/content").status_code == 404
    assert path.read_text() == TEXT and len(calls) == 2


@pytest.mark.parametrize("loss", ["membership", "run_scope"])
def test_revoked_queued_preparation_stops_before_source_read_and_native_work(
    preparation, monkeypatch, loss
):
    f, u = preparation, preparation.u
    record = project(f)
    _source_id, path = cleaned_source(f, record)
    token = None
    if loss == "membership":
        receipt = admit(u.alice, u.ah, record["id"])
    else:
        token, bearer = scoped_token(
            f.case, u.alice_id, {"app.read", "app.run"}, workspace=u.workspace_id
        )
        receipt = admit(f.case.app.test_client(), bearer, record["id"])
    before = counts(f)
    with f.case.database.immediate_session() as unit:
        if loss == "membership":
            unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
        else:
            assert token is not None
            unit.get_one(m.ApiToken, token.id).scopes_json = ["app.read"]
    native = Mock(side_effect=AssertionError("Revoked preparation reached the preprocessor"))
    resolver = Mock(side_effect=AssertionError("Revoked preparation resolved the source body"))
    monkeypatch.setattr("pandrator.logic.text_preprocessor.preprocess_text", native)
    monkeypatch.setattr(f.services.workflow_handlers, "_resolve_input", resolver)
    completed = execute(f, receipt)
    assert completed.status == "failed"
    native.assert_not_called()
    resolver.assert_not_called()
    assert counts(f) == before and path.read_text() == TEXT
    assert not list(path.parent.rglob("prepared_narration.json"))


def test_preparation_writer_rechecks_revocation_during_hash(preparation, monkeypatch):
    f, u = preparation, preparation.u
    record = project(f)
    source_id, path = cleaned_source(f, record)
    original_revision = seed_plan(f, record["id"], source_id)
    preprocess(monkeypatch)
    receipt = admit(u.alice, u.ah, record["id"])
    before = counts(f)
    worker_artifacts = f.services.workflow_handlers.artifacts
    prepare = worker_artifacts.prepare_registration
    hash_calls = []

    def hash_then_revoke(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        hash_calls.append(prepared)
        with f.case.database.immediate_session() as unit:
            unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
        return prepared

    monkeypatch.setattr(worker_artifacts, "prepare_registration", hash_then_revoke)
    completed = execute(f, receipt)
    assert len(hash_calls) == 1
    assert completed.status == "failed"
    assert counts(f) == before and active_revision(f, record["id"]) == original_revision
    assert path.read_text() == TEXT


def test_plan_store_fault_rolls_back_output_artifact_and_active_revision(preparation, monkeypatch):
    f = preparation
    record = project(f)
    source_id, path = cleaned_source(f, record)
    original_revision = seed_plan(f, record["id"], source_id)
    preprocess(monkeypatch)
    receipt = admit(f.u.alice, f.u.ah, record["id"])
    before = counts(f)
    store = f.services.workflow_handlers._store_generation_plan

    def store_then_fail(*args, **kwargs):
        assert kwargs["db_session"] is not None
        store(*args, **kwargs)
        raise RuntimeError("Synthetic failure after plan rows were staged")

    monkeypatch.setattr(f.services.workflow_handlers, "_store_generation_plan", store_then_fail)
    completed = execute(f, receipt)
    assert completed.status == "failed" and "Synthetic failure" in completed.error_message
    assert counts(f) == before and active_revision(f, record["id"]) == original_revision
    assert path.read_text() == TEXT
