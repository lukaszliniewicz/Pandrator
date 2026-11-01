"""Offline actual-I/O fences for generic speech preview preparation."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from pydub import AudioSegment

from pandrator.logic import (
    elevenlabs_v4_audio,
    kobold_qwen_http,
    native_speech_http,
    tts_handler,
    voxcpm_speech_http,
)
from pandrator.logic.elevenlabs_speech_contracts import ELEVENLABS_V4_TURBO_MODEL
from pandrator.logic.model_acquisition import ModelAcquisitionDenied, model_acquisition_scope
from pandrator.logic.tts_io_policy import TtsIoDenied, current_tts_io_policy, tts_io_scope
from pandrator.logic.tts_retry import TtsRetryGate


def forbidden(*_args: Any, **_kwargs: Any) -> Any:
    pytest.fail("Unexpected real transport, SDK, auth discovery or model acquisition")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(tts_handler, "_get_litellm_speech_client", forbidden)
    monkeypatch.setattr("google.auth.default", forbidden)
    monkeypatch.setattr("google.auth.transport.requests.Request", forbidden)
    with model_acquisition_scope(None):
        yield
    assert current_tts_io_policy() is None


def response(status: int = 200, payload: Any = None) -> requests.Response:
    value = requests.Response()
    value.status_code = status
    value._content = json.dumps(payload).encode()
    value._content_consumed = True
    value.url = "https://synthetic.invalid/speech"
    return value


def proof_after(allowed: int, events: list[str]) -> Callable[[], None]:
    def prove() -> None:
        events.append("proof")
        if events.count("proof") > allowed:
            raise PermissionError("synthetic proof loss")
    return prove


@pytest.mark.parametrize("kind", ["native", "prepared", "candidates"])
@pytest.mark.parametrize("guarded", [False, True])
def test_native_options_are_built_before_proof_and_preserve_identity(
    monkeypatch: pytest.MonkeyPatch, kind: str, guarded: bool,
) -> None:
    events: list[str] = []
    body: dict[str, Any] = {"input": "private synthetic text"}
    options: Any = {"headers": {"Authorization": "Bearer namespace"}, "json": body, "timeout": 7}
    result = response()
    session = requests.Session()

    def build() -> Any:
        events.append("options")
        return options

    def post(url: str, **kwargs: Any) -> requests.Response:
        events.append("send")
        assert url == "https://synthetic.invalid/speech"
        assert kwargs == {**options, **({"allow_redirects": False} if guarded else {})}
        assert kwargs["json"] is body and kwargs["headers"] is options["headers"]
        return result

    monkeypatch.setattr(requests, "post", forbidden if kind == "prepared" else post)
    monkeypatch.setattr(session, "post", post)
    with tts_io_scope(lambda: events.append("proof")) if guarded else nullcontext():
        if kind == "native":
            actual = native_speech_http.post_native_speech(
                "https://synthetic.invalid/speech", request_label="fixture", request_options=build,
            )
        elif kind == "prepared":
            def url() -> str:
                events.append("url")
                return "https://synthetic.invalid/speech"
            actual = native_speech_http.post_prepared_speech(
                url, request_options=build, request_session=session,
            )
        else:
            actual = native_speech_http.post_speech_candidates(
                ["https://synthetic.invalid/speech"], request_options=build,
                should_try_next=lambda _: False, no_endpoint_message="none",
            )
    assert actual is result and "allow_redirects" not in options
    assert events == (["url"] if kind == "prepared" else []) + ["options"] + (
        ["proof"] if guarded else []
    ) + ["send"]
    assert session.adapters  # Borrowed transport remains usable and owned by caller.
    session.close()


@pytest.mark.parametrize("kind,allowed", [("native", 0), ("prepared", 0), ("candidates", 0), ("candidates", 1)])
def test_native_denial_prevents_first_or_next_candidate(
    monkeypatch: pytest.MonkeyPatch, kind: str, allowed: int,
) -> None:
    events: list[str] = []

    def post(*_args: Any, **kwargs: Any) -> requests.Response:
        assert kwargs["allow_redirects"] is False
        events.append("send")
        return response(404)

    monkeypatch.setattr(requests, "post", post)
    with tts_io_scope(proof_after(allowed, events)):
        if kind == "candidates":
            with pytest.raises(TtsIoDenied):
                native_speech_http.post_speech_candidates(
                    ["https://synthetic.invalid/first", "https://synthetic.invalid/next"],
                    request_options=lambda: {"json": {}, "timeout": 1},
                    should_try_next=lambda status: status == 404, no_endpoint_message="none",
                )
        elif allowed == 0:
            with pytest.raises(TtsIoDenied):
                if kind == "native":
                    native_speech_http.post_native_speech(
                        "https://synthetic.invalid", request_label="fixture",
                        request_options=lambda: {"headers": {}, "timeout": 1},
                    )
                else:
                    native_speech_http.post_prepared_speech(
                        lambda: "https://synthetic.invalid",
                        request_options=lambda: {"json": {}, "timeout": 1},
                    )
    assert events.count("send") == allowed


@pytest.mark.parametrize("allowed", [0, 1, 2, 3])
def test_voxcpm_hifi_and_next_candidate_each_require_fresh_proof(
    monkeypatch: pytest.MonkeyPatch, allowed: int,
) -> None:
    events: list[str] = []
    payload: dict[str, Any] = {"input": "synthetic private text", "mode": "clone", "voice": "v"}
    attempts: list[tuple[str, dict[str, Any]]] = []
    pairing = "prompt_wav_path and prompt_text must both be provided or both be none"
    results = iter([response(422, {"error": {"message": pairing}}), response(404), response()])

    def post(url: str, **options: Any) -> requests.Response:
        assert options["allow_redirects"] is False
        events.append("send")
        attempts.append((url, options["json"]))
        return next(results)

    monkeypatch.setattr(requests, "post", post)
    with tts_io_scope(proof_after(allowed, events)):
        with pytest.raises(TtsIoDenied) if allowed < 3 else nullcontext():
            voxcpm_speech_http.post_voxcpm_speech(
                ["first", "next"], payload,
                request_options=lambda data: {"json": data, "timeout": 1},
                prompt_pairing_error=voxcpm_speech_http._is_voxcpm_prompt_pairing_error,
                should_try_next=lambda status: status == 404, warn=lambda *_: None,
                no_endpoint_message="none",
            )
    assert len(attempts) == allowed
    if attempts:
        assert attempts[0] == ("first", payload) and attempts[0][1] is payload
    if len(attempts) > 1:
        assert attempts[1] == ("first", {**payload, "mode": "hifi"})
    if len(attempts) > 2:
        assert attempts[2] == ("next", payload) and attempts[2][1] is payload
    assert payload["mode"] == "clone"


def provider_settings(provider: str = "openai") -> dict[str, Any]:
    return {
        "service": "OpenAI" if provider == "openai" else "Google Gemini",
        "xtts_model": "gpt-4o-mini-tts" if provider == "openai" else "gemini-3.1-flash-tts-preview",
        "speaker": "alloy" if provider == "openai" else "Kore", "language": "en",
        "provider_configs": [{"id": provider, "api_key_env": "PREVIEW_POISON",
                              "api_key": "namespace-key"}],
    }


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_registry_provider_request_is_direct_and_uses_namespace_key(
    monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    monkeypatch.setenv("PREVIEW_POISON", "host-key-must-not-escape")
    sent: list[dict[str, Any]] = []

    def post(_url: str, **options: Any) -> requests.Response:
        sent.append(options)
        return response(400)  # Avoid needing a real encoded provider response.

    monkeypatch.setattr(requests, "post", post)
    with tts_io_scope(lambda: None, allow_environment_credentials=False, allow_host_adc=False):
        result = tts_handler._request_openai_compatible_audio("Hello.", provider_settings(provider))
    assert result.status_code == 400 and len(sent) == 1
    assert sent[0]["allow_redirects"] is False
    assert "namespace-key" in str(sent[0]["headers"])
    assert "host-key" not in str(sent)


@pytest.mark.parametrize("allowed", [0, 1])
def test_actual_outer_retry_never_swallows_denial_or_calls_sdk(
    monkeypatch: pytest.MonkeyPatch, allowed: int,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(tts_handler, "_TTS_RETRY_GATE", TtsRetryGate())
    classified: list[BaseException] = []

    def classify(error: BaseException) -> bool:
        assert not isinstance(error, TtsIoDenied)
        classified.append(error)
        return True

    monkeypatch.setattr(tts_handler, "retryable_error", classify)
    monkeypatch.setattr(tts_handler, "wait_for_retry", lambda *_a, **_kw: True)
    monkeypatch.setattr(tts_handler, "_decode_audio_response", forbidden)

    def post(*_args: Any, **options: Any) -> requests.Response:
        assert options["allow_redirects"] is False
        events.append("send")
        return response(503)

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(TtsIoDenied), tts_io_scope(
        proof_after(allowed, events), allow_environment_credentials=False, allow_host_adc=False,
    ):
        tts_handler.text_to_audio("Hello.", provider_settings(), max_attempts=4)
    assert events.count("send") == allowed
    assert events.count("proof") == allowed + 1
    assert len(classified) == allowed


def test_successful_guarded_outer_request_bypasses_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_handler, "_TTS_RETRY_GATE", TtsRetryGate())
    audio = AudioSegment.silent(duration=1)
    monkeypatch.setattr(tts_handler, "_decode_audio_response", lambda _: audio)
    calls: list[str] = []
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: calls.append("send") or response())
    with tts_io_scope(lambda: calls.append("proof")):
        assert tts_handler.text_to_audio("Hello.", provider_settings(), max_attempts=4) is audio
    assert calls == ["proof", "send"]


@pytest.mark.parametrize("resolver", ["openai", "headers", "azure", "elevenlabs", "elevenlabs_endpoint", "service"])
def test_strict_shared_key_helpers_ignore_host_environment_and_restore(
    monkeypatch: pytest.MonkeyPatch, resolver: str,
) -> None:
    monkeypatch.setenv("PREVIEW_POISON", "host-poison")
    endpoint: dict[str, object] = {"api_key_env": "PREVIEW_POISON", "api_key": "namespace-key"}
    settings = {"provider_configs": [{"id": "elevenlabs", **endpoint}]}
    calls: dict[str, Callable[[], Any]] = {
        "openai": lambda: tts_handler._resolve_openai_audio_api_key(endpoint),
        "headers": lambda: tts_handler._configured_endpoint_auth_headers(endpoint),
        "azure": lambda: tts_handler._resolve_azure_speech_api_key(endpoint),
        "elevenlabs": lambda: tts_handler._resolve_elevenlabs_api_key(settings),
        "elevenlabs_endpoint": lambda: tts_handler._resolve_elevenlabs_endpoint_api_key(endpoint),
        "service": lambda: tts_handler._resolve_service_api_key(settings, "elevenlabs", "PREVIEW_POISON"),
    }
    assert "host-poison" in str(calls[resolver]())
    with tts_io_scope(lambda: None, allow_environment_credentials=False):
        assert "namespace-key" in str(calls[resolver]())
    assert "host-poison" in str(calls[resolver]())


@pytest.mark.parametrize("explicit", [False, True])
def test_strict_retry_identity_never_reads_environment_or_adc_file(
    monkeypatch: pytest.MonkeyPatch, explicit: bool,
) -> None:
    reads: list[str] = []
    original = tts_handler.os.getenv
    poisoned = {"PREVIEW_POISON", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
                "GCLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT"}

    def getenv(key: str, default: Any = None) -> Any:
        reads.append(key)
        if key in poisoned:
            pytest.fail("Strict retry identity inspected a host environment hint")
        return original(key, default)

    monkeypatch.setattr(tts_handler.os, "getenv", getenv)
    service: dict[str, Any] = {"api_key_env": "PREVIEW_POISON", "secret_ref": "namespace-secret"}
    if explicit:
        service.update(api_key=json.dumps({"client_email": "synthetic@namespace", "project_id": "namespace-project"}))
    with tts_io_scope(lambda: None, allow_environment_credentials=False, allow_host_adc=False):
        identity = tts_handler._credential_material_for_retry_scope(service, vertex=True)
    assert identity == (("synthetic@namespace", "namespace-project", "global") if explicit else (
        "namespace-secret", "", "global",
    ))
    assert not poisoned.intersection(reads)


def test_vertex_empty_strict_credentials_denies_before_adc(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = {"service": "Google Vertex AI", "provider_configs": [{"id": "vertex_ai", "api_key": ""}]}
    with tts_io_scope(lambda: None, allow_environment_credentials=False, allow_host_adc=False):
        with pytest.raises(ValueError, match="credentials are not configured"):
            tts_handler._request_vertex_ai_audio("Hello.", settings)


@pytest.mark.parametrize("allowed", [0, 1, 2, 3])
def test_vertex_explicit_refresh_and_auth_request_then_speech_are_guarded(
    monkeypatch: pytest.MonkeyPatch, allowed: int,
) -> None:
    events: list[str] = []

    class Credentials:
        valid = False
        token: str | None = None

        def refresh(self, request: Callable[..., Any]) -> None:
            events.append("refresh")
            request("https://synthetic.invalid/token")
            self.valid, self.token = True, "synthetic-token"

    monkeypatch.setattr("google.auth.transport.requests.Request", lambda: lambda *_a, **_kw: events.append("auth-send"))
    monkeypatch.setattr("google.oauth2.service_account.Credentials.from_service_account_info", lambda *_a, **_kw: Credentials())

    def post(*_a: Any, **options: Any) -> requests.Response:
        assert options["headers"]["Authorization"] == "Bearer synthetic-token"
        assert options["allow_redirects"] is False
        events.append("speech-send")
        return response(400)

    monkeypatch.setattr(requests, "post", post)
    settings = {"service": "Google Vertex AI", "model": "gemini-3.1-flash-tts-preview", "voice": "Kore",
                "provider_configs": [{"id": "vertex_ai", "api_key": json.dumps({"project_id": "namespace-project"})}]}
    with tts_io_scope(proof_after(allowed, events), allow_environment_credentials=False, allow_host_adc=False):
        with pytest.raises(TtsIoDenied) if allowed < 3 else nullcontext():
            tts_handler._request_vertex_ai_audio("Hello.", settings)
    assert events.count("auth-send") == int(allowed >= 2)
    assert events.count("speech-send") == int(allowed >= 3)
    assert events.count("refresh") == int(allowed >= 1)


@pytest.mark.parametrize("method", ["ready", "recovery", "capabilities", "connection", "models", "voices"])
def test_qwen_get_fallback_cannot_swallow_proof_loss(
    monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    events: list[str] = []

    def get(*_args: Any, **options: Any) -> requests.Response:
        assert options["allow_redirects"] is False
        events.append("get")
        return response(404)

    monkeypatch.setattr(requests, "get", get)
    calls: dict[str, Callable[[], Any]] = {
        "ready": lambda: tts_handler._kobold_qwen_is_ready("https://synthetic.invalid", api_key="namespace"),
        "recovery": lambda: tts_handler._wait_for_kobold_qwen_recovery("https://synthetic.invalid", api_key="namespace", timeout_seconds=1),
        "capabilities": lambda: tts_handler.get_kobold_qwen_batch_capabilities("https://synthetic.invalid", api_key="namespace"),
        "connection": lambda: tts_handler.check_kobold_qwen_connection("https://synthetic.invalid"),
        "models": lambda: tts_handler.get_kobold_qwen_models("https://synthetic.invalid"),
        "voices": lambda: tts_handler.get_kobold_qwen_voice_catalog("https://synthetic.invalid", api_key="namespace"),
    }
    with pytest.raises(TtsIoDenied), tts_io_scope(proof_after(1, events)):
        calls[method]()
    assert events == ["proof", "get", "proof"]


def test_qwen_single_keeps_model_authorizer_and_fences_each_post(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    def post(*_a: Any, **options: Any) -> requests.Response:
        assert options["allow_redirects"] is False
        events.append("post")
        return response(404)

    monkeypatch.setattr(requests, "post", post)

    def call() -> requests.Response:
        return kobold_qwen_http._request_kobold_qwen_audio(
            "Hello.", {}, "https://synthetic.invalid", resolve_api_key=lambda _: "namespace",
            build_payload=lambda *_: {"input": "Hello."},
        )
    with pytest.raises(ModelAcquisitionDenied), tts_io_scope(lambda: events.append("proof")):
        call()
    assert events == []
    with model_acquisition_scope(lambda: events.append("acquire")), tts_io_scope(proof_after(1, events)):
        with pytest.raises(TtsIoDenied):
            call()
    assert events == ["acquire", "proof", "post", "acquire", "proof"]


@pytest.mark.parametrize("allowed", [0, 1, 2, 3, 4])
def test_websocket_connect_and_each_frame_are_fenced_and_cleanup_survives_denial(
    monkeypatch: pytest.MonkeyPatch, allowed: int,
) -> None:
    events: list[str] = []
    frames: list[dict[str, Any]] = []

    def send(frame: str) -> None:
        events.append("send")
        frames.append(json.loads(frame))

    connection = SimpleNamespace(send=send, recv=lambda **_: '{"audio":"YQ==","is_final":true}',
                                 close=lambda: events.append("close"))

    def connect(*_a: Any, **_kw: Any) -> Any:
        events.append("connect")
        return connection

    monkeypatch.setattr("websockets.sync.client.connect", connect)
    with tts_io_scope(proof_after(allowed, events)):
        with pytest.raises(TtsIoDenied) if allowed < 4 else nullcontext():
            result = elevenlabs_v4_audio.stream_turbo_dialogue_audio(
                base_url="https://synthetic.invalid", api_key="namespace", model_id=ELEVENLABS_V4_TURBO_MODEL,
                voice_id="voice", text="Hello.", output_format="mp3_44100_128",
            )
            assert result.content == b"a"
    assert events.count("connect") == int(allowed >= 1)
    assert events.count("send") == max(0, allowed - 1)
    assert events.count("close") == int(allowed >= 1)
    assert frames == [{"voices": ["voice"]}, {"inputs": [{"text": "Hello.", "voice_id": "voice"}], "flush": True},
                      {"close_socket": True}][:max(0, allowed - 1)]


def test_websocket_transport_error_remains_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def connect(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("synthetic secret namespace and text")
    monkeypatch.setattr("websockets.sync.client.connect", connect)
    with tts_io_scope(lambda: None), pytest.raises(ValueError) as caught:
        elevenlabs_v4_audio.stream_turbo_dialogue_audio(
            base_url="https://synthetic.invalid", api_key="namespace", model_id=ELEVENLABS_V4_TURBO_MODEL,
            voice_id="voice", text="Hello.", output_format="mp3_44100_128",
        )
    assert str(caught.value) == "ElevenLabs realtime dialogue transport failed; no audio was returned."
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("allowed", [0, 1])
def test_qwen_batch_fences_candidate_posts_without_changing_batch_format(
    monkeypatch: pytest.MonkeyPatch, allowed: int,
) -> None:
    events: list[str] = []
    payloads: list[dict[str, Any]] = []

    def post(*_args: Any, **options: Any) -> requests.Response:
        assert options["allow_redirects"] is False and options["stream"] is True
        events.append("post")
        payloads.append(options["json"])
        return response(404)

    monkeypatch.setattr(requests, "post", post)
    settings = {"model": "synthetic-model"}
    with model_acquisition_scope(lambda: events.append("acquire")), tts_io_scope(proof_after(allowed, events)):
        with pytest.raises(TtsIoDenied):
            list(kobold_qwen_http._iter_kobold_qwen_batch_audio_http(
                [{"id": "one", "text": "Hello.", "settings": settings}],
                base_url="https://synthetic.invalid", api_key="namespace",
                resolve_api_key=forbidden, build_payload=lambda *_: {"input": "Hello."},
                decode_audio=forbidden,
            ))
    assert events.count("post") == allowed and events.count("acquire") == allowed + 1
    if payloads:
        assert payloads == [{"items": [{"id": "one", "input": "Hello."}], "stream": True, "fail_fast": False}]


def test_websocket_cleanup_failure_cannot_mask_denied_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    def close() -> None:
        events.append("close")
        raise RuntimeError("synthetic cleanup failure")

    connection = SimpleNamespace(send=forbidden, recv=forbidden, close=close)
    monkeypatch.setattr("websockets.sync.client.connect", lambda *_a, **_kw: connection)
    with tts_io_scope(proof_after(1, events)), pytest.raises(TtsIoDenied) as caught:
        elevenlabs_v4_audio.stream_turbo_dialogue_audio(
            base_url="https://synthetic.invalid", api_key="namespace", model_id=ELEVENLABS_V4_TURBO_MODEL,
            voice_id="voice", text="Hello.", output_format="mp3_44100_128",
        )
    assert events == ["proof", "proof", "close"]
    assert isinstance(caught.value.__cause__, PermissionError)
