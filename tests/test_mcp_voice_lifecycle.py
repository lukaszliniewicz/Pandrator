from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.schemas.voice_lifecycle import (
    AuditionVoiceInput,
    GetVoiceSamplesInput,
    TranscribeVoiceSampleInput,
    UpdateCatalogVoiceMetadataInput,
    VoiceCatalogCapabilitiesInput,
    VoiceCatalogInput,
    VoiceReferenceInput,
)
from pandrator_mcp.tools.voice_lifecycle import (
    audition_voice,
    get_voice_samples,
    transcribe_voice_sample,
    voice_catalog,
    voice_catalog_capabilities,
)


def test_voice_reference_requires_exact_managed_or_provider_shape():
    assert VoiceReferenceInput(kind="managed", voice_id="voice-1").model_dump(
        exclude_none=True
    ) == {"kind": "managed", "voice_id": "voice-1"}
    assert VoiceReferenceInput(
        kind="provider", service_id="s", model="m", voice="v"
    ).model_dump(exclude_none=True)["voice"] == "v"
    with pytest.raises(ValueError):
        VoiceReferenceInput(kind="managed", voice_id="voice-1", model="m")
    with pytest.raises(ValueError):
        VoiceReferenceInput(kind="provider", service_id="s", model="m")


def test_catalog_metadata_is_provider_only_and_requires_a_change():
    with pytest.raises(ValueError):
        UpdateCatalogVoiceMetadataInput(
            reference={"kind": "managed", "voice_id": "v"},
            expected_revision=0,
            changes={},
            idempotency_key="catalog:metadata:1",
        )
    with pytest.raises(ValueError):
        UpdateCatalogVoiceMetadataInput(
            reference={"kind": "managed", "voice_id": "v"},
            expected_revision=0,
            changes={"name": "Voice"},
            idempotency_key="catalog:metadata:2",
        )


def test_audition_returns_safe_work_reference_and_poll_action():
    application = Mock()
    application.audition_voice.return_value = {
        "id": "job-1",
        "state": "queued",
        "payload": {"settings": {"api_key": "secret"}},
    }
    runtime = SimpleNamespace(require_application=lambda: application)
    outcome = audition_voice(
        runtime,
        AuditionVoiceInput(
            text="Hello",
            service_id="service-a",
            model="model-a",
            idempotency_key="audition:voice:1",
        ),
    )
    assert outcome.work is not None
    assert outcome.work.id == "job-1"
    assert outcome.next_actions[0].tool == "pandrator_get_work"
    assert "payload" not in str(outcome.result)
    assert "secret" not in str(outcome.result)


def test_voice_client_uses_encoded_fixed_paths_and_headers():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={"id": "job-1", "state": "queued"})
    client.publish_voice(
        "voice/a",
        "service/b",
        expected_revision=3,
        idempotency_key="voice:publish:1",
    )
    args, kwargs = client._request_json.call_args
    path = args[0]
    assert path.endswith("/voices/voice%2Fa/providers/service%2Fb")
    assert kwargs["method"] == "POST"
    assert kwargs["if_match_revision"] == 3
    assert kwargs["idempotency_key"] == "voice:publish:1"


def test_sample_projection_keeps_native_transcript_and_file_fields():
    application = Mock()
    application.get_voice_samples.return_value = {
        "voice_revision": 4,
        "items": [
            {
                "id": "sample-1",
                "voice_id": "voice-1",
                "artifact_id": "artifact-1",
                "transcript": "Hello",
                "transcript_language": "en",
                "file_status": "ready",
                "available": True,
                "voice_revision": 4,
                "provider_secret": "must not pass",
            }
        ],
    }
    runtime = SimpleNamespace(require_application=lambda: application)
    result = get_voice_samples(runtime, GetVoiceSamplesInput(voice_id="voice-1"))
    sample = result["items"][0]
    assert sample["transcript_language"] == "en"
    assert sample["file_status"] == "ready"
    assert sample["available"] is True
    assert sample["voice_revision"] == 4
    assert "provider_secret" not in sample


def test_capabilities_and_catalog_preserve_schema_and_collection_summaries():
    application = Mock()
    application.voice_catalog_capabilities.return_value = {
        "schema_version": "1",
        "voice_profile_schema_version": 1,
        "models": [
            {"service_id": "svc", "model": "model-a", "available": True},
            {"service_id": "other", "model": "model-a", "available": False},
        ],
    }
    application.voice_catalog.return_value = {
        "schema_version": "1",
        "items": [],
        "collections": [
            {"id": "c-1", "name": "Favorites", "revision": 2, "member_count": 3}
        ],
    }
    runtime = SimpleNamespace(require_application=lambda: application)
    capabilities = voice_catalog_capabilities(
        runtime,
        VoiceCatalogCapabilitiesInput(service_id="svc"),
    )
    assert capabilities["voice_profile_schema_version"] == 1
    assert [item["service_id"] for item in capabilities["models"]] == ["svc"]
    catalog = voice_catalog(runtime, VoiceCatalogInput())
    assert catalog["collections"][0]["member_count"] == 3


def test_transcription_exposes_bounded_work_result_for_transcript_review():
    application = Mock()
    application.transcribe_voice_sample.return_value = {
        "id": "job-1",
        "state": "succeeded",
        "result": {
            "sample_id": "sample-1",
            "artifact_id": "artifact-transcript",
            "word_timestamps_artifact_id": "artifact-words",
            "transcript": "Hello from the completed transcription.",
            "path": "/private/server/path.srt",
            "settings": {"api_key": "secret"},
        },
    }
    runtime = SimpleNamespace(require_application=lambda: application)
    outcome = transcribe_voice_sample(
        runtime,
        TranscribeVoiceSampleInput(
            voice_id="voice-1",
            sample_id="sample-1",
            idempotency_key="transcribe:voice:1",
        ),
    )
    assert outcome.result["transcript"] == "Hello from the completed transcription."
    assert outcome.result["word_timestamps_artifact_id"] == "artifact-words"
    assert "path" not in str(outcome.result)
    assert "secret" not in str(outcome.result)
    assert outcome.next_actions[0].tool == "pandrator_get_work"
