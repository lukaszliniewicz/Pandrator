from __future__ import annotations

import base64
import json
from contextvars import ContextVar
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from pydub import AudioSegment

from pandrator.logic import kobold_qwen_http, xtts_trainer_handler
from pandrator.logic.kobold_qwen_contracts import KoboldQwenBatchAudioEvent
from pandrator.logic.kobold_qwen_stream import iter_read_ahead_batch
from pandrator.logic.model_acquisition import (
    ModelAcquisitionDenied,
    model_acquisition_scope,
)
from pandrator.web.accounts.execution_actor import UserActor, current_execution_actor


class _Response:
    def __init__(self, status_code: int = 200, lines: tuple[str, ...] = ()) -> None:
        self.status_code = status_code
        self._lines = lines
        self.closed = False
        # Windows read-ahead cleanup inspects the transport for an owned
        # socket. The synthetic response has none, but the attribute must
        # exist on every platform the suite runs on.
        self.raw = SimpleNamespace()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True

    def iter_lines(self, *, decode_unicode: bool = False) -> Iterator[str]:
        del decode_unicode
        return iter(self._lines)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"synthetic HTTP {self.status_code}")


def _single_request(monkeypatch: pytest.MonkeyPatch, response: _Response) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> _Response:
        calls.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(kobold_qwen_http, "_openai_audio_speech_urls", lambda _url: ["speech-a"])
    monkeypatch.setattr(kobold_qwen_http.requests, "post", post)
    return calls


def _batch_item() -> list[dict[str, object]]:
    return [{"id": "synthetic-1", "text": "Hello", "settings": {"voice": "synthetic"}}]


def _decode_audio(audio: bytes, *, format_hint: str) -> AudioSegment:
    assert audio == b"synthetic-audio"
    assert format_hint == "wav"
    return AudioSegment.silent(duration=1)


def _batch_stream(
    stop_event,
    response_callback,
) -> Iterator[KoboldQwenBatchAudioEvent]:
    yield from kobold_qwen_http._iter_kobold_qwen_batch_audio_http(
        _batch_item(),
        base_url="http://kobold.invalid",
        resolve_api_key=lambda _settings: "synthetic-key",
        build_payload=lambda text, _settings: {"model": "qwen", "input": text},
        decode_audio=_decode_audio,
        stop_event=stop_event,
        response_callback=response_callback,
    )


def test_default_deny_prevents_single_and_batch_speech_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    single_calls = _single_request(monkeypatch, _Response())
    batch_calls: list[dict[str, Any]] = []

    def post_batch(url: str, **kwargs: Any) -> _Response:
        batch_calls.append({"url": url, **kwargs})
        return _Response()

    monkeypatch.setattr(
        kobold_qwen_http, "_openai_audio_speech_batch_urls", lambda _url: ["batch-a"]
    )
    monkeypatch.setattr(kobold_qwen_http.requests, "post", post_batch)

    def unrelated_outer_authorizer() -> None:
        return None

    with model_acquisition_scope(unrelated_outer_authorizer):
        with model_acquisition_scope(None):
            with pytest.raises(ModelAcquisitionDenied):
                kobold_qwen_http._request_kobold_qwen_audio(
                    "Hello",
                    {"voice": "synthetic"},
                    "http://kobold.invalid",
                    resolve_api_key=lambda _settings: "synthetic-key",
                    build_payload=lambda text, _settings: {"model": "qwen", "input": text},
                )
            with pytest.raises(ModelAcquisitionDenied):
                list(_batch_stream(None, lambda _response: None))

    assert single_calls == []
    assert batch_calls == []


def test_single_speech_preserves_payload_and_rechecks_before_fallback_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    responses = iter((_Response(404), _Response(200)))

    def post(url: str, **kwargs: Any) -> _Response:
        calls.append({"url": url, **kwargs})
        return next(responses)

    monkeypatch.setattr(
        kobold_qwen_http,
        "_openai_audio_speech_urls",
        lambda _url: ["speech-a", "speech-b"],
    )
    monkeypatch.setattr(
        kobold_qwen_http, "_should_try_next_openai_candidate", lambda status: status == 404
    )
    monkeypatch.setattr(kobold_qwen_http.requests, "post", post)
    authorizer_calls = 0

    def authorize() -> None:
        nonlocal authorizer_calls
        authorizer_calls += 1

    with model_acquisition_scope(authorize):
        response = kobold_qwen_http._request_kobold_qwen_audio(
            "Hello",
            {"voice": "synthetic"},
            "http://kobold.invalid",
            resolve_api_key=lambda _settings: "synthetic-key",
            build_payload=lambda text, settings: {
                "model": "qwen",
                "input": text,
                "voice": str(settings["voice"]),
            },
        )

    assert response.status_code == 200
    assert [call["url"] for call in calls] == ["speech-a", "speech-b"]
    assert calls[0]["json"] == {"model": "qwen", "input": "Hello", "voice": "synthetic"}
    assert calls[1]["json"] == calls[0]["json"]
    assert authorizer_calls == 2


