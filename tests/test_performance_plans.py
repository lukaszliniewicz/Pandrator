"""End-to-end API sidecars, leases, immutability and frozen context tests."""

import json
import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_performance import compile_performance
from pandrator.web import models as m
from pandrator.web import performance_plans as plans
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_audio_identity import AudioIdentityContext
from pandrator.web.speech_plan_preview import preview_speech_segment
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


@pytest.mark.parametrize("action", ["claim", "renew", "adopt"])
def test_missing_lease_expiry_is_invalid_and_recoverable(case, action):
    plan = create(case, mode="passive")
    claim = case["post"]("/" + plan["id"] + "/claim").get_json()
    if action == "adopt":
        active = case["post"](
            "/" + plan["id"] + "/adopt",
            {"expected_version": plan["version"], "accept_unanalysed": True},
        )
        assert active.status_code == 409
    with case["services"]["database"].session() as session:
        batch = session.get(m.PerformanceBatch, claim["batch_id"])
        batch.lease_expires_at = None
    items = [
        {"segment_id": key, "annotation": {"decision": "none"}}
        for key in case["segment_ids"]
    ]
    old_path = f"/{plan['id']}/batches/{claim['batch_id']}"
    assert case["post"](
        old_path + "/submit", {"lease_token": claim["lease_token"], "items": items}
    ).status_code == 409
    if action == "claim":
        recovered = case["post"]("/" + plan["id"] + "/claim")
        assert recovered.status_code == 200, recovered.get_json()
        new = recovered.get_json()
        assert new["batch_id"] == claim["batch_id"]
        assert new["lease_token"] != claim["lease_token"]
        assert new["lease_expires_at"]
        assert case["post"](
            old_path + "/renew", {"lease_token": claim["lease_token"]}
        ).status_code == 409
        assert case["post"](
            old_path + "/submit", {"lease_token": claim["lease_token"], "items": items}
        ).status_code == 409
    elif action == "renew":
        refused = case["post"](
            old_path + "/renew", {"lease_token": claim["lease_token"]}
        )
        assert refused.status_code == 409, refused.get_json()
        with case["services"]["database"].session() as session:
            batch = session.get(m.PerformanceBatch, claim["batch_id"])
            assert batch.status == "leased"
            assert batch.lease_token == claim["lease_token"]
            assert batch.lease_expires_at is None
    else:
        adopted = case["post"](
            "/" + plan["id"] + "/adopt",
            {"expected_version": plan["version"], "accept_unanalysed": True},
        )
        assert adopted.status_code == 200, adopted.get_json()
        assert get(case, plan)["status"] == "adopted"


@pytest.mark.parametrize("failure_batch", [1, 2])
def test_usage_initialization_failure_preserves_error_and_releases_lease(case, failure_batch):
    from types import SimpleNamespace

    from pandrator.web.tts_optimization import OptimizationUsage

    plan = create(case, mode="passive", batch_size=1)
    handlers = case["services"]["workflow_handlers"]
    payload = {"session_id": case["session_id"], "performance_plan_id": plan["id"]}
    allocations = [OptimizationUsage() for _ in range(failure_batch - 1)]
    allocations.append(RuntimeError("Usage initialization failed"))
    with (
        patch(
            "pandrator.web.provider_settings.build_llm_settings",
            return_value=(SimpleNamespace(provider_configs=[]), "local/test"),
        ),
        patch("pandrator.web.tts_optimization.OptimizationUsage", side_effect=allocations),
        patch(
            "pandrator.logic.llm_handler.chat_completion_with_metadata",
            side_effect=_llm_response,
        ) as provider,
        patch.object(handlers, "_record_usage", wraps=handlers._record_usage) as accounting,
    ):
        with pytest.raises(RuntimeError, match="^Usage initialization failed$"):
            plans.run_analysis(handlers, payload, lambda *args: None, threading.Event())
    assert provider.call_count == accounting.call_count == failure_batch - 1
    state = get(case, plan)
    assert state["analysed_count"] == failure_batch - 1
    assert [batch["status"] for batch in state["batches"]] == (
        ["completed"] * (failure_batch - 1) + ["pending"] * (4 - failure_batch)
    )
    with case["services"]["database"].session() as session:
        events = list(session.scalars(select(m.UsageEvent).where(
            m.UsageEvent.session_id == case["session_id"]
        )))
        assert len(events) == failure_batch - 1
        for batch in session.scalars(select(m.PerformanceBatch).where(
            m.PerformanceBatch.performance_plan_id == plan["id"],
            m.PerformanceBatch.status == "pending",
        )):
            assert batch.lease_token is None and batch.lease_expires_at is None


@pytest.mark.parametrize("replacement_lease", [False, True])
def test_progress_failure_releases_only_its_owned_analysis_lease(case, replacement_lease):
    from types import SimpleNamespace

    plan = create(case, mode="passive", batch_size=1)
    handlers = case["services"]["workflow_handlers"]
    payload = {"session_id": case["session_id"], "performance_plan_id": plan["id"]}

    def fail_progress(*args):
        if replacement_lease:
            with case["services"]["database"].immediate_session() as session:
                batch = session.scalar(select(m.PerformanceBatch).where(
                    m.PerformanceBatch.performance_plan_id == plan["id"],
                    m.PerformanceBatch.status == "leased",
                ))
                batch.lease_token = "replacement-token"
        raise RuntimeError("Progress update failed")

    with (
        patch(
            "pandrator.web.provider_settings.build_llm_settings",
            return_value=(SimpleNamespace(provider_configs=[]), "local/test"),
        ),
        patch("pandrator.logic.llm_handler.chat_completion_with_metadata") as provider,
        patch.object(handlers, "_record_usage") as accounting,
    ):
        with pytest.raises(RuntimeError, match="^Progress update failed$"):
            plans.run_analysis(handlers, payload, fail_progress, threading.Event())
    provider.assert_not_called()
    accounting.assert_not_called()
    state = get(case, plan)
    assert state["analysed_count"] == 0
    assert [batch["status"] for batch in state["batches"]] == (
        ["leased", "pending", "pending"] if replacement_lease else ["pending"] * 3
    )
    with case["services"]["database"].session() as session:
        first = session.scalar(select(m.PerformanceBatch).where(
            m.PerformanceBatch.performance_plan_id == plan["id"]
        ).order_by(m.PerformanceBatch.ordinal))
        if replacement_lease:
            assert first.lease_token == "replacement-token"
            assert first.lease_expires_at is not None
        else:
            assert first.lease_token is None and first.lease_expires_at is None


