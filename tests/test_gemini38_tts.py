"""Offline Gemini 3.8 catalogue, performance and native request contracts."""

from __future__ import annotations

import base64
import io
import json
import socket
import wave
from copy import deepcopy
from typing import Any

import pytest
import requests

from pandrator.logic import tts_handler
from pandrator.logic.model_catalogue import catalogue_page
from pandrator.logic.speech_performance import compile_performance

MODEL = "gemini-3.8-flash-tts"
TEXT = "Hello 世界. Keep these exact words."
STYLE = "Warm and restrained."
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
VERTEX_URL = f"https://aiplatform.googleapis.com/v1/projects/project%2Ffixture/locations/global/publishers/google/models/{MODEL}:generateContent"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Native Google fixtures must not access the network")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00\x40\x00\xc0\xff\x00\x00")
    return output.getvalue()


def settings(provider: str, *, style: str = "") -> dict[str, Any]:
    return {
        "service": provider,
        "xtts_model": MODEL,
        "speaker": "Kore",
        "language": "en",
        "generation_prompt": style,
        "provider_configs": [
            {
                "id": provider,
                "name": provider,
                "provider": provider,
                "api_base": tts_handler.GEMINI_AUDIO_BASE_URL if provider == "gemini" else "",
                "api_key": "fixture-key",
                "api_key_env": "GEMINI38_FIXTURE_KEY",
                "vertex_project": "project/fixture",
                "vertex_location": "global",
                "default_model": MODEL,
                "default_voice": "Kore",
            }
        ],
    }


def expected_body(provider: str, style: str) -> dict[str, Any]:
    if provider == "gemini":
        part: dict[str, Any] = {"type": "text", "text": TEXT}
        if style:
            part["annotations"] = [{"type": "speech_metadata", "style": style}]
        return {
            "model": MODEL,
            "input": [{"type": "user_input", "content": [part]}],
            "response_format": {"type": "audio", "mime_type": "audio/wav"},
            "generation_config": {"speech_config": [{"voice": "Kore"}]},
            "store": False,
        }
    part = {"text": TEXT}
    if style:
        part["speechMetadata"] = {"style": style}
    return {
        "contents": [{"role": "user", "parts": [part]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"voice": "Kore"}},
        },
    }


@pytest.mark.parametrize("provider", ["gemini", "vertex_ai"])
@pytest.mark.parametrize("style", ["", STYLE])
@pytest.mark.parametrize("status", [200, 401, 503])
def test_native_38_request_has_verbatim_text_structured_style_and_exact_wav(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    style: str,
    status: int,
) -> None:
    monkeypatch.setenv("GEMINI38_FIXTURE_KEY", "fixture-key")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    original = settings(provider, style=style)
    snapshot = deepcopy(original)
    sdk_calls: list[bool] = []
    calls: list[str] = []
    expected_url = GEMINI_URL if provider == "gemini" else VERTEX_URL
    output = wav_bytes()
    response = requests.Response()
    response.status_code = status
    response.url = expected_url
    response.headers["Content-Type"] = "application/json"
    if status != 200:
        response._content = b'{"error":{"message":"fixture failure"}}'
    elif provider == "gemini":
        response._content = json.dumps(
            {
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {"type": "text", "text": "metadata"},
                            {
                                "type": "audio",
                                "mime_type": "audio/wav",
                                "data": base64.b64encode(output).decode(),
                            },
                        ],
                    }
                ],
            }
        ).encode()
    else:
        response._content = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "metadata"},
                                {
                                    "inlineData": {
                                        "mimeType": "audio/wav",
                                        "data": base64.b64encode(output).decode(),
                                    }
                                },
                            ]
                        }
                    }
                ]
            }
        ).encode()

    def sdk(*args: object, **kwargs: object) -> requests.Response:
        sdk_calls.append(True)
        raise AssertionError("3.8 must use its documented native contract")

    def token(service: dict[str, object]) -> tuple[str, str]:
        assert service["vertex_location"] == "global"
        return "fixture-token", "project/fixture"

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append(url)
        assert url == expected_url
        assert options == {
            "headers": (
                {"x-goog-api-key": "fixture-key", "Content-Type": "application/json"}
                if provider == "gemini"
                else {"Authorization": "Bearer fixture-token", "Content-Type": "application/json"}
            ),
            "json": expected_body(provider, style),
            "timeout": 300,
        }
        return response

    monkeypatch.setattr(tts_handler, "_request_litellm_audio", sdk)
    monkeypatch.setattr(tts_handler, "_vertex_access_token", token)
    monkeypatch.setattr(tts_handler.requests, "post", post)
    if provider == "gemini":
        returned = tts_handler._request_openai_compatible_audio(TEXT, original)
    else:
        returned = tts_handler._request_vertex_ai_audio(TEXT, original)
    assert calls == [expected_url]
    assert sdk_calls == []
    assert original == snapshot
    if status == 200:
        assert returned.content == output
        assert returned.headers["Content-Type"] == "audio/wav"
        assert returned.url == expected_url
        assert returned.content.count(b"RIFF") == 1
    else:
        assert returned is response