def test_denial_between_single_endpoint_attempts_prevents_second_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _single_request(monkeypatch, _Response(404))
    monkeypatch.setattr(
        kobold_qwen_http,
        "_openai_audio_speech_urls",
        lambda _url: ["speech-a", "speech-b"],
    )
    monkeypatch.setattr(
        kobold_qwen_http, "_should_try_next_openai_candidate", lambda status: status == 404
    )
    authorizer_calls = 0

    def authorize_until_revoked() -> None:
        nonlocal authorizer_calls
        authorizer_calls += 1
        if authorizer_calls == 2:
            raise PermissionError("synthetic revocation")

    with model_acquisition_scope(authorize_until_revoked):
        with pytest.raises(ModelAcquisitionDenied):
            kobold_qwen_http._request_kobold_qwen_audio(
                "Hello",
                {"voice": "synthetic"},
                "http://kobold.invalid",
                resolve_api_key=lambda _settings: "synthetic-key",
                build_payload=lambda text, _settings: {"model": "qwen", "input": text},
            )

    assert len(calls) == 1
    assert authorizer_calls == 2


def test_batch_speech_preserves_payload_decoding_and_response_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_data = base64.b64encode(b"synthetic-audio").decode("ascii")
    line = json.dumps(
        {
            "type": "item",
            "id": "synthetic-1",
            "status": "completed",
            "audio_base64": audio_data,
            "response_format": "wav",
        }
    )
    response = _Response(lines=(line,))
    calls: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> _Response:
        calls.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(
        kobold_qwen_http, "_openai_audio_speech_batch_urls", lambda _url: ["batch-a"]
    )
    monkeypatch.setattr(kobold_qwen_http.requests, "post", post)
    response_states: list[bool] = []
    authorizer_calls = 0

    def authorize() -> None:
        nonlocal authorizer_calls
        authorizer_calls += 1

    with model_acquisition_scope(authorize):
        events = list(
            kobold_qwen_http._iter_kobold_qwen_batch_audio_http(
                _batch_item(),
                base_url="http://kobold.invalid",
                resolve_api_key=lambda _settings: "synthetic-key",
                build_payload=lambda text, _settings: {"model": "qwen", "input": text},
                decode_audio=_decode_audio,
                response_callback=lambda current: response_states.append(current is not None),
            )
        )

    assert authorizer_calls == 1
    assert len(calls) == 1
    assert calls[0]["url"] == "batch-a"
    assert calls[0]["json"] == {
        "items": [{"id": "synthetic-1", "model": "qwen", "input": "Hello"}],
        "stream": True,
        "fail_fast": False,
    }
    assert len(events) == 1 and events[0]["id"] == "synthetic-1"
    assert isinstance(events[0]["audio"], AudioSegment)
    assert response_states == [True, False]
    assert response.closed