def _adopted(case, *, locked=True, **options):
    """An adopted plan whose middle block holds the one explicit choice."""
    plan = create(case, context_before=1, context_after=0, **options)
    return adopt(case, edit(case, plan, index=1, locked=locked))


def _change(case, index, **values):
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, case["segment_ids"][index])
        for key, value in values.items():
            setattr(segment, key, value)


def _items(plan):
    return {item["id"]: item for item in plan["items"]}


def _drop_carried_plan(case, revision_id):
    """Leave the revision as one written before plans followed the blocks."""
    with case["services"]["database"].session() as session:
        for plan in session.scalars(
            select(m.PerformancePlan).where(m.PerformancePlan.plan_revision_id == revision_id)
        ):
            session.delete(plan)


@pytest.mark.parametrize("locked", [True, False])
def test_copy_of_stale_plan_keeps_entries_whose_block_and_context_are_unchanged(case, locked):
    adopted = _adopted(case, locked=locked)
    _change(case, 0, text="The request was rejected outright.")
    assert get(case, adopted)["stale"]
    copied = create(case, copy_from_id=adopted["id"])
    first, middle, last = case["segment_ids"]
    items = _items(copied)
    # The edited block is unreviewed again.
    assert items[first]["annotation"] is None
    # Its neighbour was planned with it as context: only a locked choice stays.
    if locked:
        assert items[middle]["annotation"]["delivery"]["instruction"] == "Mark a restrained contrast"
    else:
        assert items[middle]["annotation"] is None
    # The last block and its context are unchanged.
    assert items[last]["annotation"] is not None
    expected = (2, 1) if locked else (1, 2)
    assert (copied["copied_count"], copied["skipped_count"]) == expected
    assert copied["copied_from_id"] == adopted["id"]


def test_copy_of_stale_plan_notices_a_block_change_beyond_its_text(case):
    adopted = _adopted(case)
    _change(case, 2, silence_after_ms=900)
    copied = create(case, copy_from_id=adopted["id"])
    assert _items(copied)[case["segment_ids"][2]]["annotation"] is None
    assert (copied["copied_count"], copied["skipped_count"]) == (2, 1)


def test_copy_of_stale_plan_made_before_block_hashes_compares_recorded_fields(case):
    adopted = _adopted(case)
    with case["services"]["database"].session() as session:
        plan = session.get(m.PerformancePlan, adopted["id"])
        plan.units_json = [
            {key: value for key, value in unit.items() if key != "source_hash"}
            for unit in plan.units_json
        ]
    _change(case, 0, text="The request was rejected outright.")
    copied = create(case, copy_from_id=adopted["id"])
    assert (copied["copied_count"], copied["skipped_count"]) == (2, 1)


def test_reanalysis_copy_can_keep_unchanged_entries(case):
    adopted = _adopted(case, locked=False)
    _change(case, 0, text="The request was rejected outright.")
    reconsidered = create(case, mode="passive", copy_from_id=adopted["id"])
    assert reconsidered["copied_count"] == 0
    assert sum(batch["unit_count"] for batch in reconsidered["batches"]) == 3
    kept = create(case, mode="passive", copy_from_id=adopted["id"], keep_unchanged=True)
    assert kept["copied_count"] == 1
    assert sum(batch["unit_count"] for batch in kept["batches"]) == 2
    refused = case["post"](
        "", {"expected_plan_revision_id": case["revision_id"], "keep_unchanged": True}
    )
    assert refused.status_code == 422


@pytest.mark.parametrize("annotation_format", ["pssml", "xml"])
def test_restore_carries_the_adopted_plan_without_a_new_review(case, annotation_format):
    if annotation_format == "xml":
        adopted = adopt(case, create(case, annotation_format="xml"))
    else:
        adopted = _adopted(case)
    result = case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "restore", "target_revision_id": case["revision_id"], "reason": "edit_copy"},
    )
    assert result["review_required"]["performance"] is False
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert carried["status"] == "adopted" and not carried["stale"]
    assert carried["carried_from_id"] == adopted["id"]
    assert carried["plan_revision_id"] == result["plan_revision_id"]
    assert [item["id"] for item in carried["items"]] == result["segment_ids"]
    if annotation_format == "xml":
        for item in carried["items"]:
            assert f'id="{item["id"]}"' in item["speech_xml"]
    else:
        assert (
            carried["items"][1]["annotation"]["delivery"]["instruction"]
            == "Mark a restrained contrast"
        )
    snapshot = {"tts": {"performance_enabled": True}}
    with case["services"]["database"].session() as session:
        plans.freeze_performance_snapshot(session, result["plan_revision_id"], snapshot)
    assert snapshot["performance_snapshot"]["id"] == carried["id"]
    for item in carried["items"]:
        # The carried markup validates against its new block at synthesis.
        markup = plans.speech_markup_for_segment(snapshot, item["id"], item["spoken_text"])
        assert (markup is not None) == (annotation_format == "xml")


def test_restore_of_a_current_plan_with_units_made_before_structure_fields_needs_no_review(case):
    # Judged per block, such units would all need review; a plan that fits its
    # revision as a whole is not judged, so the restore needs none.
    adopted = _adopted(case)
    with case["services"]["database"].session() as session:
        plan = session.get(m.PerformancePlan, adopted["id"])
        plan.units_json = [
            {
                key: value
                for key, value in unit.items()
                if key not in {"speaker", "language", "node_kind", "section_id"}
            }
            for unit in plan.units_json
        ]
    assert not get(case, adopted)["stale"]
    result = case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "restore", "target_revision_id": case["revision_id"]},
    )
    assert result["review_required"]["performance"] is False
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert not carried["stale"] and carried["review_segment_count"] == 0


