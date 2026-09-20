import tempfile
from unittest.mock import patch

import pytest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.voice_catalog import (
    VoiceCatalogQuery,
    catalog_entries,
    model_modes,
    query_catalog,
)
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def catalog_client():
    with tempfile.TemporaryDirectory() as directory:
        prepare_web_test_data_root(directory)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        app = create_app(data_root=directory, testing=True, bootstrap_tokens=bootstrap)
        client = app.test_client()
        csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()[
            "csrf_token"
        ]
        yield client, {"X-CSRF-Token": csrf}, app
        app.extensions["pandrator"]["database"].dispose()


def _voice(identifier, accent="Scottish", evidence=None):
    return {
        "id": identifier,
        "name": identifier,
        "language": "en",
        "revision": 1,
        "profile": {
            "pitch": "low",
            "languages": [
                {
                    "language": "en",
                    "accent": accent,
                    "evidence": evidence or {"source": "user", "status": "described"},
                }
            ],
        },
    }


def test_requested_accent_is_not_a_demonstrated_match():
    entries = catalog_entries(
        [
            _voice(
                "requested",
                evidence={"source": "design_request", "status": "requested"},
            ),
            _voice("described"),
            _voice("other", "American"),
        ],
        {},
        [],
    )
    result = query_catalog(entries, VoiceCatalogQuery(language="en", accent="Scottish"))
    assert [v["id"] for v in result["items"]] == ["described"]
    assert (
        query_catalog(
            entries, VoiceCatalogQuery(accent="Scottish", reviewed_only=True)
        )["total"]
        == 0
    )


def test_reviewed_accent_has_audition_evidence():
    entries = catalog_entries(
        [
            _voice(
                "reviewed",
                evidence={
                    "source": "audition_review",
                    "status": "reviewed",
                    "artifact_id": "audition",
                },
            )
        ],
        {},
        [],
    )

    assert (
        query_catalog(
            entries, VoiceCatalogQuery(accent="Scottish", reviewed_only=True)
        )["total"]
        == 1
    )


def test_model_modes_distinguish_design_cloning_and_direction():
    from pandrator.logic.speech_performance import decorate_service_capabilities

    models = [
        {
            "id": "qwen3_tts_1_7b_base_q8_0",
            "family": "qwen3_tts",
            "voice_mode": "cloning",
        },
        {
            "id": "qwen3_tts_1_7b_voicedesign_q8_0",
            "family": "qwen3_tts",
            "voice_mode": "design",
        },
        {
            "id": "breeze_tts_2_q8_0",
            "family": "breeze_tts",
            "voice_mode": "optional_cloning",
        },
    ]
    service = {
        "id": "audio_cpp",
        "adapter": "audio_cpp",
        "models": [m["id"] for m in models],
        "model_catalog": models,
    }
    decorate_service_capabilities(service)
    base, design, breeze = [model_modes(service, model) for model in models]
    assert base["cloning"] and not base["reference_with_instructions"]
    assert design["design"] and not design["cloning"]
    assert breeze["design"] and breeze["reference_with_instructions"]
    assert all(not modes["environmental_effects"] for modes in (base, design, breeze))


def test_pagination_has_no_duplicates_and_rejects_changed_filters():
    entries = catalog_entries([_voice(f"voice-{i}") for i in range(5)], {}, [])
    first = query_catalog(entries, VoiceCatalogQuery(limit=2))
    second = query_catalog(
        entries, VoiceCatalogQuery(limit=2, cursor=first["next_cursor"])
    )
    assert {v["key"] for v in first["items"]}.isdisjoint(
        v["key"] for v in second["items"]
    )
    with pytest.raises(ValueError, match="changed"):
        query_catalog(
            entries, VoiceCatalogQuery(query="voice", cursor=first["next_cursor"])
        )
    changed = [*entries, {**entries[0], "key": "new"}]
    with pytest.raises(ValueError, match="changed"):
        query_catalog(changed, VoiceCatalogQuery(cursor=first["next_cursor"]))