@pytest.mark.parametrize("provider", ["gemini", "vertex_ai"])
def test_38_performance_separates_unspoken_controls_and_context(provider: str) -> None:
    config = settings(provider, style=STYLE)
    config.update(
        {
            "tts_context_mode": "both",
            "_semantic_context": {"before": "Earlier context.", "after": "Later context."},
            "performance_allow_vocalizations": True,
            "_performance": {
                "decision": "steer",
                "delivery": {"emotion": "excited"},
                "spans": [{"anchor": {"quote": "exact words"}, "delivery": {"pace": "slower"}}],
                "events": [{"kind": "laugh", "position": "after"}],
            },
        }
    )
    compiled = compile_performance(TEXT, config)
    assert compiled.transcript == TEXT
    assert compiled.input == TEXT + "<laugh>"
    assert STYLE in compiled.instructions
    assert "excited tone" in compiled.instructions
    assert "exact words" in compiled.instructions
    assert "Earlier context." in compiled.instructions
    assert "Later context." in compiled.instructions
    assert "[" not in compiled.input
    assert "Transcript:" not in compiled.input
    assert compiled.capabilities["dialect"] == "gemini38"


@pytest.mark.parametrize("provider", ["gemini", "vertex_ai"])
def test_38_is_available_in_real_catalogue_with_structured_capabilities(provider: str) -> None:
    row = next(
        row for row in catalogue_page(provider=provider, limit=100)["items"] if row["id"] == MODEL
    )
    assert row["catalogue_id"] == f"{provider}:{MODEL}"
    assert row["pandrator_features"]["instructions"] == "field"
    assert row["upstream_features"]["instructions"] is True
    assert "vocal_events" in row["capabilities"]
    assert row["pandrator_features"]["voice_design"] == "none"


def test_vertex_38_rejects_non_global_location_before_speech_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = settings("vertex_ai")
    config["provider_configs"][0]["vertex_location"] = "europe-west4"
    calls: list[bool] = []
    monkeypatch.setattr(
        tts_handler, "_vertex_access_token", lambda service: ("fixture-token", "project")
    )

    def post(*args: object, **kwargs: object) -> requests.Response:
        calls.append(True)
        raise AssertionError("unsupported region must not receive speech")

    monkeypatch.setattr(tts_handler.requests, "post", post)
    with pytest.raises(ValueError, match="global"):
        tts_handler._request_vertex_ai_audio(TEXT, config)
    assert calls == []


@pytest.mark.parametrize("provider", ["gemini", "vertex_ai"])
@pytest.mark.parametrize("milliseconds, tag", [(250, "<short pause>"), (2000, "<long pause>")])
def test_38_pause_uses_documented_angle_tags_with_soft_duration(
    provider: str,
    milliseconds: int,
    tag: str,
) -> None:
    config = settings(provider)
    config["_performance"] = {
        "decision": "steer",
        "events": [{"kind": "pause", "position": "after", "duration_ms": milliseconds}],
    }
    compiled = compile_performance(TEXT, config)
    assert compiled.input == TEXT + tag
    assert any(
        item["control"] == "pause" and item["status"] == "approximated" for item in compiled.report
    )


