from __future__ import annotations

import json
from pathlib import Path

from pandrator.logic import tts_handler, tts_service_catalogue
from pandrator.logic.speech_capabilities import capabilities_for_endpoint_model
from pandrator.logic.speech_performance import compile_performance, resolve_capabilities

MODEL = "MAI-Voice-2.1"
FLASH = "MAI-Voice-2.1-Flash"
ETHAN = f"en-US-Ethan:{MODEL}"
GRANT = f"en-US-Grant:{MODEL}"
CATALOGUE_PATH = (
    Path(__file__).resolve().parents[1]
    / "pandrator"
    / "logic"
    / "azure_mai_voice_catalogue.json"
)


def azure_endpoint(**overrides):
    return {
        "id": "azure-speech-test",
        "adapter": "azure_speech",
        "provider": "azure",
        "default_model": MODEL,
        "default_voice": ETHAN,
        **overrides,
    }


def source_styles(voice_id: str) -> list[str]:
    catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    return next(row["supported_styles"] for row in catalogue["rows"] if row["voice_id"] == voice_id)


def emotion_rows(compiled):
    return [row for row in compiled.report if row["control"] == "emotion"]


def test_capability_profile_uses_exact_models_and_selected_voice_styles():
    endpoint = azure_endpoint()
    for model in (MODEL, FLASH):
        model_profile = capabilities_for_endpoint_model(model, endpoint)
        assert model_profile["status"] == "documented"
        assert model_profile["dialect"] == "azure_style"
        assert model_profile["emotion"]["mode"] == "enum"
        assert model_profile["emotion"]["voice_dependent"] is True
        assert "educational" in model_profile["emotion"]["tags"]

    ethan = capabilities_for_endpoint_model(MODEL, endpoint, voice=ETHAN)
    grant = capabilities_for_endpoint_model(MODEL, endpoint, voice=GRANT)
    assert ethan["emotion"]["tags"] == source_styles("en-US-Ethan")
    assert grant["emotion"]["tags"] == source_styles("en-US-Grant")
    assert "educational" not in ethan["emotion"]["tags"]
    assert "educational" in grant["emotion"]["tags"]
    unknown_voice = capabilities_for_endpoint_model(
        MODEL, endpoint, voice=f"unlisted:{MODEL}"
    )
    assert unknown_voice["emotion"]["tags"] == []

    unknown = capabilities_for_endpoint_model("MAI-Voice-2", endpoint, voice=ETHAN)
    assert unknown["status"] == "unknown"
    assert unknown["dialect"] == "none"


def test_resolver_uses_model_default_voice_and_caches_per_selected_voice(monkeypatch):
    endpoint = azure_endpoint(
        default_voice="en-US-Ethan:MAI-Voice-2.1-Flash",
        default_voices={MODEL: GRANT, FLASH: f"en-US-Ethan:{FLASH}"},
    )
    monkeypatch.setattr(
        tts_service_catalogue,
        "get_service_config",
        lambda *_args, **_kwargs: endpoint,
    )
    settings = {"service": endpoint["id"], "model": MODEL}
    profile = resolve_capabilities(settings, _service_config_cache={})
    assert profile["emotion"]["tags"] == source_styles("en-US-Grant")

    cache = {}
    grant_profile = resolve_capabilities(
        {**settings, "voice": GRANT}, _service_config_cache=cache
    )
    ethan_profile = resolve_capabilities(
        {**settings, "voice": ETHAN}, _service_config_cache=cache
    )
    assert "educational" in grant_profile["emotion"]["tags"]
    assert "educational" not in ethan_profile["emotion"]["tags"]
    profile_keys = [key for key in cache if key[0] == "resolved_capabilities"]
    assert len(profile_keys) == 2
    assert profile_keys[0] != profile_keys[1]


def test_whole_request_and_uniform_markup_emotion_lower_to_azure_style():
    endpoint = azure_endpoint()
    compiled = compile_performance(
        "Hello.",
        {
            "service": endpoint["id"],
            "model": MODEL,
            "voice": ETHAN,
            "_performance": {
                "decision": "steer",
                "delivery": {"emotion": " HaPPy "},
            },
        },
        endpoint,
    )
    assert compiled.input == "Hello."
    assert compiled.instructions == ""
    assert compiled.request_options == {"azure_speech_style": "happy"}
    assert emotion_rows(compiled) == [
        {
            "status": "applied",
            "control": "emotion",
            "message": f"Applied literal Azure style 'happy' to the entire request for {ETHAN}.",
        }
    ]

    uniform_markup = compile_performance(
        "Hello.",
        {
            "service": endpoint["id"],
            "model": MODEL,
            "voice": ETHAN,
            "_performance_markup_scope": {
                "root_delivery": {"emotion": "neutral"},
                "spans": [
                    {"start": 0, "end": 6, "delivery": {"emotion": "neutral"}}
                ],
            },
        },
        endpoint,
    )
    assert uniform_markup.input == "Hello."
    assert uniform_markup.request_options == {"azure_speech_style": "neutral"}
    assert emotion_rows(uniform_markup)[0]["status"] == "applied"