def test_a_topology_batch_carries_the_plan_and_reviews_only_the_new_blocks(case):
    # A batch carries the adopted plan through its splits and merges as a
    # single edit does. A legacy direction does not follow a split block, so
    # exactly the blocks the batch created need a new review.
    adopted = _adopted(case)
    revision = case["revision_id"]
    response = case["client"].post(
        f"/api/v1/sessions/{case['session_id']}/generation-plan/topology/batch",
        json={
            "expected_revision_id": revision,
            "operations": [
                {"action": "split", "segment_id": case["segment_ids"][0], "cursor": 3}
            ],
        },
        headers={
            **case["headers"],
            "If-Match": f'"{revision}"',
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 201, response.get_json()
    result = response.get_json()
    assert result["plan_revision_id"] != revision
    assert result["review_required"]["performance"] is True
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert carried["status"] == "adopted" and carried["stale"]
    assert carried["carried_from_id"] == adopted["id"]
    assert carried["plan_revision_id"] == result["plan_revision_id"]
    halves = result["lineage"][case["segment_ids"][0]]
    source, items = _items(adopted), _items(carried)
    assert carried["review_segment_count"] == 2
    assert {segment["id"] for segment in carried["review_segments"]} == set(halves)
    for old in case["segment_ids"][1:]:
        (new,) = result["lineage"][old]
        assert items[new]["annotation"] == source[old]["annotation"]


@pytest.mark.parametrize("change", ["fits", "structure", "removed"])
def test_restore_of_a_stale_plan_carries_it_judged_per_block(case, change):
    # Upstream carried nothing from a plan made stale by an edit, so from the
    # second edit copy on the plan was lost. Each entry now moves with its
    # block and is judged on the new revision as it was on the source.
    adopted = _adopted(case)
    _change(case, 0, text="The request was rejected outright.")
    if change == "structure":
        _change(case, 2, speaker="B")
    elif change == "removed":
        _change(case, 2, removed=True)
    source = get(case, adopted)
    assert source["stale"]
    result = case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "restore", "target_revision_id": case["revision_id"]},
    )
    lineage = {key: children[0] for key, children in result["lineage"].items()}
    assert result["review_required"]["performance"] is (change == "structure")
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert carried["status"] == "adopted" and carried["stale"]
    assert carried["carried_from_id"] == adopted["id"]
    # Every entry is mapped, a removed block's included.
    assert [item["id"] for item in carried["items"]] == result["segment_ids"]
    assert (carried["changed_segment_count"], carried["review_segment_count"]) == (
        source["changed_segment_count"],
        source["review_segment_count"],
    )
    assert carried["review_segments"] == [
        {"ordinal": item["ordinal"], "id": lineage[item["id"]]}
        for item in source["review_segments"]
    ]
    snapshot: dict = {}
    with case["services"]["database"].session() as session:
        plans.freeze_performance_snapshot(session, result["plan_revision_id"], snapshot)
    frozen = snapshot["performance_snapshot"]
    first, middle, last = (lineage[key] for key in case["segment_ids"])
    assert frozen["id"] == carried["id"]
    # The unchanged middle entry freezes as it was; the edited block fits.
    assert (
        frozen["annotations"][middle]["annotation"]["delivery"]["instruction"]
        == "Mark a restrained contrast"
    )
    assert first in frozen["annotations"]
    assert (last in frozen["annotations"]) is (change == "fits")
    assert frozen["review_segment_ids"] == ([last] if change == "structure" else [])


@pytest.mark.parametrize("locked", [True, False])
def test_a_copy_of_a_plan_carried_stale_keeps_what_a_copy_of_its_source_keeps(case, locked):
    # The carried plan keeps the edited block's old hash and moves the others,
    # so copying it judges the restored blocks exactly as copying the source.
    adopted = _adopted(case, locked=locked)
    _change(case, 0, text="The request was rejected outright.")
    result = case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "restore", "target_revision_id": case["revision_id"]},
    )
    carried_id = result["carried_performance_plan_id"]
    carried = get(case, {"id": carried_id})
    assert carried["stale"] and carried["carried_from_id"] == adopted["id"]
    copied = create(
        {**case, "revision_id": result["plan_revision_id"]},
        copy_from_id=carried_id,
        keep_unchanged=True,
    )
    first, middle, last = result["segment_ids"]
    items = _items(copied)
    assert items[first]["annotation"] is None
    if locked:
        assert items[middle]["annotation"]["delivery"]["instruction"] == "Mark a restrained contrast"
    else:
        assert items[middle]["annotation"] is None
    assert items[last]["annotation"] is not None
    expected = (2, 1) if locked else (1, 2)
    assert (copied["copied_count"], copied["skipped_count"]) == expected
    assert copied["copied_from_id"] == carried_id


def _adopted_xml(case):
    """An adopted XML plan: a locked delivery on the first block, an event in
    the second, and an unlocked delivery on the last."""
    plan = create(case, annotation_format="xml")
    first, middle, last = case["segment_ids"]
    items = [
        {
            "segment_id": first,
            "speech_xml": f'<segment id="{first}"><em>cross</em>The request was rejected.</segment>',
            "locked": True,
        },
        {
            "segment_id": middle,
            "speech_xml": (
                f'<segment id="{middle}">Yet this defeat<event kind="laugh"/>'
                " would be temporary.</segment>"
            ),
            "locked": True,
        },
        {
            "segment_id": last,
            "speech_xml": f'<segment id="{last}"><em>calm</em>Membership doubled soon afterwards.</segment>',
        },
    ]
    edited = case["post"](
        "/" + plan["id"],
        {"expected_version": plan["version"], "items": items},
        method="patch",
    )
    assert edited.status_code == 200, edited.get_json()
    return adopt(case, get(case, plan))


