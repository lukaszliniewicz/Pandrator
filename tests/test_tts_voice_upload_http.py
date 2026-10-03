"""Provider policy survives shared multipart upload ownership changes."""

import inspect
import json
import subprocess
import sys

import pytest
import requests

from pandrator.logic import tts_handler


@pytest.mark.parametrize(
    ("provider", "options", "key", "optional_fields"),
    [
        ("xtts", {}, "sk-placeholder", {}),
        (
            "voxcpm",
            {"prompt_text": "  Reference transcript.  ", "mode": "  REFERENCE  "},
            "voxcpm-fixture-key",
            {"prompt_text": "Reference transcript.", "mode": "reference"},
        ),
        (
            "fishs2",
            {"prompt_text": "  Reference transcript.  "},
            "fishs2-fixture-key",
            {"prompt_text": "Reference transcript."},
        ),
        (
            "chatterbox",
            {"prompt_text": "  Reference transcript.  "},
            "sk-placeholder",
            {"prompt_text": "Reference transcript."},
        ),
        ("kobold_qwen", {}, "kobold_qwen-fixture-key", {}),
    ],
)
@pytest.mark.parametrize("sample_count", [1, 2])
def test_provider_wrapper_retains_its_upload_policy(
    monkeypatch, tmp_path, provider, options, key, optional_fields, sample_count
):
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"controlled WAV fixture")
    paths = [str(wav)]
    if sample_count == 2:
        second = tmp_path / "second.wav"
        second.write_bytes(b"controlled WAV fixture")
        paths.append(str(second))
    resolver_calls = []
    handles = []
    calls = []

    def resolve(*args, **kwargs):
        resolver_calls.append((args, kwargs))
        return key

    class UploadResponse(requests.Response):
        def json(self, **kwargs):
            assert all(handle.closed for handle in handles)
            return super().json(**kwargs)

    def post(url, **request_options):
        files = request_options["files"]
        assert [field for field, _sample in files] == [*["files"] * sample_count, "audio_sample"]
        for _field, (filename, handle, mime_type) in files:
            assert filename in {"sample.wav", "second.wav"}
            assert mime_type == "audio/wav"
            assert not handle.closed
            assert handle.read() == b"controlled WAV fixture"
            handles.append(handle)
        calls.append((url, request_options))
        response = UploadResponse()
        response.status_code = 200
        response._content = json.dumps({"voice_id": " opaque:voice/id "}).encode()
        return response

    if provider in {"voxcpm", "fishs2", "kobold_qwen"}:
        monkeypatch.setattr(tts_handler, f"_resolve_{provider}_api_key", resolve)
    monkeypatch.setattr(tts_handler.requests, "post", post)
    upload = getattr(tts_handler, f"upload_{provider}_speaker_voice")
    assert (
        upload(
            paths if sample_count == 2 else paths[0],
            " http://provider.invalid:8042/v1/ ",
            voice_id=" requested:voice ",
            **options,
        )
        == "opaque:voice/id"
    )
    assert resolver_calls == ([((), {})] if provider in {"voxcpm", "fishs2", "kobold_qwen"} else [])
    assert len(calls) == 1
    assert calls[0][0] == "http://provider.invalid:8042/v1/audio/voices"
    assert calls[0][1]["headers"] == {"Authorization": f"Bearer {key}"}
    assert calls[0][1]["timeout"] == 120
    assert calls[0][1]["data"] == {
        "voice_id": "requested:voice",
        "name": "requested:voice",
        "purpose": "user_data",
        **{
            name: value
            for name, value in optional_fields.items()
            if name != "prompt_text" or sample_count == 1
        },
    }
    assert len({id(handle) for handle in handles}) == sample_count + 1
    assert all(handle.closed for handle in handles)


@pytest.mark.parametrize("failure", ["request", "http", "invalid-json", "missing-id"])
def test_legacy_upload_failure_closes_file_and_stops_fallback(monkeypatch, tmp_path, failure):
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"controlled WAV fixture")
    handles = []
    calls = []
    error = requests.ConnectionError("controlled legacy failure")

    def post(url, **options):
        calls.append(url)
        parts = options["files"]
        if isinstance(parts, dict):
            assert set(parts) == {"file"}
            handles.append(parts["file"][1])
            assert not handles[-1].closed
            if failure == "request":
                raise error
            response = requests.Response()
            response.status_code = 500 if failure == "http" else 200
            response._content = b"invalid JSON" if failure == "invalid-json" else b"{}"
            return response
        handles.extend(handle for _field, (_filename, handle, _mime) in parts)
        response = requests.Response()
        response.status_code = 404
        return response

    monkeypatch.setattr(tts_handler.requests, "post", post)
    message = {
        "request": "Failed uploading voice",
        "http": r"voice upload failed \(500\)",
        "invalid-json": "did not return a file ID",
        "missing-id": "did not return a file ID",
    }[failure]
    with pytest.raises(RuntimeError, match=message) as caught:
        tts_handler.upload_kobold_qwen_speaker_voice(
            str(wav), "http://provider.invalid:8042", api_key="fixture-key"
        )
    assert calls == [
        "http://provider.invalid:8042/v1/audio/voices",
        "http://provider.invalid:8042/audio/voices",
        "http://provider.invalid:8042/v1/files",
    ]
    assert len(handles) == 5 and all(handle.closed for handle in handles)
    assert caught.value.__cause__ is (error if failure == "request" else None)


def test_lower_upload_owner_imports_without_the_facade():
    code = (
        "import sys\n"
        "from pandrator.logic import tts_voice_upload_http\n"
        "assert 'pandrator.logic.tts_handler' not in sys.modules\n"
        "assert not any(name.startswith('pandrator.web') for name in sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "name",
    [
        "_normalize_upload_wav_paths",
        "_extract_uploaded_identifier",
        "_upload_speaker_voice_openai_compatible",
    ],
)
def test_facade_reexports_the_canonical_upload_function(name):
    from pandrator.logic import tts_voice_upload_http

    facade = getattr(tts_handler, name)
    lower = getattr(tts_voice_upload_http, name)
    assert facade is lower
    assert inspect.signature(facade) == inspect.signature(lower)
