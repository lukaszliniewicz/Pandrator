"""End-to-end API sidecars, leases, immutability and frozen context tests."""

import json
import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from pandrator.logic.speech_performance import compile_performance
from pandrator.web import models as m
from pandrator.web import performance_plans as plans
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_audio_identity import AudioIdentityContext
from pandrator.web.speech_plan_workspace import (
    freeze_speech_snapshot,
    frozen_semantic_contexts,
    segment_performance_settings,
)


@pytest.fixture
def case(tmp_path):
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    headers = {
        "X-CSRF-Token": client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
    }
    svc = app.extensions["pandrator"]
    created = client.post(
        "/api/v1/sessions",
        json={"name": "Performance regression", "workflow_kind": "audiobook"},
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    sid = created.get_json()["id"]
    plan = svc["generation"].create_plan(
        sid,
        source_revision_id=None,
        settings={},
        segments=[
            {"text": "The request was rejected.", "speaker": "A"},
            {"text": "Yet this defeat would be temporary.", "speaker": "A"},
            {"text": "Membership doubled soon afterwards.", "speaker": "A"},
        ],
    )
    revision = plan["active_revision_id"]
    with svc["database"].session() as session:
        ids = list(
            session.scalars(
                select(m.GenerationSegment.id)
                .where(m.GenerationSegment.plan_revision_id == revision)
                .order_by(m.GenerationSegment.ordinal)
            )
        )
    base = f"/api/v1/sessions/{sid}/performance-plans"

    def post(path, data=None, key=None, method="post"):
        return getattr(client, method)(
            base + path,
            json=data or {},
            headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    result = {
        "app": app,
        "client": client,
        "headers": headers,
        "services": svc,
        "session_id": sid,
        "revision_id": revision,
        "segment_ids": ids,
        "base": base,
        "post": post,
    }
    yield result
    svc["database"].dispose()


def create(case, **options):
    response = case["post"](
        "", {"expected_plan_revision_id": case["revision_id"], **options}
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def get(case, plan):
    response = case["client"].get(case["base"] + "/" + plan["id"])
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def edit(case, plan, index=1, instruction="Mark a restrained contrast", locked=True):
    response = case["post"](
        "/" + plan["id"],
        {
            "expected_version": plan["version"],
            "items": [
                {
                    "segment_id": case["segment_ids"][index],
                    "annotation": {
                        "decision": "steer",
                        "delivery": {"instruction": instruction},
                        "locked": locked,
                    },
                }
            ],
        },
        method="patch",
    )
    assert response.status_code == 200, response.get_json()
    return get(case, plan)


def adopt(case, plan):
    response = case["post"](
        "/" + plan["id"] + "/adopt",
        {"expected_version": plan["version"], "accept_unanalysed": True},
    )
    assert response.status_code == 200, response.get_json()
    return get(case, plan)


def test_manual_plan_adoption_and_immutable_copy(case):
    plan = edit(case, create(case))
    adopted = adopt(case, plan)
    assert adopted["status"] == "adopted"
    assert adopted["analysed_count"] == 3
    settings, _ = case["services"]["workspace_settings"].resolve(case["session_id"])
    assert settings["tts"]["performance_enabled"]
    refused = case["post"](
        "/" + plan["id"],
        {
            "expected_version": adopted["version"],
            "items": [
                {
                    "segment_id": case["segment_ids"][0],
                    "annotation": {"decision": "none"},
                }
            ],
        },
        method="patch",
    )
    assert refused.status_code == 409
    copied = create(case, copy_from_id=plan["id"])
    assert copied["status"] == "draft" and copied["locked_count"] == 1
    assert (
        copied["items"][1]["annotation"]["delivery"]["instruction"]
        == "Mark a restrained contrast"
    )


def test_create_idempotency_and_invalid_replay(case):
    payload = {"expected_plan_revision_id": case["revision_id"]}
    first = case["post"]("", payload, key="performance-idempotency")
    again = case["post"]("", payload, key="performance-idempotency")
    assert first.status_code == again.status_code == 201
    assert first.get_json()["id"] == again.get_json()["id"]
    assert again.headers["Idempotency-Replayed"] == "true"
    changed = case["post"](
        "", {**payload, "batch_size": 1}, key="performance-idempotency"
    )
    assert changed.status_code == 409


def test_unauthenticated_and_missing_idempotency_are_rejected(case):
    assert case["app"].test_client().get(case["base"]).status_code == 401
    response = case["client"].post(
        case["base"],
        json={"expected_plan_revision_id": case["revision_id"]},
        headers=case["headers"],
    )
    assert response.status_code == 400


def test_claim_submit_order_strictness_idempotency_and_manual_precedence(case):
    plan = create(case, mode="passive")
    claim = case["post"]("/" + plan["id"] + "/claim").get_json()
    assert (
        claim["batch"]["items"][1]["context"]["before"] == "The request was rejected."
    )
    items = [
        {"segment_id": key, "annotation": {"decision": "none"}}
        for key in case["segment_ids"]
    ]
    path = f"/{plan['id']}/batches/{claim['batch_id']}/submit"
    wrong = case["post"](
        path, {"lease_token": claim["lease_token"], "items": list(reversed(items))}
    )
    assert wrong.status_code == 422
    plan = edit(case, get(case, plan))
    correct = case["post"](path, {"lease_token": claim["lease_token"], "items": items})
    assert correct.status_code == 200, correct.get_json()
    repeat = case["post"](path, {"lease_token": claim["lease_token"], "items": items})
    assert repeat.get_json()["replayed"]
    plan = get(case, plan)
    assert plan["items"][1]["annotation"]["locked"]
    assert plan["items"][1]["annotation"]["decision"] == "steer"
    assert case["post"]("/" + plan["id"] + "/claim").get_json()["complete"]


def test_lease_expiry_and_release(case):
    plan = create(case, mode="passive")
    first = case["post"]("/" + plan["id"] + "/claim").get_json()
    assert case["post"]("/" + plan["id"] + "/claim").get_json()["batch"] is None
    with case["services"]["database"].session() as session:
        batch = session.get(m.PerformanceBatch, first["batch_id"])
        batch.lease_expires_at = m.utcnow() - timedelta(seconds=1)
    new = case["post"]("/" + plan["id"] + "/claim").get_json()
    assert (
        new["batch_id"] == first["batch_id"]
        and new["lease_token"] != first["lease_token"]
    )
    path = f"/{plan['id']}/batches/{new['batch_id']}/release"
    assert case["post"](path, {"lease_token": first["lease_token"]}).status_code == 409
    assert case["post"](path, {"lease_token": new["lease_token"]}).status_code == 200


def test_no_worker_vocalizations_locks_or_text_mutations(case):
    plan = create(case, mode="passive")
    claim = case["post"]("/" + plan["id"] + "/claim").get_json()
    base_items = [
        {"segment_id": key, "annotation": {"decision": "none"}}
        for key in case["segment_ids"]
    ]
    for invalid in (
        {"decision": "none", "locked": True},
        {"decision": "steer", "events": [{"kind": "laugh"}]},
        {"decision": "none", "text": "Injected speech"},
    ):
        items = [{**item} for item in base_items]
        items[0] = {**items[0], "annotation": invalid}
        response = case["post"](
            f"/{plan['id']}/batches/{claim['batch_id']}/submit",
            {"lease_token": claim["lease_token"], "items": items},
        )
        assert response.status_code == 422, response.get_json()


def test_stale_text_fails_adoption(case):
    plan = create(case)
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, case["segment_ids"][1])
        segment.text = "Changed source"
    response = case["post"](
        "/" + plan["id"] + "/adopt",
        {"expected_version": plan["version"], "accept_unanalysed": True},
    )
    assert response.status_code == 409
    assert get(case, plan)["stale"]


def test_snapshot_keeps_old_performance_and_full_neighbour_context(case):
    plan = adopt(case, edit(case, create(case)))
    sid = case["segment_ids"][1]
    settings = {
        "service": "gemini",
        "model": "gemini-2.5-flash-tts",
        "performance_enabled": True,
        "tts_context_mode": "both",
    }
    snapshot = {"tts": settings}
    with case["services"]["database"].session() as session:
        freeze_speech_snapshot(session, case["revision_id"], snapshot)
    context = frozen_semantic_contexts(snapshot)
    assert context[sid]["before"] == "The request was rejected."
    assert context[sid]["after"] == "Membership doubled soon afterwards."
    copied = create(case, copy_from_id=plan["id"])
    changed = case["post"](
        "/" + copied["id"],
        {
            "expected_version": copied["version"],
            "unlock_locked": True,
            "items": [
                {
                    "segment_id": sid,
                    "annotation": {
                        "decision": "steer",
                        "delivery": {"instruction": "New direction"},
                    },
                }
            ],
        },
        method="patch",
    )
    assert changed.status_code == 200
    adopt(case, get(case, copied))
    result = segment_performance_settings(
        settings, snapshot, sid, "Yet this defeat would be temporary.", contexts=context
    )
    assert (
        result["_performance"]["delivery"]["instruction"]
        == "Mark a restrained contrast"
    )
    assert compile_performance(
        "Yet this defeat would be temporary.", result
    ).input.endswith("Transcript:\nYet this defeat would be temporary.")
    with pytest.raises(ValueError, match="Spoken text changed"):
        segment_performance_settings(
            settings, snapshot, sid, "Different text", contexts=context
        )


def test_effective_audio_identity_is_per_segment_and_changes_with_adoption(case):
    adopt(case, edit(case, create(case)))
    settings = {
        "tts": {
            "service": "audio_cpp",
            "model": "fish_audio_s2_pro_q8_0",
            "performance_enabled": True,
        }
    }
    with case["services"]["database"].session() as session:
        ctx = AudioIdentityContext(session, settings)
        first = ctx.for_segment(
            session.get(m.GenerationSegment, case["segment_ids"][0])
        )
        second = ctx.for_segment(
            session.get(m.GenerationSegment, case["segment_ids"][1])
        )
    assert "performance_request_hash" not in first
    assert "performance_request_hash" in second


def test_preview_returns_compiled_request_without_synthesis(case):
    plan = edit(case, create(case))
    response = case["post"](
        "/" + plan["id"] + "/preview",
        {
            "segment_id": case["segment_ids"][1],
            "service": "audio_cpp",
            "model": "fish_audio_s2_pro_q8_0",
        },
    )
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result["input"].startswith("[Mark a restrained contrast]")
    assert result["transcript"] == "Yet this defeat would be temporary."


def test_passive_reanalysis_preserves_locked_entries(case):
    adopted = adopt(case, edit(case, create(case)))
    new = create(case, mode="passive", copy_from_id=adopted["id"])
    assert new["locked_count"] == 1 and new["analysed_count"] == 1
    batch = case["post"]("/" + new["id"] + "/claim").get_json()["batch"]
    assert case["segment_ids"][1] not in [item["segment_id"] for item in batch["items"]]


def test_llm_job_is_queued_not_run_in_request(case):
    with patch(
        "pandrator.logic.llm_handler.chat_completion_with_metadata"
    ) as completion:
        plan = create(case, mode="llm", model_name="local/test")
    completion.assert_not_called()
    assert plan["job_id"]
    with case["services"]["database"].session() as session:
        job = session.get(m.Job, plan["job_id"])
        assert job.kind == "speech.performance"
        assert job.payload_json["performance_plan_id"] == plan["id"]


def _llm_response(**kwargs):
    from pandrator.logic.llm_handler import ChatCompletionResult

    request = json.loads(kwargs["messages"][-1]["content"])
    return ChatCompletionResult(
        content=json.dumps(
            {
                "items": [
                    {
                        "segment_id": item["segment_id"],
                        "annotation": {"decision": "none"},
                    }
                    for item in request["items"]
                ]
            }
        ),
        model="local/test",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )


def test_llm_runner_checkpoints_and_resumes_without_replaying_completed_work(case):
    from types import SimpleNamespace

    plan = create(case, mode="passive", batch_size=1)
    handlers = case["services"]["workflow_handlers"]
    payload = {"session_id": case["session_id"], "performance_plan_id": plan["id"]}
    calls = []

    def interrupted(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("Simulated model interruption")
        return _llm_response(**kwargs)

    with (
        patch(
            "pandrator.web.provider_settings.build_llm_settings",
            return_value=(SimpleNamespace(provider_configs=[]), "local/test"),
        ),
        patch(
            "pandrator.logic.llm_handler.chat_completion_with_metadata",
            side_effect=interrupted,
        ),
    ):
        with pytest.raises(RuntimeError, match="interruption"):
            plans.run_analysis(handlers, payload, lambda *args: None, threading.Event())
    state = get(case, plan)
    assert state["analysed_count"] == 1
    assert [batch["status"] for batch in state["batches"]] == [
        "completed",
        "pending",
        "pending",
    ]
    with (
        patch(
            "pandrator.web.provider_settings.build_llm_settings",
            return_value=(SimpleNamespace(provider_configs=[]), "local/test"),
        ),
        patch(
            "pandrator.logic.llm_handler.chat_completion_with_metadata",
            side_effect=_llm_response,
        ) as resumed,
    ):
        result = plans.run_analysis(
            handlers, payload, lambda *args: None, threading.Event()
        )
    assert result["status"] == "ready_for_review"
    assert resumed.call_count == 2
    assert get(case, plan)["status"] == "draft"
    with case["services"]["database"].session() as session:
        events = list(
            session.scalars(
                select(m.UsageEvent).where(
                    m.UsageEvent.session_id == case["session_id"]
                )
            )
        )
        assert len(events) == 3
        assert all(event.model_id == "local/test" for event in events)


def test_missing_performance_is_inspectable_but_generation_still_rejects_it(case):
    snapshot = {
        "tts": {
            "service": "audio_cpp",
            "model": "fish_audio_s2_pro_q8_0",
            "performance_enabled": True,
        }
    }
    with case["services"]["database"].session() as session:
        identity = AudioIdentityContext(session, snapshot).for_segment(
            session.get(m.GenerationSegment, case["segment_ids"][0])
        )
        assert identity["performance_request_hash"] == "unavailable"
        with pytest.raises(ValueError, match="no adopted performance"):
            freeze_speech_snapshot(session, case["revision_id"], snapshot)


@pytest.mark.parametrize("batch_size", [1, 2])
def test_real_generation_runner_applies_frozen_performance_to_each_short_request(
    case, batch_size
):
    from pydub import AudioSegment

    from pandrator.web.tts_providers import TtsBatchResult, TtsCapabilities

    adopt(case, edit(case, create(case)))
    service = case["services"]["generation"]
    handlers = case["services"]["workflow_handlers"]
    started = service.start(
        case["session_id"],
        speech_plan_revision_id=case["revision_id"],
        run_override={
            "tts": {
                "service": "gemini",
                "model": "gemini-2.5-flash-tts",
                "voice": "Kore",
                "generation_prompt": "Restrained narration",
                "tts_context_mode": "both",
                "tts_batch_size": batch_size,
                "tts_concurrent_requests": 1,
            }
        },
    )
    requests = []
    batch_calls = []

    def synthesize(text, settings, **_options):
        requests.append(compile_performance(text, settings))
        return AudioSegment.silent(duration=20)

    def synthesize_batch(items, **_options):
        batch_calls.append(len(items))
        for item in items:
            yield TtsBatchResult(id=item.id, audio=synthesize(item.text, item.settings))

    capabilities = TtsCapabilities(
        batch_synthesis=batch_size > 1,
        streaming_batch=batch_size > 1,
        default_batch_size=batch_size,
        max_batch_size=batch_size,
    )
    with (
        patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize),
        patch.object(
            handlers.tts_providers, "synthesize_batch", side_effect=synthesize_batch
        ),
        patch.object(
            handlers.tts_providers, "synthesis_capabilities", return_value=capabilities
        ),
        patch.object(
            handlers,
            "_optimize_generation_texts",
            side_effect=AssertionError("Frozen speech must not be rewritten"),
        ),
    ):
        handlers.run_generation(
            {"generation_run_id": started["id"]}, lambda *args: None, threading.Event()
        )
    assert len(requests) == 3
    middle = next(
        request
        for request in requests
        if request.transcript == "Yet this defeat would be temporary."
    )
    assert middle.input.endswith("Transcript:\nYet this defeat would be temporary.")
    assert "The request was rejected." in middle.input
    assert "Membership doubled soon afterwards." in middle.input
    assert "Mark a restrained contrast" in middle.input
    assert all(request.input.count("Transcript:\n") == 1 for request in requests)
    assert bool(batch_calls) == (batch_size > 1)


def test_directed_generation_does_not_silently_regroup_blocks():
    from pandrator.logic.dubbing.passage_regroup import select_second_pass

    args = dict(operation="generate", has_selected_ids=False, workflow_kind="voiceover")
    baseline = {
        "speech_block_generation_mode": "passage",
        "speech_block_regroup_enabled": True,
    }
    assert select_second_pass({"tts": baseline}, **args) == "regroup"
    assert (
        select_second_pass({"tts": {**baseline, "performance_enabled": True}}, **args)
        is None
    )
    assert (
        select_second_pass({"tts": {**baseline, "tts_context_mode": "both"}}, **args)
        is None
    )
