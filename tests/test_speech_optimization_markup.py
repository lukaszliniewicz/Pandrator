"""Focused passive speech-optimization markup contract checks."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from pandrator.web.dispatch import DispatchError
from pandrator.web.models import utcnow
from pandrator.web.schemas import (
    SpeechOptimizationDispatchBatchSubmitRequest,
    SpeechOptimizationDispatchRunCreateRequest,
)
from pandrator.web.speech_optimization_dispatch import (
    SpeechOptimizationDispatchRunService,
)
from pandrator_mcp.schemas.speech_optimization_dispatch import (
    CreateSpeechOptimizationDispatchRunInput,
    SpeechOptimizationDispatchItemInput,
    SpeechOptimizationDispatchResultInput,
)
from pandrator_mcp.tools.speech_optimization_dispatch import _claim


def _batch(*, text: str = "Hello", speech_xml: str | None = None):
    unit = {"unit_id": 1, "text": text, "language": "en", "speaker": None}
    if speech_xml is not None:
        unit["speech_xml"] = speech_xml
    return SimpleNamespace(input_json={"units": [unit]})


def test_annotation_only_requires_exact_source_text_and_canonicalizes_xml():
    xml = '<segment id="1"><dialogue>Hello</dialogue></segment>'
    normalized = SpeechOptimizationDispatchRunService._normalize_result(
        _batch(text="Hello", speech_xml=xml),
        {
            "kind": "speech_optimization",
            "items": [{"unit_id": 1, "text": "Hello"}],
        },
        annotation_only=True,
    )
    assert normalized[0]["text"] == "Hello"
    assert normalized[0]["speech_xml"] == xml

    xml_only = SpeechOptimizationDispatchRunService._normalize_result(
        _batch(text="Hello", speech_xml=xml),
        {
            "kind": "speech_optimization",
            "items": [{"unit_id": 1, "speech_xml": xml}],
        },
        annotation_only=True,
    )
    assert xml_only[0]["text"] == "Hello"
    assert xml_only[0]["speech_xml"] == xml

    with pytest.raises(DispatchError, match="preserve the source text exactly"):
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello", speech_xml=xml),
            {
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "text": "Hello!"}],
            },
            annotation_only=True,
        )


def test_supplied_markup_must_survive_text_rewrite_and_unknown_speaker_is_rejected():
    source_xml = '<segment id="1"><speaker ref="c-missing">Hello</speaker></segment>'
    with pytest.raises(DispatchError, match="unresolved speaker"):
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello", speech_xml=source_xml),
            {
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "text": "Hello"}],
            },
            characters=[],
        )


def test_passive_annotation_cannot_strip_authored_markup():
    source_xml = '<segment id="generation-guid"><speaker ref="c-alice" voice="voice-a">Hello</speaker></segment>'
    with pytest.raises(DispatchError, match="authored speech markup"):
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello", speech_xml=source_xml),
            {
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "speech_xml": '<segment id="1">Hello</segment>'}],
            },
            characters=[
                {
                    "id": "c-alice",
                    "display_name": "Alice",
                    "voice_category": "female",
                }
            ],
        )

    with pytest.raises(DispatchError, match="revise the clean text"):
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello", speech_xml=source_xml),
            {
                "kind": "speech_optimization",
                "items": [
                    {
                        "unit_id": 1,
                        "speech_xml": '<segment id="1"><speaker ref="c-alice" voice="voice-a">Hi</speaker></segment>',
                    }
                ],
            },
            characters=[
                {
                    "id": "c-alice",
                    "display_name": "Alice",
                    "voice_category": "female",
                }
            ],
        )

    with pytest.raises(DispatchError, match="markup would be lost"):
        SpeechOptimizationDispatchRunService._normalize_result(
            _batch(text="Hello", speech_xml='<segment id="1">Hello</segment>'),
            {
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "text": "Hi"}],
            },
        )


def test_web_and_mcp_requests_expose_independent_markup_controls_and_proposals():
    web = SpeechOptimizationDispatchRunCreateRequest(
        annotation_mode="speakers",
        annotation_only=True,
    )
    assert web.annotation_mode == "speakers"
    assert web.annotation_only is True
    submit = SpeechOptimizationDispatchBatchSubmitRequest(
        lease_token="lease",
        result={"kind": "speech_optimization", "items": [{"unit_id": 1, "text": "x"}]},
        character_proposals=[{"id": "c-alice", "display_name": "Alice"}],
    )
    assert submit.character_proposals[0]["id"] == "c-alice"
    mcp = CreateSpeechOptimizationDispatchRunInput(
        session_id="session",
        annotation_mode="dialogue",
        annotation_only=False,
        idempotency_key="speech:create-xml",
    )
    assert mcp.annotation_mode == "dialogue"
    assert "speech_xml" in SpeechOptimizationDispatchItemInput.model_fields
    assert "speech_xml" in SpeechOptimizationDispatchResultInput.model_fields["items"].annotation.__args__[0].model_fields


def test_mcp_claim_projection_keeps_dictionary_and_unit_markup_without_private_fields():
    payload = {
        "run_id": "run-1",
        "batch_id": "batch-1",
        "task": {
            "kind": "speech_optimization",
            "output_role": "tts_optimized",
            "annotation_mode": "speakers",
            "annotation_only": False,
            "instructions": "Keep markup.",
        },
        "character_dictionary": {
            "revision": 4,
            "entries": [{"id": "c-alice", "display_name": "Alice"}],
        },
        "batch": {
            "valid_unit_ids": [1],
            "units": [
                {
                    "unit_id": 1,
                    "text": "Hello",
                    "language": "en",
                    "speech_xml": '<segment id="1">Hello</segment>',
                }
            ],
        },
    }
    projected = _claim(payload)
    assert projected["character_dictionary"]["revision"] == 4
    assert projected["character_dictionary"]["entries"][0]["id"] == "c-alice"
    assert projected["batch"]["units"][0]["speech_xml"].startswith("<segment")
    assert "private" not in projected


class _FakeSession:
    def __init__(self, batch):
        self.batch = batch
        self.flush_count = 0

    def get(self, _model, _batch_id):
        return self.batch

    def flush(self):
        self.flush_count += 1


def test_release_rejects_expired_lease_without_mutating_batch():
    old_updated_at = utcnow() - timedelta(minutes=2)
    batch = SimpleNamespace(
        id="batch-1",
        status="leased",
        lease_token="lease-1",
        claim_key="claim-1",
        lease_expires_at=utcnow() - timedelta(seconds=1),
        updated_at=old_updated_at,
    )
    session = _FakeSession(batch)

    with pytest.raises(DispatchError) as raised:
        SpeechOptimizationDispatchRunService.release_in_session(
            object(), session, batch_id=batch.id, lease_token="lease-1"
        )

    assert raised.value.code == "lease_expired"
    assert raised.value.retryable is True
    assert batch.status == "leased"
    assert batch.lease_token == "lease-1"
    assert batch.claim_key == "claim-1"
    assert batch.lease_expires_at < utcnow()
    assert batch.updated_at == old_updated_at
    assert session.flush_count == 0


def test_release_rejects_missing_expiry_as_expired():
    batch = SimpleNamespace(
        id="batch-missing-expiry",
        status="leased",
        lease_token="lease-missing-expiry",
        claim_key="claim-missing-expiry",
        lease_expires_at=None,
        updated_at=None,
    )
    session = _FakeSession(batch)

    with pytest.raises(DispatchError) as raised:
        SpeechOptimizationDispatchRunService.release_in_session(
            object(), session, batch_id=batch.id, lease_token=batch.lease_token
        )

    assert raised.value.code == "lease_expired"
    assert raised.value.retryable is True
    assert batch.status == "leased"
    assert batch.lease_token == "lease-missing-expiry"
    assert batch.claim_key == "claim-missing-expiry"
    assert batch.lease_expires_at is None
    assert batch.updated_at is None
    assert session.flush_count == 0


def test_release_resets_current_unexpired_lease():
    batch = SimpleNamespace(
        id="batch-2",
        status="leased",
        lease_token="lease-2",
        claim_key="claim-2",
        lease_expires_at=utcnow() + timedelta(minutes=5),
        updated_at=None,
    )
    session = _FakeSession(batch)

    released = SpeechOptimizationDispatchRunService.release_in_session(
        object(), session, batch_id=batch.id, lease_token="lease-2"
    )

    assert released == {
        "batch_id": "batch-2",
        "status": "ready",
        "lease_expires_at": None,
    }
    assert batch.status == "ready"
    assert batch.lease_token is None
    assert batch.claim_key is None
    assert batch.lease_expires_at is None
    assert batch.updated_at is not None
    assert session.flush_count == 1