def test_model_language_support_is_separate_from_native_accent():
    model = "qwen3_tts_1_7b_customvoice_q8_0"
    entries = catalog_entries(
        [],
        {
            "services": [
                {
                    "id": "audio_cpp",
                    "models": [model],
                    "available": True,
                    "model_catalog": [
                        {"id": model, "family": "qwen3_tts", "voice_mode": "prebuilt"}
                    ],
                    "voice_catalogues": {model: ["Dylan", "Aiden"]},
                }
            ]
        },
        [],
    )
    result = query_catalog(entries, VoiceCatalogQuery(language="en"))
    assert result["total"] == 2
    dylan = next(v for v in result["items"] if v["id"] == "Dylan")
    assert dylan["profile"]["languages"][0]["accent"] == "Beijing"
    assert "unreviewed" in dylan["match_reasons"][-1]
    assert (
        query_catalog(entries, VoiceCatalogQuery(language="en", accent="Scottish"))[
            "total"
        ]
        == 0
    )


def test_provider_metadata_and_existing_clones_are_discoverable_per_model():
    entries = catalog_entries(
        [],
        {
            "services": [
                {
                    "id": "provider",
                    "available": True,
                    "models": ["cloning"],
                    "model_catalog": [
                        {"id": "cloning", "voice_mode": "cloning"},
                        {"id": "other-model", "voice_mode": "prebuilt"},
                    ],
                    "voices": ["speaker-1"],
                    "voice_catalogues": {"cloning": ["speaker-1"]},
                    "voice_metadata": {
                        "cloning:speaker-1": {
                            "name": "Scottish speaker",
                            "locale": "en-GB",
                            "gender": "Male",
                            "labels": {"accent": "Scottish"},
                            "description": "Provider description",
                        }
                    },
                }
            ]
        },
        [],
    )
    assert len(entries) == 1
    result = query_catalog(
        entries,
        VoiceCatalogQuery(language="en", accent="Scottish", voice_category="male"),
    )
    assert result["total"] == 1
    voice = result["items"][0]
    assert voice["name"] == "Scottish speaker"
    assert voice["reference"]["voice"] == "speaker-1"
    assert voice["profile"]["languages"][0]["evidence"]["source"] == "provider"
    assert voice["description"] == "Provider description"


def test_profile_roundtrip_preserves_provider_registration(catalog_client):
    client, headers, app = catalog_client
    voice = client.post(
        "/api/v1/voices",
        json={
            "name": "Scrooge",
            "language": "en",
            "voice_category": "male",
            "profile": _voice("x")["profile"],
        },
        headers=headers,
    ).get_json()
    from pandrator.web.models import Voice

    with app.extensions["pandrator"]["database"].session() as session:
        row = session.get(Voice, voice["id"])
        row.metadata_json = {
            **row.metadata_json,
            "providers": {"custom": {"status": "ready", "voice_id": "stable"}},
        }
    response = client.patch(
        f"/api/v1/voices/{voice['id']}",
        json={"profile": {"pitch": "high"}},
        headers={**headers, "If-Match": f'"{voice["revision"]}"'},
    )
    assert response.status_code == 200, response.get_json()
    assert (
        response.get_json()["metadata_json"]["providers"]["custom"]["voice_id"]
        == "stable"
    )
    assert response.get_json()["profile"]["pitch"] == "high"
    conflict = client.patch(
        f"/api/v1/voices/{voice['id']}",
        json={"profile": None},
        headers={**headers, "If-Match": f'"{voice["revision"]}"'},
    )
    assert conflict.status_code == 409