@pytest.mark.parametrize("mode", ["manual", "passive", "llm"])
def test_a_new_draft_starts_from_a_stale_adopted_plans_valid_entries(case, mode):
    adopted = _adopted_xml(case)
    first, middle, last = case["segment_ids"]
    # A fitted edit inside the first block's text, and one that crosses the
    # event in the second.
    _change(case, 0, text="The request was firmly rejected.")
    _change(case, 1, text="Temporary.")
    read = get(case, adopted)
    assert (read["changed_segment_count"], read["review_segment_count"]) == (2, 1)
    options = {"model_name": "local/test"} if mode == "llm" else {}
    # An llm draft only queues its analysis job, which is never run here.
    with patch("pandrator.logic.llm_handler.chat_completion_with_metadata") as completion:
        draft = create(case, annotation_format="xml", mode=mode, **options)
    completion.assert_not_called()
    assert bool(draft.get("job_id")) is (mode == "llm")
    assert "copied_from_id" not in draft
    items = _items(draft)
    # The fitted entry is rebased onto the new text, the review block starts
    # from its own markup, and the unchanged block keeps its entry.
    assert items[first]["speech_xml"] == (
        f'<segment id="{first}"><em>cross</em>The request was firmly rejected.</segment>'
    )
    assert "<event" not in items[middle]["speech_xml"]
    assert "<em>calm</em>" in items[last]["speech_xml"]
    if mode == "manual":
        # A manual draft starts from those entries but records no choice.
        assert all(item["annotation"] is None for item in items.values())
    else:
        # Reanalysis (passive or llm) keeps only the locked entry that still holds.
        assert [key for key, item in items.items() if item["annotation"]] == [first]
        assert sum(batch["unit_count"] for batch in draft["batches"]) == 2


def _cast_override():
    return {
        "tts": {
            "service": "openai",
            "model": "model-a",
            "voice": "voice-a",
            "language": "en",
            "casting_enabled": True,
            "performance_enabled": False,
        }
    }


def test_cast_start_is_refused_when_the_revision_lost_its_speaker_plan(case):
    adopted = _adopted(case)
    generation = case["services"]["generation"]
    split = generation.revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][1], "cursor": 3, "text_layer": "display"},
    )
    revision_id = split["plan_revision_id"]
    _drop_carried_plan(case, revision_id)
    settings = {"casting_enabled": True}
    with case["services"]["database"].session() as session:
        notice = plans.missing_speaker_plan(session, revision_id, settings)
        assert notice is not None
        assert (notice["plan_id"], notice["plan_revision_id"], notice["purpose"]) == (
            adopted["id"],
            case["revision_id"],
            "delivery",
        )
        assert plans.missing_speaker_plan(session, revision_id, {}) is None
        assert plans.missing_speaker_plan(
            session, revision_id, {"casting_enabled": True, "performance_enabled": True}
        ) is None
        assert plans.missing_speaker_plan(session, case["revision_id"], settings) is None
    preview = generation.preview_selection(case["session_id"], run_override=_cast_override())
    assert preview["missing_speaker_plan"]["plan_id"] == adopted["id"]
    with pytest.raises(ValueError, match="no adopted speaker plan"):
        generation.start(case["session_id"], run_override=_cast_override())
    started = generation.start(
        case["session_id"], run_override=_cast_override(), allow_without_speaker_plan=True
    )
    assert started["id"]


def test_speaker_plan_refusal_has_one_rule(case):
    adopted = _adopted(case)
    split = case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][1], "cursor": 3, "text_layer": "display"},
    )
    revision_id = split["plan_revision_id"]
    _drop_carried_plan(case, revision_id)
    settings = {"casting_enabled": True}
    with case["services"]["database"].session() as session:
        notice = plans.missing_speaker_plan(session, revision_id, settings)
        assert notice is not None
        with pytest.raises(ValueError) as refused:
            plans.refuse_missing_speaker_plan(session, revision_id, settings)
        # No casting, no guard.
        plans.refuse_missing_speaker_plan(session, revision_id, {})
        # The acknowledgement is the other way through.
        plans.waive_speaker_plan(session, revision_id, adopted["id"])
        plans.refuse_missing_speaker_plan(session, revision_id, settings)
    message = str(refused.value)
    assert message.startswith(
        "Casting is on, but this speech plan has no adopted speaker plan, so its "
        "dialogue would be voiced as narration. "
    )
    assert f"Speech plan revision {notice['revision_number']} adopted one" in message
    assert f"(performance plan {adopted['id']})" in message
    assert '"Generate without the speaker plan" (allow_without_speaker_plan)' in message
    assert "that choice is kept for this revision and the revisions edited from it" in message


def _lost_plan_revision(case):
    """A revision on the lineage of an adopted plan that carries no plan itself."""
    split = _revise(
        case, case["revision_id"], action="split",
        segment_id=case["segment_ids"][1], cursor=3, text_layer="display",
    )
    _drop_carried_plan(case, split["plan_revision_id"])
    return split["plan_revision_id"]


def _first_segment_id(case, revision_id):
    with case["services"]["database"].session() as session:
        return session.scalar(
            select(m.GenerationSegment.id)
            .where(m.GenerationSegment.plan_revision_id == revision_id)
            .order_by(m.GenerationSegment.ordinal)
        )


def _last_segment_id(case, revision_id):
    with case["services"]["database"].session() as session:
        return session.scalar(
            select(m.GenerationSegment.id)
            .where(m.GenerationSegment.plan_revision_id == revision_id)
            .order_by(m.GenerationSegment.ordinal.desc())
        )


def test_a_start_without_the_speaker_plan_is_remembered(case):
    adopted = _adopted(case)
    revision_id = _lost_plan_revision(case)
    generation = case["services"]["generation"]
    with pytest.raises(ValueError, match="no adopted speaker plan"):
        generation.start(case["session_id"], run_override=_cast_override())
    started = generation.start(
        case["session_id"], run_override=_cast_override(), allow_without_speaker_plan=True
    )
    assert started["id"]
    with case["services"]["database"].session() as session:
        assert plans.missing_speaker_plan(
            session, revision_id, {"casting_enabled": True}
        ) is None
        settings = session.get(m.GenerationPlanRevision, revision_id).settings_json
        assert settings[plans.SPEAKER_PLAN_WAIVER_KEY] == {"plan_id": adopted["id"]}
    preview = generation.preview_selection(
        case["session_id"], run_override=_cast_override()
    )
    assert preview["missing_speaker_plan"] is None
    # A second start without the flag is not refused by this guard.
    restarted = generation.start(case["session_id"], run_override=_cast_override())
    assert restarted["id"] != started["id"]