@pytest.mark.parametrize("provider, expected_count", [("gemini", 88), ("vertex_ai", 86)])
def test_38_language_evidence_is_its_own_subset(provider: str, expected_count: int) -> None:
    from pandrator.logic.tts_language_preflight import validate_tts_language

    config = settings(provider)
    result = validate_tts_language(config)
    assert result["decision"] == "supported"
    support = result["language_support"]
    assert support is not None
    assert support["model_id"] == MODEL
    assert support["coverage"] == "subset"
    assert len(support["languages"]) == expected_count
    config["language"] = "ja"
    assert validate_tts_language(config)["decision"] == "supported"
    if provider == "gemini":
        assert support["native_route"] == "gemini_interactions"
    else:
        config["language"] = "vi"
        assert validate_tts_language(config)["decision"] == "unverified"


def test_38_vertex_does_not_inherit_old_8000_byte_limit() -> None:
    text = "語" * 3000
    compiled = compile_performance(text, settings("vertex_ai"))
    assert compiled.input == text
    assert len(text.encode()) == 9000


@pytest.mark.parametrize("provider", ["gemini", "vertex_ai"])
@pytest.mark.parametrize(
    "on_date, developer_price", [("2026-12-31", (0.5, 9.0)), ("2027-01-01", (1.0, 18.0))]
)
def test_38_pricing_respects_launch_expiry_and_cloud_billing_credits(
    provider: str,
    on_date: str,
    developer_price: tuple[float, float],
) -> None:
    from datetime import date

    before = deepcopy(tts_handler.DEFAULT_TTS_PRICING)
    pricing = tts_handler._default_tts_pricing(provider, as_of=date.fromisoformat(on_date))
    expected = developer_price if provider == "gemini" else (1.0, 18.0)
    assert pricing[MODEL] == {
        "input_cost_per_million_tokens": expected[0],
        "output_cost_per_million_audio_tokens": expected[1],
        "audio_tokens_per_second": 25.0,
    }
    assert tts_handler.DEFAULT_TTS_PRICING == before
    assert {key: value for key, value in pricing.items() if key != MODEL} == before


def test_38_native_gemini_keeps_borrowed_session_and_late_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint: dict[str, object] = {"api_key": "fixture-key", "api_key_env": "GEMINI38_FIXTURE_KEY"}
    monkeypatch.setenv("GEMINI38_FIXTURE_KEY", "fixture-key")
    calls: list[str] = []
    with requests.Session() as session:
        error = requests.Response()
        error.status_code = 503

        def post(url: str, **options: object) -> requests.Response:
            calls.append(url)
            assert options["timeout"] == 111
            return error

        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("borrowed session must be retained")

        monkeypatch.setattr(session, "post", post)
        monkeypatch.setattr(session, "close", forbidden)
        monkeypatch.setattr(tts_handler.requests, "post", forbidden)
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 111)
        result = tts_handler._request_gemini_native_audio(
            {"model": MODEL, "input": TEXT, "voice": "Kore"},
            endpoint,
            request_session=session,
        )
        assert result is error
        assert calls == [GEMINI_URL]
        monkeypatch.undo()


@pytest.mark.parametrize("provider", ["gemini", "vertex_ai"])
@pytest.mark.parametrize("prefix", ["models/", "gemini/"])
def test_prefixed_38_model_uses_structured_performance_in_preview(
    provider: str, prefix: str
) -> None:
    config = settings(provider, style=STYLE)
    config["xtts_model"] = prefix + MODEL
    compiled = compile_performance(TEXT, config)
    assert compiled.input == TEXT
    assert compiled.instructions == STYLE
    assert compiled.capabilities["dialect"] == "gemini38"
