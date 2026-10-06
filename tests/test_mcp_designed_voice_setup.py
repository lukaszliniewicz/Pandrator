from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest
from pydantic import ValidationError

from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.voice_lifecycle import PromoteVoiceDesignInput
from pandrator_mcp.schemas.voice_setup import SetupDesignedVoiceInput
from pandrator_mcp.tools.voice_lifecycle import promote_voice_design, safe_sample_projection
from pandrator_mcp.tools.voice_setup import setup_designed_voice


def arguments():
    return SetupDesignedVoiceInput(
        voice_id="voice-1", artifact_id="design-1", transcript="Reviewed words.",
        transcript_reviewed=True, language="en", service_id="audio_cpp",
        expected_voice_revision=1, idempotency_key="original-setup-key",
    )


class DurableApplication:
    def __init__(self):
        self.preparations = {}
        self.publications = {}
        self.states = {"prepare-job": "running", "publish-job": "running"}
        self.sample = {"id": "sample-1", "artifact_id": "normalized-1", "sample_sha256": "a" * 64}
        self.prepare_receipt = {"sample_id": "sample-1", "artifact_id": "normalized-1", "sample_sha256": "a" * 64, "voice_revision": 2}
        self.publish_receipt = {"provider_voice_id": "linked-1", "voice_revision": 3, "linked": True, "reused_registration": False}
        self.fail_once = None

    def promote_voice_design(self, voice_id, **kwargs):
        if self.fail_once == "promote":
            self.fail_once = None
            self.preparations.setdefault(kwargs["idempotency_key"], kwargs)
            raise PandratorMcpError("application_response_timeout", "Response timed out", retryable=True)
        old = self.preparations.setdefault(kwargs["idempotency_key"], kwargs)
        if old != kwargs:
            raise PandratorMcpError("idempotency_conflict", "Changed preparation packet.")
        return {"id": "prepare-job", "status": "queued"}

    def get_work(self, work_id):
        state = self.states[work_id]
        result = self.prepare_receipt if work_id == "prepare-job" else self.publish_receipt
        return {"id": work_id, "state": state, "result_summary": result if state == "succeeded" else None,
                "error": {"code": "failed", "message": "Durable stage failure"} if state == "failed" else None}

    def get_voice_samples(self, voice_id):
        return {"voice_revision": 3, "items": [self.sample]}

    def publish_voice(self, voice_id, service_id, **kwargs):
        old = self.publications.setdefault(kwargs["idempotency_key"], kwargs)
        if old != kwargs:
            raise PandratorMcpError("idempotency_conflict", "Changed publication packet.")
        if self.fail_once == "publish":
            self.fail_once = None
            raise PandratorMcpError("application_response_timeout", "Response timed out", retryable=True)
        return {"id": "publish-job", "status": "queued"}


def runtime(application):
    return SimpleNamespace(require_application=lambda: application)


def test_recipe_resumes_two_jobs_using_unchanged_original_args_and_exact_pins():
    app = DurableApplication()
    args = arguments()
    preparing = setup_designed_voice(runtime(app), args)
    assert preparing.result["stage"] == "preparing_reference"
    assert preparing.work.id == "prepare-job"
    assert preparing.next_actions[0].arguments == args.model_dump(mode="json")
    assert not app.publications
    app.states["prepare-job"] = "succeeded"
    publishing = setup_designed_voice(runtime(app), args)
    assert publishing.result["stage"] == "publishing"
    assert publishing.work.id == "publish-job"
    publication = next(iter(app.publications.values()))
    assert publication["sample_id"] == "sample-1"
    assert publication["sample_sha256"] == "a" * 64
    assert publication["expected_revision"] == 2
    assert next(iter(app.preparations)) != next(iter(app.publications))
    assert max(len(key) for key in (*app.preparations, *app.publications)) <= 200
    app.states["publish-job"] = "succeeded"
    ready = setup_designed_voice(runtime(app), args)
    assert ready.result["stage"] == "ready"
    assert ready.result["provider_voice_id"] == "linked-1"
    assert "items" not in ready.result
    assert setup_designed_voice(runtime(app), args).result == ready.result
    assert len(app.preparations) == len(app.publications) == 1


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_failed_normalization_never_publishes_and_exposes_durable_log(state):
    app = DurableApplication()
    app.states["prepare-job"] = state
    result = setup_designed_voice(runtime(app), arguments())
    assert result.result["status"] == state
    assert result.work.id == "prepare-job"
    assert result.next_actions[0].tool == "pandrator_get_work_log"
    assert not app.publications