def test_a_preview_or_refused_start_does_not_store_the_choice(case):
    _adopted(case)
    revision_id = _lost_plan_revision(case)
    generation = case["services"]["generation"]
    preview = generation.preview_selection(
        case["session_id"], run_override=_cast_override()
    )
    assert preview["missing_speaker_plan"] is not None
    prepared = generation.prepare_start(
        case["session_id"], run_override=_cast_override(), allow_without_speaker_plan=True
    )
    try:
        assert prepared["speaker_plan_waiver"]
    finally:
        generation.cleanup_prepared_start(prepared)
    with case["services"]["database"].session() as session:
        assert not plans.speaker_plan_waived(session, revision_id)


def test_the_choice_follows_topology_edits(case):
    adopted = _adopted(case)
    revision_id = _lost_plan_revision(case)
    with case["services"]["database"].session() as session:
        plans.waive_speaker_plan(session, revision_id, adopted["id"])
    split = _revise(
        case, revision_id, action="split",
        segment_id=_first_segment_id(case, revision_id), cursor=3, text_layer="display",
    )
    with case["services"]["database"].session() as session:
        assert plans.speaker_plan_waived(session, split["plan_revision_id"])
    with case["services"]["database"].immediate_session() as session:
        staged = case["services"]["generation"].revise_topology_in_session(
            session,
            case["session_id"],
            split["plan_revision_id"],
            {
                "action": "split",
                "segment_id": _last_segment_id(case, split["plan_revision_id"]),
                "cursor": 10,
                "text_layer": "display",
            },
            activate=False,
        )
        assert plans.speaker_plan_waived(session, staged["plan_revision_id"])


def test_restoring_an_unacknowledged_revision_is_not_acknowledged(case):
    adopted = _adopted(case)
    first = _lost_plan_revision(case)
    second = _revise(
        case, first, action="split",
        segment_id=_first_segment_id(case, first), cursor=3, text_layer="display",
    )["plan_revision_id"]
    with case["services"]["database"].session() as session:
        plans.waive_speaker_plan(session, second, adopted["id"])
    restored = _revise(
        case, second, action="restore", target_revision_id=first
    )["plan_revision_id"]
    with case["services"]["database"].session() as session:
        # The restore writes the target's settings, which never held the choice.
        assert not plans.speaker_plan_waived(session, restored)
        with pytest.raises(ValueError, match="no adopted speaker plan"):
            plans.refuse_missing_speaker_plan(
                session, restored, {"casting_enabled": True}
            )


def test_storing_the_choice_keeps_the_snapshot_guard(case):
    from pandrator.web.generation_start_preparation import snapshot_guard

    adopted = _adopted(case)
    revision_id = _lost_plan_revision(case)
    with case["services"]["database"].session() as session:
        before = snapshot_guard(session, case["session_id"], revision_id)
        plans.waive_speaker_plan(session, revision_id, adopted["id"])
        session.flush()
        after = snapshot_guard(session, case["session_id"], revision_id)
    assert before == after


def test_the_guard_is_off_without_casting_or_with_performance(case):
    _adopted(case)
    revision_id = _lost_plan_revision(case)
    base = {"service": "gemini", "model": "gemini-2.5-flash-tts", "voice": "Kore", "language": "en"}
    with case["services"]["database"].session() as session:
        # Casting on: this is the freeze the guard protects.
        with pytest.raises(ValueError, match="no adopted speaker plan"):
            freeze_speech_snapshot(
                session, revision_id, {"tts": {**base, "casting_enabled": True}}
            )
        # No casting: a lost plan changes nothing about how blocks are voiced.
        freeze_speech_snapshot(
            session, revision_id, {"tts": {**base, "casting_enabled": False}}
        )
        # With performance on the refusal is the performance one.
        with pytest.raises(ValueError, match="no adopted performance plan"):
            freeze_speech_snapshot(
                session,
                revision_id,
                {"tts": {**base, "casting_enabled": True, "performance_enabled": True}},
            )


def _set_tts(case, **values):
    """Merge ``values`` into the session's saved TTS settings at their current revision."""
    with case["services"]["database"].session() as session:
        row = session.get(m.SessionSetting, (case["session_id"], "tts"))
        revision, current = (row.revision, dict(row.value_json)) if row else (0, {})
    case["services"]["workspace_settings"].update(
        case["session_id"], "tts", revision, {**current, **values}
    )


def test_performance_on_error_names_the_lineage_plan(case):
    adopted = _adopted(case)
    split = case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][1], "cursor": 3, "text_layer": "display"},
    )
    split_id = split["plan_revision_id"]
    _drop_carried_plan(case, split_id)
    with case["services"]["database"].session() as session:
        named = plans.no_adopted_performance_plan_message(session, split_id)
        bare = plans.no_adopted_performance_plan_message(session, case["revision_id"])
        # The run-start freeze raises the named message.
        with pytest.raises(ValueError) as frozen:
            plans.freeze_performance_snapshot(
                session, split_id, {"tts": {"performance_enabled": True}}
            )
    assert "no adopted performance plan" in named
    assert adopted["id"] in named
    assert named.endswith("Copy it to this revision and adopt it, or disable performance.")
    assert bare == (
        "Performance is enabled, but this speech plan has no adopted performance plan. "
        "Adopt one or disable performance."
    )
    assert str(frozen.value) == named
    # So does the block preview on that revision.
    services = case["services"]
    _set_tts(case, service="openai", voice="voice-a", language="en", performance_enabled=True)
    with services["database"].session() as session:
        segment_id = session.scalar(
            select(m.GenerationSegment.id)
            .where(m.GenerationSegment.plan_revision_id == split_id)
            .order_by(m.GenerationSegment.ordinal)
        )
    with pytest.raises(ValueError) as previewed:
        preview_speech_segment(
            services, case["session_id"], revision_id=split_id, segment_id=segment_id
        )
    assert str(previewed.value) == named
    # And the selection inspector, whose preview reuses the plan it has read.
    from pandrator.web.speech_selection import get_speech_selection_context

    with services["database"].session() as session:
        revision = session.get(m.GenerationSegment, segment_id).revision
        inspected = get_speech_selection_context(
            services,
            session,
            case["session_id"],
            {
                "revision_id": split_id,
                "segment_id": segment_id,
                "expected_segment_revision": revision,
                "start": 0,
                "end": 1,
            },
        )
    assert inspected["preview"] is None
    assert inspected["preview_error"] == named