def test_reviewed_evidence_requires_audio_and_category_edits_clear_old_evidence(
    catalog_client,
):
    client, headers, app = catalog_client
    extension = app.extensions["pandrator"]
    audio = extension["paths"].artifacts / "review-evidence.wav"
    audio.write_bytes(b"test-only audio evidence")
    artifact = extension["artifacts"].register(
        audio, kind="audio", role="tts_voice_preview"
    )
    evidence = {
        "source": "audition_review",
        "status": "reviewed",
        "artifact_id": artifact.id,
    }
    voice = client.post(
        "/api/v1/voices",
        json={
            "name": "Reviewed voice",
            "voice_category": "male",
            "profile": {"evidence": {"voice_category": evidence}},
        },
        headers=headers,
    ).get_json()
    changed = client.patch(
        f"/api/v1/voices/{voice['id']}",
        json={"voice_category": "androgynous"},
        headers={**headers, "If-Match": str(voice["revision"])},
    )
    assert changed.status_code == 200
    assert "voice_category" not in changed.get_json()["profile"]["evidence"]
    invalid = client.patch(
        f"/api/v1/voices/{voice['id']}",
        json={
            "name": "Must not save",
            "profile": {"evidence": {"pitch": {**evidence, "artifact_id": "absent"}}},
        },
        headers={**headers, "If-Match": str(changed.get_json()["revision"])},
    )
    assert invalid.status_code == 422
    current = client.get(f"/api/v1/voice-catalog?query={voice['id']}").get_json()[
        "items"
    ][0]
    assert current["name"] == "Reviewed voice"
    assert current["revision"] == changed.get_json()["revision"]


def test_shared_query_and_collection_mutations_are_revisioned(catalog_client):
    client, headers, _ = catalog_client
    voice = client.post(
        "/api/v1/voices",
        json={"name": "Scottish elder", "profile": _voice("x")["profile"]},
        headers=headers,
    ).get_json()
    created = client.post(
        "/api/v1/voice-collections",
        json={"name": "Christmas Carol"},
        headers={**headers, "Idempotency-Key": "collection-create-1"},
    )
    assert created.status_code == 201, created.get_json()
    collection = created.get_json()
    body = {
        "expected_revision": collection["revision"],
        "add_members": [{"kind": "managed", "voice_id": voice["id"]}],
    }
    target = f"/api/v1/voice-collections/{collection['id']}"
    updated = client.patch(
        target, json=body, headers={**headers, "Idempotency-Key": "membership-add-1"}
    )
    assert updated.status_code == 200, updated.get_json()
    replay = client.patch(
        target, json=body, headers={**headers, "Idempotency-Key": "membership-add-1"}
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    with patch(
        "pandrator.web.tts_providers.TtsCatalogueService.snapshot",
        return_value=({"services": []}, 1),
    ):
        result = client.get(
            "/api/v1/voice-catalog",
            query_string={"collection_id": collection["id"], "accent": "Scottish"},
        )
    assert result.status_code == 200, result.get_json()
    assert [v["id"] for v in result.get_json()["items"]] == [voice["id"]]


def test_provider_metadata_survives_catalog_refresh(catalog_client):
    client, headers, _ = catalog_client
    service = {
        "id": "reader",
        "models": ["basic"],
        "voices": ["stable-id"],
        "available": True,
    }
    ref = {
        "kind": "provider",
        "service_id": "reader",
        "model": "basic",
        "voice": "stable-id",
    }
    with patch(
        "pandrator.web.tts_providers.TtsCatalogueService.snapshot",
        return_value=({"services": [service]}, 1),
    ):
        saved = client.patch(
            "/api/v1/voice-catalog/metadata",
            json={
                "reference": ref,
                "expected_revision": 0,
                "changes": {"name": "My narrator", "profile": {"pitch": "low"}},
            },
            headers={**headers, "Idempotency-Key": "provider-metadata-1"},
        )
        assert saved.status_code == 200, saved.get_json()
        result = client.get(
            "/api/v1/voice-catalog?kind=provider&query=narrator"
        ).get_json()
    assert result["items"][0]["name"] == "My narrator"
    assert result["items"][0]["profile"]["pitch"] == "low"
    assert result["items"][0]["reference"] == ref