@pytest.mark.parametrize("stage", ["promote", "publish"])
def test_timeout_preserves_original_resume_packet_and_accepted_stage_key(stage):
    app = DurableApplication()
    app.fail_once = stage
    if stage == "publish":
        app.states["prepare-job"] = "succeeded"
    args = arguments()
    with pytest.raises(PandratorMcpError) as failure:
        setup_designed_voice(runtime(app), args)
    assert failure.value.code == "application_response_timeout"
    assert failure.value.next_actions[0].arguments == args.model_dump(mode="json")
    setup_designed_voice(runtime(app), args)
    assert len(app.preparations) == 1
    assert len(app.publications) == (1 if stage == "publish" else 0)


def test_recipe_uses_original_preparation_hash_without_a_mutable_sample_read():
    app = DurableApplication()
    app.states["prepare-job"] = "succeeded"
    app.sample["sample_sha256"] = "b" * 64
    app.sample["artifact_id"] = "another-artifact"
    app.get_voice_samples = mock.Mock(side_effect=AssertionError("Unnecessary sample lookup"))
    setup_designed_voice(runtime(app), arguments())
    assert next(iter(app.publications.values()))["sample_sha256"] == "a" * 64


def test_recipe_can_pin_an_older_preparation_receipt_without_a_hash():
    app = DurableApplication()
    app.states["prepare-job"] = "succeeded"
    app.prepare_receipt.pop("sample_sha256")
    setup_designed_voice(runtime(app), arguments())
    assert next(iter(app.publications.values()))["sample_sha256"] == app.sample["sample_sha256"]


def test_recipe_reused_preparation_skips_preparation_job_read():
    app = DurableApplication()
    app.promote_voice_design = mock.Mock(return_value={"status": "ready", "reused_reference": True, **app.prepare_receipt})
    app.states["publish-job"] = "succeeded"
    result = setup_designed_voice(runtime(app), arguments())
    assert result.result["stage"] == "ready"
    assert result.result["reused_reference"] is True


def test_promote_tool_accepts_immediate_reuse_receipt_without_work_handle():
    receipt = {"status": "ready", "sample_id": "sample-1", "artifact_id": "normalized-1", "sample_sha256": "a" * 64,
               "voice_revision": 2, "reused_reference": True, "path": "private/path.wav"}
    app = SimpleNamespace(promote_voice_design=lambda *args, **kwargs: receipt)
    result = promote_voice_design(runtime(app), PromoteVoiceDesignInput(
        voice_id="voice-1", artifact_id="design-1", transcript="words", language="en",
        expected_voice_revision=1, idempotency_key="promote-key",
    ))
    assert result.work is None
    assert result.result["reused_reference"]
    assert "path" not in result.result
    assert safe_sample_projection(receipt)["sample_sha256"] == "a" * 64


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_recipe_requires_literal_explicit_review(value):
    values = arguments().model_dump()
    values["transcript_reviewed"] = value
    with pytest.raises(ValidationError):
        SetupDesignedVoiceInput.model_validate(values)


@pytest.mark.parametrize("field,value", [
    ("voice_id", "another-voice"), ("artifact_id", "another-source"),
    ("transcript", "Changed reviewed words."), ("service_id", "another-service"),
])
def test_original_recipe_key_conflicts_on_changed_arguments_before_any_other_job(field, value):
    app = DurableApplication()
    args = arguments()
    setup_designed_voice(runtime(app), args)
    changed = SetupDesignedVoiceInput.model_validate({**args.model_dump(), field: value})
    with pytest.raises(PandratorMcpError) as failure:
        setup_designed_voice(runtime(app), changed)
    assert failure.value.code == "idempotency_conflict"
    assert len(app.preparations) == 1
    assert not app.publications


def test_publication_failure_exposes_durable_log_without_ready_result():
    app = DurableApplication()
    app.states.update({"prepare-job": "succeeded", "publish-job": "failed"})
    result = setup_designed_voice(runtime(app), arguments())
    assert result.result["stage"] == "publishing"
    assert result.result["status"] == "failed"
    assert result.work.id == "publish-job"
    assert result.next_actions[0].tool == "pandrator_get_work_log"


def test_completed_publication_without_registration_receipt_is_not_reported_ready():
    app = DurableApplication()
    app.states.update({"prepare-job": "succeeded", "publish-job": "succeeded"})
    app.publish_receipt = {}
    with pytest.raises(PandratorMcpError, match="no registration receipt"):
        setup_designed_voice(runtime(app), arguments())