def test_read_ahead_copies_actor_and_authorizer_and_propagates_retry_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    responses = iter((_Response(404), _Response(200)))

    def post(url: str, **_kwargs: Any) -> _Response:
        calls.append(url)
        return next(responses)

    monkeypatch.setattr(
        kobold_qwen_http,
        "_openai_audio_speech_batch_urls",
        lambda _url: ["batch-a", "batch-b"],
    )
    monkeypatch.setattr(
        kobold_qwen_http, "_should_try_next_openai_candidate", lambda status: status == 404
    )
    monkeypatch.setattr(kobold_qwen_http.requests, "post", post)

    actor = UserActor(
        user_id="synthetic-user",
        auth_revision=1,
        subject="user:synthetic-user",
        principal_kind="api_token",
        scopes=frozenset({"app.read"}),
        token_id="synthetic-token",
    )
    actor_token = current_execution_actor.set(actor)
    probe: ContextVar[str] = ContextVar("provider_acquisition_probe", default="missing")
    probe.set("parent")
    callback_actors: list[object] = []
    callback_context_values: list[str] = []
    callback_threads: list[int] = []
    authorizer_calls = 0
    parent_thread = get_ident()

    def authorize() -> None:
        nonlocal authorizer_calls
        authorizer_calls += 1
        callback_actors.append(current_execution_actor.get())
        callback_context_values.append(probe.get())
        callback_threads.append(get_ident())
        probe.set("worker")
        if authorizer_calls == 2:
            raise PermissionError("synthetic revocation")

    try:
        with model_acquisition_scope(authorize):
            with pytest.raises(ModelAcquisitionDenied):
                list(
                    iter_read_ahead_batch(
                        1,
                        producer=_batch_stream,
                    )
                )
        assert current_execution_actor.get() == actor
        assert probe.get() == "parent"
    finally:
        current_execution_actor.reset(actor_token)

    assert calls == ["batch-a"]
    assert authorizer_calls == 2
    assert callback_actors == [actor, actor]
    assert callback_context_values == ["parent", "worker"]
    assert len(set(callback_threads)) == 1
    assert callback_threads[0] != parent_thread


def test_trainer_and_installation_default_deny_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("side-effect")
        raise AssertionError("denied operation reached a side effect")

    monkeypatch.setattr(xtts_trainer_handler, "get_training_paths", unexpected)
    monkeypatch.setattr(xtts_trainer_handler, "run_training_process", unexpected)
    with model_acquisition_scope(None):
        with pytest.raises(ModelAcquisitionDenied):
            xtts_trainer_handler.start_training({})
        with pytest.raises(ModelAcquisitionDenied):
            xtts_trainer_handler._copy_trained_model(
                "synthetic-model",
                {"xtts_models_dir": str(tmp_path / "models")},
            )

    assert calls == []
    assert not (tmp_path / "models").exists()


def test_authorized_trainer_launch_and_bundle_install_use_existing_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_name = "synthetic-model"
    models_root = tmp_path / "models"
    models_root.mkdir()
    trainer_root = tmp_path / "trainer"
    source_bundle = trainer_root / model_name / "models" / "xtts-synthetic"
    source_bundle.mkdir(parents=True)
    for filename in xtts_trainer_handler.XTTS_MODEL_BUNDLE_FILENAMES:
        (source_bundle / filename).write_bytes(b"synthetic model artifact")
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"synthetic audio")
    paths = {
        "pixi_executable": "synthetic-pixi",
        "trainer_manifest": "synthetic-trainer.toml",
        "trainer_dir": str(trainer_root),
        "trainer_script": "synthetic-trainer.py",
        "xtts_models_dir": str(models_root),
        "whisperx_manifest": "synthetic-whisperx.toml",
        "legacy_conda_executable": "",
        "legacy_trainer_conda_env": "",
        "legacy_whisperx_conda_env": "",
    }
    monkeypatch.setattr(xtts_trainer_handler, "get_training_paths", lambda: dict(paths))
    monkeypatch.setattr(xtts_trainer_handler, "validate_training_environment", lambda: (True, ""))
    monkeypatch.setattr(xtts_trainer_handler, "_build_trainer_subprocess_env", lambda _paths: {})
    commands: list[list[str]] = []

    def run_synthetic_process(command: list[str], **_kwargs: Any) -> int:
        commands.append(command)
        return 0

    monkeypatch.setattr(xtts_trainer_handler, "run_training_process", run_synthetic_process)
    authorizer_calls = 0

    def authorize() -> None:
        nonlocal authorizer_calls
        authorizer_calls += 1

    with model_acquisition_scope(authorize):
        success, message = xtts_trainer_handler.start_training(
            {"model_name": model_name, "source_audio_path": str(audio_path)},
            model_root=models_root,
        )

    installed = models_root / model_name
    assert success
    assert "published" in message.lower() or "copied successfully" in message.lower()
    assert len(commands) == 1
    assert "synthetic-pixi" in commands[0]
    assert authorizer_calls == 2
    assert all(
        (installed / filename).is_file()
        for filename in xtts_trainer_handler.XTTS_MODEL_BUNDLE_FILENAMES
    )