def test_phrase_and_unknown_emotions_are_unsupported_and_never_become_prose():
    endpoint = azure_endpoint()
    markup = compile_performance(
        "Hello. Bye.",
        {
            "service": endpoint["id"],
            "model": MODEL,
            "voice": ETHAN,
            "_performance_markup_scope": {
                "root_delivery": {"emotion": "happy"},
                "spans": [
                    {"start": 0, "end": 7, "delivery": {"emotion": "happy"}},
                    {"start": 7, "end": 11, "delivery": {"emotion": "sad"}},
                ],
            },
        },
        endpoint,
    )
    assert markup.input == "Hello. Bye."
    assert markup.instructions == ""
    assert markup.request_options == {"azure_speech_style": "happy"}
    assert any(
        row["status"] == "unsupported"
        and row["control"] == "emotion"
        and "Phrase-specific Azure style 'sad'" in row["message"]
        for row in markup.report
    )

    phrase_only = compile_performance(
        "Hello.",
        {
            "service": endpoint["id"],
            "model": MODEL,
            "voice": ETHAN,
            "_performance": {
                "decision": "steer",
                "spans": [
                    {
                        "anchor": {"quote": "Hello."},
                        "delivery": {"emotion": "educational"},
                    }
                ],
            },
        },
        endpoint,
    )
    assert phrase_only.input == "Hello."
    assert phrase_only.instructions == ""
    assert phrase_only.request_options == {}
    assert any(
        row["status"] == "unsupported" and row["control"] == "emotion"
        for row in phrase_only.report
    )

    unknown = compile_performance(
        "Hello.",
        {
            "service": endpoint["id"],
            "model": MODEL,
            "voice": ETHAN,
            "_performance": {
                "decision": "steer",
                "delivery": {"emotion": "enthusiastic"},
            },
        },
        endpoint,
    )
    assert unknown.input == "Hello."
    assert unknown.instructions == ""
    assert unknown.request_options == {}
    assert any(
        row["status"] == "unsupported"
        and row["control"] == "emotion"
        and "enthusiastic" in row["message"]
        and "documented styles" in row["message"]
        for row in unknown.report
    )


def test_disabled_performance_preserves_native_project_style_only():
    endpoint = azure_endpoint()
    compiled = compile_performance(
        "Hello.",
        {
            "service": endpoint["id"],
            "model": MODEL,
            "voice": ETHAN,
            "performance_enabled": False,
            "_performance": {
                "decision": "steer",
                "delivery": {"emotion": "happy"},
            },
        },
        endpoint,
    )
    assert compiled.request_options == {}
    assert emotion_rows(compiled) == []


def test_azure_adapter_consumes_only_style_options_into_escaped_ssml(monkeypatch):
    monkeypatch.setenv("TEST_AZURE_SPEECH_KEY", "test-key")
    endpoint = azure_endpoint(
        base_url="https://example.cognitiveservices.azure.com",
        api_key_env="TEST_AZURE_SPEECH_KEY",
    )
    calls = []

    def fake_post_native_speech(url, *, request_label, request_options):
        calls.append(
            {"url": url, "request_label": request_label, **request_options()}
        )
        return calls[-1]

    monkeypatch.setattr(
        tts_handler._native_speech_http,
        "post_native_speech",
        fake_post_native_speech,
    )
    response = tts_handler._request_azure_speech_audio(
        "Tom & <Jo>",
        {
            "service": endpoint["id"],
            "model": MODEL,
            "voice": ETHAN,
            "azure_speech_style": "whispering",
            "azure_speech_style_degree": 1.25,
            "_performance": {
                "decision": "steer",
                "delivery": {"emotion": "happy"},
            },
        },
        endpoint,
    )
    assert response is calls[0]
    assert calls[0]["request_label"] == "Azure Speech"
    assert calls[0]["data"].count('<mstts:express-as style="happy" styledegree="1.25">') == 1
    assert "Tom &amp; &lt;Jo&gt;" in calls[0]["data"]
    assert "whispering" not in calls[0]["data"]

    tts_handler._request_azure_speech_audio(
        "Manual style.",
        {
            "service": endpoint["id"],
            "model": MODEL,
            "voice": ETHAN,
            "performance_enabled": False,
            "_performance": {
                "decision": "steer",
                "delivery": {"emotion": "happy"},
            },
            "azure_speech_style": "whispering",
            "azure_speech_style_degree": 0.75,
        },
        endpoint,
    )
    assert '<mstts:express-as style="whispering" styledegree="0.75">' in calls[1]["data"]


def test_azure_lowering_does_not_change_native_emotion_request_options():
    index = compile_performance(
        "Hello.",
        {
            "service": "audio_cpp",
            "model": "index_tts2_q8_0",
            "_performance": {
                "decision": "steer",
                "delivery": {"emotion": "quiet delight"},
            },
        },
        {"id": "audio_cpp", "adapter": "audio_cpp"},
    )
    assert index.request_options == {
        "emotion_text": "quiet delight",
        "use_emotion_text": True,
    }

    neutts = compile_performance(
        "Hello.",
        {
            "service": "audio_cpp",
            "model": "neutts_2e_orig",
            "_performance": {
                "decision": "steer",
                "delivery": {"emotion": "happy"},
            },
        },
        {"id": "audio_cpp", "adapter": "audio_cpp"},
    )
    assert neutts.request_options == {"emotion": "happy"}