def test_copying_the_named_plan_onto_the_split_revision_clears_the_notice(case):
    adopted = _adopted(case)
    generation = case["services"]["generation"]
    split = generation.revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][1], "cursor": 3, "text_layer": "display"},
    )
    _drop_carried_plan(case, split["plan_revision_id"])
    listing = case["client"].get(
        case["base"] + "?plan_revision_id=" + split["plan_revision_id"]
    ).get_json()
    assert listing["items"] == []
    assert listing["ancestor_adopted_plan"]["plan_id"] == adopted["id"]
    response = case["post"](
        "",
        {
            "expected_plan_revision_id": split["plan_revision_id"],
            "copy_from_id": adopted["id"],
        },
    )
    assert response.status_code == 201, response.get_json()
    copied = response.get_json()
    # The first block maps one to one with its context. The split halves need
    # review, and so does the last block: it was planned with the whole line
    # before it as context and now follows only its second half.
    assert (copied["copied_count"], copied["skipped_count"]) == (1, 2)
    adopt(case, copied)
    preview = generation.preview_selection(case["session_id"], run_override=_cast_override())
    assert preview["missing_speaker_plan"] is None
    listing = case["client"].get(
        case["base"] + "?plan_revision_id=" + split["plan_revision_id"]
    ).get_json()
    assert listing["ancestor_adopted_plan"] is None


def test_start_route_names_the_lost_speaker_plan_and_accepts_the_acknowledgement(case):
    adopted = _adopted(case)
    split = case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][1], "cursor": 3, "text_layer": "display"},
    )
    _drop_carried_plan(case, split["plan_revision_id"])
    path = f"/api/v1/sessions/{case['session_id']}/generation-runs"
    refused = case["client"].post(
        path, json={"run_override": _cast_override()}, headers=case["headers"]
    )
    assert refused.status_code == 409
    assert adopted["id"] in refused.get_json()["error"]["message"]
    started = case["client"].post(
        path,
        json={"run_override": _cast_override(), "allow_without_speaker_plan": True},
        headers=case["headers"],
    )
    assert started.status_code == 202, started.get_json()


def test_a_stale_ancestor_plan_can_still_be_copied_across_a_split(case):
    adopted = _adopted(case)
    _change(case, 0, text="The request was rejected outright.")
    split = case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][0], "cursor": 3, "text_layer": "display"},
    )
    _drop_carried_plan(case, split["plan_revision_id"])
    with case["services"]["database"].session() as session:
        notice = plans.ancestor_adopted_plan(
            session, split["plan_revision_id"], session_id=case["session_id"]
        )
        assert notice is not None and notice["plan_id"] == adopted["id"]
        # Another project cannot read it through this revision.
        assert plans.ancestor_adopted_plan(
            session, split["plan_revision_id"], session_id="another-session"
        ) is None
    response = case["post"](
        "",
        {"expected_plan_revision_id": split["plan_revision_id"], "copy_from_id": adopted["id"]},
    )
    assert response.status_code == 201, response.get_json()
    copied = response.get_json()
    # The edited block is gone; the locked middle entry and the last block,
    # whose neighbour did not change, map one to one onto the split revision.
    assert (copied["copied_count"], copied["skipped_count"]) == (2, 1)
    items = _items(copied)
    assert items[split["lineage"][case["segment_ids"][1]][0]]["annotation"] is not None
    assert items[split["lineage"][case["segment_ids"][2]][0]]["annotation"] is not None


def _split_first_block(case):
    return case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][0], "cursor": 3, "text_layer": "display"},
    )


@pytest.mark.parametrize("neighbour_changed", [False, True])
def test_copy_across_revisions_compares_the_target_context(case, neighbour_changed):
    adopted = _adopted(case, locked=False)
    split = _split_first_block(case)
    first, middle, last = (split["lineage"][key] for key in case["segment_ids"])
    if neighbour_changed:
        # Only the target revision changes: the plan's own revision still
        # matches it, so the source looks current.
        with case["services"]["database"].session() as session:
            session.get(m.GenerationSegment, middle[0]).text = "Mary said nothing at all."
    response = case["post"](
        "",
        {"expected_plan_revision_id": split["plan_revision_id"], "copy_from_id": adopted["id"]},
    )
    assert response.status_code == 201, response.get_json()
    copied = response.get_json()
    items = _items(copied)
    # The last block was planned with the middle line as context.
    assert (items[last[0]]["annotation"] is None) is neighbour_changed
    # The middle block follows half of the split line now, so it needs review
    # either way; the split halves are new blocks.
    assert items[middle[0]]["annotation"] is None
    expected = (0, 3) if neighbour_changed else (1, 2)
    assert (copied["copied_count"], copied["skipped_count"]) == expected


def test_copy_of_stale_plan_ignores_an_oversized_source_block_it_drops(case):
    adopted = _adopted(case)
    long_text = "word " * 4801
    _change(case, 0, text=long_text)
    split = case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][0], "cursor": 12000, "text_layer": "display"},
    )
    response = case["post"](
        "",
        {"expected_plan_revision_id": split["plan_revision_id"], "copy_from_id": adopted["id"]},
    )
    assert response.status_code == 201, response.get_json()
    copied = response.get_json()
    # The oversized block is a changed block; the locked middle entry and the
    # last block, whose neighbour did not change, still map.
    assert (copied["copied_count"], copied["skipped_count"]) == (2, 1)


