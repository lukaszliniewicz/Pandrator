"""Authenticated automatic narration delegates only its actual source stages."""

from __future__ import annotations

import io
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.source_preparation_policy import require_text_preparation_access
from pandrator.web.accounts.execution_actor import UserActor, current_execution_actor
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import Worker
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401

TEXT = "The first sentence. The next sentence."
RECORDS = [{"text": "The first sentence."}, {"text": "The next sentence."}]


@pytest.mark.parametrize("mode", ["solo", "team"])
def test_primary_http_generation_runs_source_children_under_real_parent_lease(
    case, monkeypatch, mode
):
    services = case.app.extensions["pandrator"]["services"]
    if mode == "team":
        people = team(case, monkeypatch)
        client, csrf, expected_user, workspace = (
            people.alice,
            people.ah,
            people.alice_id,
            people.workspace_id,
        )
    else:
        client, csrf, expected_user, workspace = (
            case.client,
            {"X-CSRF-Token": case.csrf},
            case.actor.user_id,
            None,
        )
        with case.database.snapshot_session() as unit:
            assert unit.scalar(select(func.count()).select_from(m.User)) == 1
    values = {
        "name": f"Automatic source {uuid4()}",
        "workflow_kind": "audiobook",
        "source_language": "en",
        "included_stages": ["clean_source", "prepare_text", "generate_audio"],
    }
    if workspace is not None:
        values["workspace_id"] = workspace
    record = body(
        client.post("/api/v1/sessions", json=values, headers=headers(csrf, str(uuid4()))), 201
    )
    body(
        client.post(
            "/api/v1/uploads",
            data={"session_id": record["id"], "file": (io.BytesIO(TEXT.encode()), "chapter.txt")},
            headers=headers(csrf, str(uuid4())),
        ),
        201,
    )
    with case.database.snapshot_session() as unit:
        original = unit.scalar(
            select(m.Artifact).where(
                m.Artifact.session_id == record["id"], m.Artifact.role == "upload"
            )
        )
        assert original is not None
        original_path = services.paths.managed_path(original.relative_path)

    # The changed source stages and their normal queue/worker are real. Only
    # native tokenization and the later, separately qualified audio stage are stubbed.
    monkeypatch.setattr(
        "pandrator.logic.text_preprocessor.preprocess_text",
        lambda *_args, **_kwargs: [dict(value) for value in RECORDS],
    )
    audio_calls = []

    def audio_boundary(payload, _progress, _cancel, **_kwargs):
        actor, lease = current_execution_actor.get(), current_job_lease.get()
        assert type(actor) is UserActor and actor.user_id == expected_user
        assert lease is not None
        with case.database.snapshot_session() as unit:
            assert unit.get_one(m.Job, lease["job_id"]).kind == "workflow.continue"
            plan = unit.scalar(
                select(m.GenerationPlan).where(m.GenerationPlan.session_id == record["id"])
            )
            assert plan is not None
            assert list(
                unit.scalars(
                    select(m.GenerationSegment.text)
                    .where(m.GenerationSegment.plan_revision_id == plan.active_revision_id)
                    .order_by(m.GenerationSegment.ordinal)
                )
            ) == [row["text"] for row in RECORDS]
            clean = unit.scalar(
                select(m.Artifact).where(
                    m.Artifact.session_id == record["id"],
                    m.Artifact.role == "clean_text",
                    m.Artifact.state == "current",
                )
            )
            assert clean is not None
            # The source-stage delegation must have ended before the audio stage.
            with pytest.raises(AccessDenied):
                require_text_preparation_access(unit, record["id"], clean.id)
        audio_calls.append(payload)
        return {"status": "paused", "segments": len(RECORDS)}

    monkeypatch.setattr(services.workflow_handlers, "_run_reviewable_generation", audio_boundary)
    admitted = body(
        client.post(
            f"/api/v1/sessions/{record['id']}/stages/generate_audio/run",
            json={
                "llm_tts_optimization": False,
                "llm_tts_document_optimization": False,
                "stage_settings": {
                    "clean_source": {"agentic": False},
                    "prepare_text": {"enable_nemo_normalization": False},
                },
            },
            headers=csrf,
        ),
        202,
    )
    assert admitted["kind"] == "workflow.continue"
    assert Worker(
        services.jobs, "automatic-source-user", services.workflow_handlers.handlers()
    ).run_once()
    completed = services.jobs.get(admitted["id"])
    assert completed.status == "succeeded", completed.error_message
    assert len(audio_calls) == 1
    assert [item["stage"] for item in completed.result_json["artifacts"]] == [
        "clean_source",
        "prepare_text",
        "generate_audio",
    ]
    assert original_path.read_text() == TEXT
    assert client.get(f"/api/v1/sessions/{record['id']}/generation/summary").status_code == 200