def test_an_ancestor_plan_far_back_in_the_history_can_be_named_and_copied(case):
    adopted = _adopted(case)
    split = case["services"]["generation"].revise_topology(
        case["session_id"],
        case["revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][1], "cursor": 3, "text_layer": "display"},
    )
    _drop_carried_plan(case, split["plan_revision_id"])
    # Put 120 revisions that change nothing between the plan's revision and
    # the split, more than the copy's old walk allowed.
    with case["services"]["database"].session() as session:
        origin = session.get(m.GenerationPlanRevision, case["revision_id"])
        parent = origin.id
        for step in range(120):
            revision = m.GenerationPlanRevision(
                plan_id=origin.plan_id,
                parent_revision_id=parent,
                revision_number=10_000 + step,
                settings_json={},
                operation_json={
                    "action": "edit",
                    "lineage": {key: [key] for key in case["segment_ids"]},
                },
                content_hash=f"history-{step}",
            )
            session.add(revision)
            session.flush()
            parent = revision.id
        session.get(m.GenerationPlanRevision, split["plan_revision_id"]).parent_revision_id = parent
    with case["services"]["database"].session() as session:
        notice = plans.ancestor_adopted_plan(session, split["plan_revision_id"])
        assert notice is not None and notice["plan_id"] == adopted["id"]
    response = case["post"](
        "",
        {"expected_plan_revision_id": split["plan_revision_id"], "copy_from_id": adopted["id"]},
    )
    assert response.status_code == 201, response.get_json()
    assert (response.get_json()["copied_count"], response.get_json()["skipped_count"]) == (1, 2)


def _revise(case, revision_id, **operation):
    """One topology revision through the public service."""
    return case["services"]["generation"].revise_topology(
        case["session_id"], revision_id, operation
    )


def _segment_markup(case, segment_id):
    """The markup the revision wrote for a block, before any plan."""
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, segment_id)
        return (row.speech_plan_json or {}).get("speech_xml")


def _segment_spoken(case, segment_id):
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, segment_id)
        return row.optimized_text or row.text or ""


def _entry_structure(case, segment_id, markup):
    """An entry's public structure, without its own block ID and markup."""
    parsed = parse_speech_markup(
        markup,
        expected_segment_id=segment_id,
        expected_text=_segment_spoken(case, segment_id),
    )
    return {
        key: value
        for key, value in parsed.public().items()
        if key not in {"segment_id", "xml"}
    }


def _freeze(case, revision_id):
    snapshot: dict = {}
    with case["services"]["database"].session() as session:
        plans.freeze_performance_snapshot(session, revision_id, snapshot)
    return snapshot


def test_a_split_carries_an_xml_plan_without_a_new_review(case):
    adopted = _adopted_xml(case)
    first, middle, last = case["segment_ids"]
    result = _revise(
        case, case["revision_id"], action="split", segment_id=last, cursor=18,
        text_layer="display",
    )
    assert result["review_required"]["performance"] is False
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert carried["status"] == "adopted" and not carried["stale"]
    assert carried["carried_from_id"] == adopted["id"]
    assert carried["plan_revision_id"] == result["plan_revision_id"]
    assert [item["id"] for item in carried["items"]] == result["segment_ids"]
    source, items = _items(adopted), _items(carried)
    # Each half's entry is the markup the split wrote for it.
    for half in result["lineage"][last]:
        assert items[half]["speech_xml"] == _segment_markup(case, half)
    # Every untouched block keeps its entry with only the block ID changed.
    for old in (first, middle):
        (new,) = result["lineage"][old]
        assert items[new]["speech_xml"] == source[old]["speech_xml"].replace(
            f'id="{old}"', f'id="{new}"'
        )
        assert items[new]["annotation"] == source[old]["annotation"]
    snapshot = _freeze(case, result["plan_revision_id"])
    assert snapshot["performance_snapshot"]["id"] == carried["id"]
    for item in carried["items"]:
        assert plans.speech_markup_for_segment(
            snapshot, item["id"], item["spoken_text"]
        ) is not None


def test_a_merge_carries_an_xml_plan_without_a_new_review(case):
    adopted = _adopted_xml(case)
    first, middle, last = case["segment_ids"]
    result = _revise(
        case, case["revision_id"], action="merge",
        left_segment_id=first, right_segment_id=middle,
    )
    assert result["review_required"]["performance"] is False
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert carried["status"] == "adopted" and not carried["stale"]
    assert carried["carried_from_id"] == adopted["id"]
    (merged,) = result["lineage"][first]
    assert result["lineage"][middle] == [merged]
    source, items = _items(adopted), _items(carried)
    assert items[merged]["speech_xml"] == _segment_markup(case, merged)
    (new_last,) = result["lineage"][last]
    assert items[new_last]["speech_xml"] == source[last]["speech_xml"].replace(
        f'id="{last}"', f'id="{new_last}"'
    )
    snapshot = _freeze(case, result["plan_revision_id"])
    for item in carried["items"]:
        assert plans.speech_markup_for_segment(
            snapshot, item["id"], item["spoken_text"]
        ) is not None


def test_a_batch_of_edits_carries_an_xml_plan(case):
    adopted = _adopted_xml(case)
    first, middle, last = case["segment_ids"]
    revision = case["revision_id"]
    response = case["client"].post(
        f"/api/v1/sessions/{case['session_id']}/generation-plan/topology/batch",
        json={
            "expected_revision_id": revision,
            "operations": [
                {"action": "split", "segment_id": last, "cursor": 18},
                {"action": "merge", "left_segment_id": first, "right_segment_id": middle},
            ],
        },
        headers={
            **case["headers"],
            "If-Match": f'"{revision}"',
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 201, response.get_json()
    result = response.get_json()
    assert result["review_required"]["performance"] is False
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert carried["status"] == "adopted" and not carried["stale"]
    assert carried["carried_from_id"] == adopted["id"]
    # The merge and the two halves: one entry for every block.
    assert carried["total"] == result["segment_count"]
    assert carried["review_segment_count"] == 0
    _freeze(case, result["plan_revision_id"])


def test_a_split_then_merge_returns_the_original_markup(case):
    # A plain XML entry: the split writes each half's markup, the merge joins
    # them back, and the carried entry must read as the one it came from.
    first, middle, last = case["segment_ids"]
    plan = create(case, annotation_format="xml")
    items = [
        {
            "segment_id": identifier,
            "speech_xml": f'<segment id="{identifier}">{text}</segment>',
        }
        for identifier, text in zip(
            (first, middle, last),
            (
                "The request was rejected.",
                "Yet this defeat would be temporary.",
                "Membership doubled soon afterwards.",
            ),
            strict=True,
        )
    ]
    edited = case["post"](
        "/" + plan["id"],
        {"expected_version": plan["version"], "items": items},
        method="patch",
    )
    assert edited.status_code == 200, edited.get_json()
    adopted = adopt(case, get(case, plan))
    split = _revise(
        case, case["revision_id"], action="split", segment_id=last, cursor=18,
        text_layer="display",
    )
    left, right = split["lineage"][last]
    merged = _revise(
        case, split["plan_revision_id"], action="merge",
        left_segment_id=left, right_segment_id=right,
    )
    (new,) = merged["lineage"][left]
    carried = get(case, {"id": merged["carried_performance_plan_id"]})
    original = _items(adopted)[last]["speech_xml"]
    assert _entry_structure(case, new, _items(carried)[new]["speech_xml"]) == (
        _entry_structure(case, last, original)
    )


def test_a_split_of_a_legacy_plan_marks_only_the_new_blocks_for_review(case):
    adopted = _adopted(case)
    first, middle, last = case["segment_ids"]
    result = _revise(
        case, case["revision_id"], action="split", segment_id=last, cursor=10,
        text_layer="display",
    )
    assert result["review_required"]["performance"] is True
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert carried["status"] == "adopted" and carried["stale"]
    assert carried["carried_from_id"] == adopted["id"]
    halves = result["lineage"][last]
    assert carried["review_segment_count"] == 2
    assert {segment["id"] for segment in carried["review_segments"]} == set(halves)
    source, items = _items(adopted), _items(carried)
    for old in (first, middle):
        (new,) = result["lineage"][old]
        assert items[new]["annotation"] == source[old]["annotation"]


def test_a_split_carries_a_stale_plan_block_by_block(case):
    adopted = _adopted_xml(case)
    first, middle, last = case["segment_ids"]
    # One fitted edit and one that needs review, as the stale-plan tests use.
    _change(case, 0, text="The request was firmly rejected.")
    _change(case, 1, text="Temporary.")
    source = get(case, adopted)
    assert source["stale"] and [segment["id"] for segment in source["review_segments"]] == [middle]
    result = _revise(
        case, case["revision_id"], action="split", segment_id=last, cursor=18,
        text_layer="display",
    )
    assert result["review_required"]["performance"] is True
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert carried["stale"] and carried["carried_from_id"] == adopted["id"]
    (new_middle,) = result["lineage"][middle]
    assert [segment["id"] for segment in carried["review_segments"]] == [new_middle]
    items = _items(carried)
    for half in result["lineage"][last]:
        assert items[half]["speech_xml"] == _segment_markup(case, half)


def test_a_split_of_a_block_that_needs_review_stays_in_review(case):
    adopted = _adopted_xml(case)
    middle = case["segment_ids"][1]
    _change(case, 1, speaker="B")
    assert get(case, adopted)["review_segment_count"] == 1
    result = _revise(
        case, case["revision_id"], action="split", segment_id=middle, cursor=5,
        text_layer="display",
    )
    assert result["review_required"]["performance"] is True
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert carried["carried_from_id"] == adopted["id"]
    assert carried["review_segment_count"] == 2
    assert {segment["id"] for segment in carried["review_segments"]} == set(
        result["lineage"][middle]
    )


def test_a_locked_entry_stays_locked_on_both_halves(case):
    _adopted_xml(case)
    first = case["segment_ids"][0]
    result = _revise(
        case, case["revision_id"], action="split", segment_id=first, cursor=11,
        text_layer="display",
    )
    items = _items(get(case, {"id": result["carried_performance_plan_id"]}))
    for half in result["lineage"][first]:
        assert items[half]["annotation"]["locked"] is True


def test_a_carried_legacy_plan_made_before_structure_fields_reviews_only_the_new_blocks(case):
    adopted = _adopted(case)
    with case["services"]["database"].session() as session:
        plan = session.get(m.PerformancePlan, adopted["id"])
        plan.units_json = [
            {
                key: value
                for key, value in unit.items()
                if key not in {"speaker", "language", "node_kind", "section_id"}
            }
            for unit in plan.units_json
        ]
    last = case["segment_ids"][2]
    result = _revise(
        case, case["revision_id"], action="split", segment_id=last, cursor=10,
        text_layer="display",
    )
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert carried["stale"] and carried["carried_from_id"] == adopted["id"]
    assert carried["review_segment_count"] == 2
    assert {segment["id"] for segment in carried["review_segments"]} == set(
        result["lineage"][last]
    )


def test_a_staged_revision_carries_the_plan(case):
    adopted = _adopted_xml(case)
    generation = case["services"]["generation"]
    with case["services"]["database"].immediate_session() as session:
        result = generation.revise_topology_in_session(
            session,
            case["session_id"],
            case["revision_id"],
            {
                "action": "split",
                "segment_id": case["segment_ids"][2],
                "cursor": 18,
                "text_layer": "display",
            },
            activate=False,
        )
        carried = session.scalar(
            select(m.PerformancePlan).where(
                m.PerformancePlan.plan_revision_id == result["plan_revision_id"],
                m.PerformancePlan.status == "adopted",
            )
        )
        assert carried is not None
        assert carried.settings_json["carried_from_plan_id"] == adopted["id"]
        assert [unit["id"] for unit in carried.units_json] == result["segment_ids"]
