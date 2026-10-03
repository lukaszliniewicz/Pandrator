"""Native Qwen discovery and multipart upload characterization."""

import builtins
import json
from types import SimpleNamespace

import pytest
import requests

from pandrator.logic import tts_handler

BASE_URL = " http://qwen.invalid:8042/// "
ORIGIN = "http://qwen.invalid:8042"
RESOLVED_KEY = "discovery-fixture-key"
VOICE_PATHS = ["v1/audio/voices", "audio/voices", "v1/voices", "voices"]
FILE_PATHS = ["v1/files", "files"]
PROBE_PATHS = ["health", "v1/models", "models", *VOICE_PATHS, *FILE_PATHS]


def _response(status=200, payload=None, *, content=None, response_type=requests.Response):
    response = response_type()
    response.status_code = status
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode() if content is None else content
    return response


@pytest.fixture
def key_resolver(monkeypatch):
    calls = []

    def resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return RESOLVED_KEY

    monkeypatch.setattr(tts_handler, "_resolve_kobold_qwen_api_key", resolve)
    return calls


@pytest.fixture
def controlled_get(monkeypatch):
    def install(*outcomes):
        calls = []
        pending = iter(outcomes)

        def get(url, **options):
            calls.append((url, options))
            outcome = next(pending)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(tts_handler.requests, "get", get)
        return calls

    return install


def _expected_gets(paths, timeout, key=RESOLVED_KEY):
    return [
        (f"{ORIGIN}/{path}", {"headers": {"Authorization": f"Bearer {key}"}, "timeout": timeout})
        for path in paths
    ]


@pytest.mark.parametrize("eventually_online", [False, True])
def test_connection_advances_through_exact_candidates_and_native_errors(
    controlled_get, key_resolver, eventually_online
):
    outcomes = [
        requests.ConnectionError("controlled health failure"),
        _response(503),
        _response(404),
        _response(405),
        _response(501),
        _response(500),
        _response(404),
        _response(405),
        _response(200 if eventually_online else 503),
    ]
    calls = controlled_get(*outcomes)

    assert tts_handler.check_kobold_qwen_connection(BASE_URL) is eventually_online

    assert calls == _expected_gets(PROBE_PATHS, 4)
    assert key_resolver == [((), {})]


@pytest.mark.parametrize("success_index", [0, 1, 8])
def test_connection_short_circuits_on_any_status_below_400(
    controlled_get, key_resolver, success_index
):
    calls = controlled_get(*[_response(404) for _ in range(success_index)], _response(302))

    assert tts_handler.check_kobold_qwen_connection(BASE_URL) is True

    assert calls == _expected_gets(PROBE_PATHS[: success_index + 1], 4)
    assert key_resolver == [((), {})]


@pytest.mark.parametrize(
    "first",
    [
        pytest.param(_response(404), id="404"),
        pytest.param(_response(405), id="405"),
        pytest.param(_response(501), id="501"),
        pytest.param(_response(500), id="native-http-error"),
        pytest.param(requests.ConnectionError("controlled models failure"), id="request-error"),
        pytest.param(_response(content=b"invalid JSON"), id="invalid-json"),
        pytest.param(_response(payload={"data": []}), id="empty-list"),
        pytest.param(_response(payload={"data": "not a list"}), id="non-list-data"),
        pytest.param(_response(payload=[]), id="non-object-payload"),
    ],
)
def test_model_discovery_falls_back_with_preferred_prefix_and_sorted_unique_ids(
    controlled_get, key_resolver, first
):
    payload = {
        "data": [
            {"id": " z "},
            {"id": "Voice Cloning"},
            {"id": " a "},
            {"id": "a"},
            {"name": "ignored"},
            "ignored",
            {"id": ""},
        ]
    }
    calls = controlled_get(first, _response(payload=payload))

    assert tts_handler.get_kobold_qwen_models(BASE_URL) == [
        "Prebuilt Voices",
        "Voice Cloning",
        "a",
        "z",
    ]

    assert calls == _expected_gets(["v1/models", "models"], 8)
    assert key_resolver == [((), {})]


def test_model_discovery_stops_at_first_nonempty_native_payload(controlled_get, key_resolver):
    calls = controlled_get(_response(payload={"data": [{"id": " remote-model "}]}))

    assert tts_handler.get_kobold_qwen_models(BASE_URL) == [
        "Prebuilt Voices",
        "Voice Cloning",
        "remote-model",
    ]

    assert calls == _expected_gets(["v1/models"], 8)
    assert key_resolver == [((), {})]


def _catalogue_items():
    return [
        {
            "voice_id": " remote ",
            "id": "ignored-id",
            "name": "ignored-name",
            "type": " CLONED ",
            "model": " custom/model ",
        },
        {"id": " colon:id ", "name": "ignored-name", "type": " ", "model": " "},
        {"name": " Ryan "},
        {"id": "repeat", "model": "first"},
        {"voice_id": "REPEAT", "type": " SPECIAL ", "model": " last "},
        {"id": " Aiden ", "type": " CLONED ", "model": " remote-model "},
        " Scalar:Voice ",
        "Dylan",
        {"voice_id": ""},
        0,
    ]


@pytest.mark.parametrize("shape", ["list", "data", "voices", "items"])
@pytest.mark.parametrize("explicit_key", ["", " explicit-fixture-key "])
def test_catalogue_normalizes_metadata_and_selects_first_list_field(
    controlled_get, key_resolver, shape, explicit_key
):
    items = _catalogue_items()
    payloads = {
        "list": items,
        "data": {"data": items, "voices": [{"id": "wrong"}], "items": [{"id": "wrong"}]},
        "voices": {"data": "not-list", "voices": items, "items": [{"id": "wrong"}]},
        "items": {"data": {}, "voices": None, "items": items},
    }
    calls = controlled_get(_response(payload=payloads[shape]))

    catalogue = tts_handler.get_kobold_qwen_voice_catalog(BASE_URL, api_key=explicit_key)

    assert catalogue[:7] == [
        {"id": "remote", "type": "cloned", "model": "custom/model"},
        {"id": "colon:id", "type": "cloned", "model": ""},
        {"id": "Ryan", "type": "preset", "model": ""},
        {"id": "REPEAT", "type": "special", "model": "last"},
        {"id": "Aiden", "type": "cloned", "model": "remote-model"},
        {"id": "Scalar:Voice", "type": "cloned", "model": "Voice Cloning"},
        {"id": "Dylan", "type": "cloned", "model": "Voice Cloning"},
    ]
    assert all(set(item) == {"id", "type", "model"} for item in catalogue)
    assert len({item["id"].lower() for item in catalogue}) == len(catalogue)
    assert not any(item["id"].startswith("ignored") or item["id"] == "wrong" for item in catalogue)
    assert catalogue[-1] == {"id": "kobo", "type": "cloned", "model": "Voice Cloning"}
    assert calls == _expected_gets([VOICE_PATHS[0]], 8, explicit_key.strip() or RESOLVED_KEY)
    assert key_resolver == ([] if explicit_key else [((), {})])


@pytest.mark.parametrize("versioned_files_unsupported", [False, True])
def test_catalogue_native_failures_fall_back_to_files(
    controlled_get, key_resolver, versioned_files_unsupported
):
    outcomes = [
        _response(404),
        requests.ConnectionError("controlled voice failure"),
        _response(content=b"invalid JSON"),
        _response(500),
    ]
    if versioned_files_unsupported:
        outcomes.append(_response(501))
    outcomes.append(_response(payload={"data": [{"id": "file:voice"}]}))
    calls = controlled_get(*outcomes)

    catalogue = tts_handler.get_kobold_qwen_voice_catalog(BASE_URL)

    assert catalogue[0] == {"id": "file:voice", "type": "cloned", "model": ""}
    paths = [*VOICE_PATHS, *FILE_PATHS[: 2 if versioned_files_unsupported else 1]]
    assert calls == _expected_gets(paths, 8)
    assert key_resolver == [((), {})]


def test_catalogue_empty_first_list_does_not_read_later_fields(controlled_get, key_resolver):
    calls = controlled_get(
        _response(payload={"data": [], "voices": [{"id": "ignored"}]}),
        _response(payload={"voices": [{"id": "next-route"}]}),
    )

    catalogue = tts_handler.get_kobold_qwen_voice_catalog(BASE_URL)

    assert catalogue[0] == {"id": "next-route", "type": "cloned", "model": ""}
    assert not any(item["id"] == "ignored" for item in catalogue)
    assert calls == _expected_gets(VOICE_PATHS[:2], 8)
    assert key_resolver == [((), {})]


def test_catalogue_all_failure_returns_exact_nine_presets_and_kobo(controlled_get, key_resolver):
    calls = controlled_get(*[_response(404) for _ in range(6)])

    catalogue = tts_handler.get_kobold_qwen_voice_catalog(BASE_URL)

    assert catalogue == [
        {"id": voice, "type": "preset", "model": "Prebuilt Voices"}
        for voice in [
            "Aiden",
            "Dylan",
            "Eric",
            "Ono_Anna",
            "Ryan",
            "Serena",
            "Sohee",
            "Uncle_Fu",
            "Vivian",
        ]
    ] + [{"id": "kobo", "type": "cloned", "model": "Voice Cloning"}]
    assert calls == _expected_gets([*VOICE_PATHS, *FILE_PATHS], 8)
    assert key_resolver == [((), {})]


def test_voice_id_projection_resolves_facade_catalogue_when_called(monkeypatch):
    project = tts_handler.get_kobold_qwen_voices
    calls = []
    entries = [{"id": "opaque:id"}, {"id": " untouched/id "}, {"id": "Aiden"}]

    def catalogue(*args, **kwargs):
        calls.append((args, kwargs))
        return entries

    monkeypatch.setattr(tts_handler, "get_kobold_qwen_voice_catalog", catalogue)

    assert project(BASE_URL) == ["opaque:id", " untouched/id ", "Aiden"]
    assert calls == [((BASE_URL,), {})]


@pytest.mark.parametrize("explicit_key", ["", " wrapper-fixture-key "])
def test_qwen_upload_wrapper_preserves_paths_and_exact_shared_helper_arguments(
    monkeypatch, key_resolver, explicit_key
):
    paths = ["first.wav", "second.wav"]
    calls = []

    def upload(*args, **kwargs):
        calls.append((args, kwargs))
        return "opaque:uploaded"

    monkeypatch.setattr(tts_handler, "_upload_speaker_voice_openai_compatible", upload)

    assert (
        tts_handler.upload_kobold_qwen_speaker_voice(
            paths, BASE_URL, voice_id=" opaque:requested ", api_key=explicit_key
        )
        == "opaque:uploaded"
    )

    assert calls == [
        (
            (paths,),
            {
                "base_url": BASE_URL,
                "fallback_base_url": tts_handler.KOBOLD_QWEN_API_BASE_URL,
                "service_name": "Qwen3 TTS",
                "api_key": explicit_key.strip() or RESOLVED_KEY,
                "upload_purpose": "user_data",
                "voice_id": " opaque:requested ",
            },
        )
    ]
    assert calls[0][0][0] is paths
    assert key_resolver == ([] if explicit_key else [((), {})])


class UploadResponse(requests.Response):
    """Retain native response behavior while witnessing prior file closure."""

    def __init__(self):
        super().__init__()
        self.upload_handles = []
        self.evaluations = []

    def __getattribute__(self, name):
        if name == "status_code":
            state = object.__getattribute__(self, "__dict__")
            handles = state.get("upload_handles", [])
            if handles:
                assert all(handle.closed for handle in handles)
                state["evaluations"].append("status")
        return super().__getattribute__(name)

    def json(self, **kwargs):
        assert self.upload_handles and all(handle.closed for handle in self.upload_handles)
        self.evaluations.append("json")
        return super().json(**kwargs)


def _upload_response(status=200, payload=None, *, content=None):
    return _response(status, payload, content=content, response_type=UploadResponse)


@pytest.fixture
def wav_paths(tmp_path):
    paths = [tmp_path / " first narrator .wav", tmp_path / "second.WAV"]
    contents = [b"first controlled WAV bytes", b"second controlled WAV bytes"]
    for path, content in zip(paths, contents, strict=True):
        path.write_bytes(content)
    return [str(path) for path in paths], contents


@pytest.fixture
def upload_transport(monkeypatch):
    def install(*outcomes):
        records = SimpleNamespace(calls=[], parts=[], handles=[])
        pending = iter(outcomes)

        def post(url, **options):
            records.calls.append((url, options))
            files = options["files"]
            parts = list(files.items()) if isinstance(files, dict) else files
            captured = []
            for field, (filename, handle, mime) in parts:
                assert not handle.closed
                records.parts.append((field, filename, handle, mime, handle.read()))
                records.handles.append(handle)
                captured.append(handle)
            outcome = next(pending)
            if isinstance(outcome, Exception):
                raise outcome
            outcome.upload_handles = captured
            return outcome

        monkeypatch.setattr(tts_handler.requests, "post", post)
        return records

    return install


def _assert_upload_options(options, voice_id, key=RESOLVED_KEY):
    assert set(options) == {"headers", "files", "data", "timeout"}
    assert options["headers"] == {"Authorization": f"Bearer {key}"}
    assert options["data"] == {"voice_id": voice_id, "name": voice_id, "purpose": "user_data"}
    assert options["timeout"] == 120


@pytest.mark.parametrize("voice_id", [None, " opaque:voice "])
def test_preferred_upload_uses_native_multipart_and_closes_before_response_evaluation(
    wav_paths, key_resolver, upload_transport, voice_id
):
    paths, contents = wav_paths
    response = _upload_response(payload={"voice_id": " uploaded:id "})
    records = upload_transport(response)

    assert (
        tts_handler.upload_kobold_qwen_speaker_voice(
            paths, BASE_URL, voice_id=voice_id, api_key=" upload-fixture-key "
        )
        == "uploaded:id"
    )

    assert len(records.calls) == 1
    url, options = records.calls[0]
    assert url == f"{ORIGIN}/v1/audio/voices"
    _assert_upload_options(
        options, "opaque:voice" if voice_id else "first narrator", key="upload-fixture-key"
    )
    assert isinstance(options["files"], list)
    assert [
        (field, filename, mime, content)
        for field, filename, _handle, mime, content in records.parts
    ] == [
        ("files", " first narrator .wav", "audio/wav", contents[0]),
        ("files", "second.WAV", "audio/wav", contents[1]),
        ("audio_sample", " first narrator .wav", "audio/wav", contents[0]),
    ]
    assert len({id(handle) for handle in records.handles}) == 3
    assert all(handle.closed for handle in records.handles)
    assert "status" in response.evaluations and "json" in response.evaluations
    assert key_resolver == []


def test_unsupported_preferred_upload_falls_back_to_one_legacy_file_per_sample(
    wav_paths, key_resolver, upload_transport
):
    paths, contents = wav_paths
    responses = [
        _upload_response(404),
        _upload_response(405),
        _upload_response(payload={"id": "first-file"}),
        _upload_response(payload={"id": " final:file "}),
    ]
    records = upload_transport(*responses)

    assert tts_handler.upload_kobold_qwen_speaker_voice(paths, BASE_URL) == "final:file"

    assert [url for url, _options in records.calls] == [
        f"{ORIGIN}/v1/audio/voices",
        f"{ORIGIN}/audio/voices",
        f"{ORIGIN}/v1/files",
        f"{ORIGIN}/v1/files",
    ]
    for _url, options in records.calls:
        _assert_upload_options(options, "first narrator")
    assert [set(options["files"]) for _url, options in records.calls[2:]] == [{"file"}, {"file"}]
    assert [
        (field, filename, mime, content)
        for field, filename, _handle, mime, content in records.parts[-2:]
    ] == [
        ("file", " first narrator .wav", "audio/wav", contents[0]),
        ("file", "second.WAV", "audio/wav", contents[1]),
    ]
    assert all(handle.closed for handle in records.handles)
    assert all("status" in response.evaluations for response in responses)
    assert key_resolver == [((), {})]


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        pytest.param(
            requests.ConnectionError("native fixture failure"),
            "Failed uploading voice",
            id="request-exception",
        ),
        pytest.param(
            _upload_response(500, {"error": "controlled failure"}),
            r"voice upload failed \(500\)",
            id="http-error",
        ),
        pytest.param(
            _upload_response(content=b"invalid JSON"),
            "did not return a voice ID",
            id="invalid-json",
        ),
        pytest.param(_upload_response(payload={}), "did not return a voice ID", id="missing-id"),
    ],
)
def test_upload_failures_close_native_handles_and_preserve_wrapped_cause(
    wav_paths, key_resolver, upload_transport, outcome, message
):
    paths, _contents = wav_paths
    records = upload_transport(outcome)

    with pytest.raises(RuntimeError, match=message) as caught:
        tts_handler.upload_kobold_qwen_speaker_voice(paths, BASE_URL)

    assert len(records.calls) == 1
    assert len(records.handles) == 3 and all(handle.closed for handle in records.handles)
    assert caught.value.__cause__ is (outcome if isinstance(outcome, Exception) else None)
    assert key_resolver == [((), {})]


def test_native_open_failure_closes_prior_handle_and_wraps_oserror(
    monkeypatch, wav_paths, key_resolver, upload_transport
):
    paths, _contents = wav_paths
    records = upload_transport()
    original_open = builtins.open
    opened = []
    error = OSError("controlled native open failure")

    def open_with_failure(file, mode="r", *args, **kwargs):
        if file == paths[1] and mode == "rb":
            raise error
        handle = original_open(file, mode, *args, **kwargs)
        if file == paths[0] and mode == "rb":
            opened.append(handle)
        return handle

    with monkeypatch.context() as context:
        context.setattr(builtins, "open", open_with_failure)
        with pytest.raises(RuntimeError, match="Could not read WAV file") as caught:
            tts_handler.upload_kobold_qwen_speaker_voice(paths, BASE_URL)

    assert caught.value.__cause__ is error
    assert len(opened) == 1 and opened[0].closed
    assert records.calls == []
    assert key_resolver == [((), {})]


def test_all_upload_endpoints_unsupported_closes_handles_and_reports_failure(
    wav_paths, key_resolver, upload_transport
):
    paths, _contents = wav_paths
    records = upload_transport(*[_upload_response(status) for status in [404, 405, 501, 404]])

    with pytest.raises(RuntimeError, match="does not support voice upload endpoints"):
        tts_handler.upload_kobold_qwen_speaker_voice(paths, BASE_URL)

    assert [url for url, _options in records.calls] == [
        f"{ORIGIN}/v1/audio/voices",
        f"{ORIGIN}/audio/voices",
        f"{ORIGIN}/v1/files",
        f"{ORIGIN}/files",
    ]
    assert all(handle.closed for handle in records.handles)
    assert key_resolver == [((), {})]


@pytest.mark.parametrize("invalid", ["empty", "no-paths", "wrong-extension", "missing-wav"])
def test_invalid_upload_paths_raise_before_http(tmp_path, key_resolver, upload_transport, invalid):
    text_file = tmp_path / "sample.txt"
    text_file.write_bytes(b"controlled bytes")
    paths = {
        "empty": " ",
        "no-paths": [],
        "wrong-extension": str(text_file),
        "missing-wav": str(tmp_path / "missing.wav"),
    }
    records = upload_transport()

    with pytest.raises(ValueError):
        tts_handler.upload_kobold_qwen_speaker_voice(paths[invalid], BASE_URL)

    assert records.calls == []
    assert key_resolver == [((), {})]


def test_connection_looks_up_voice_candidates_after_resolving_credentials(monkeypatch):
    calls = []

    def catalogue_urls(base):
        calls.append(("catalogue", base))
        return [f"{base}/late-voices"]

    def resolve():
        monkeypatch.setattr(tts_handler, "_openai_voice_catalog_urls", catalogue_urls)
        return RESOLVED_KEY

    def get(url, **options):
        calls.append(("get", url, options))
        return _response(200 if url.endswith("late-voices") else 404)

    monkeypatch.setattr(tts_handler, "_resolve_kobold_qwen_api_key", resolve)
    monkeypatch.setattr(tts_handler.requests, "get", get)
    assert tts_handler.check_kobold_qwen_connection(BASE_URL)
    assert calls[0] == ("catalogue", ORIGIN)
    assert [call[1] for call in calls[1:]] == [
        f"{ORIGIN}/{path}" for path in ["health", "v1/models", "models", "late-voices"]
    ]


def test_models_look_up_parser_merge_and_preferred_list_when_used(monkeypatch, key_resolver):
    calls = []
    payload = {"data": [{"id": "provider:late"}]}

    def merge(preferred, discovered):
        calls.append(("merge", preferred, discovered))
        return preferred + discovered

    def parse(received):
        assert received == payload
        calls.append(("parse", received))
        monkeypatch.setattr(tts_handler, "_merge_catalog_with_discovered", merge)
        monkeypatch.setattr(tts_handler, "KOBOLD_QWEN_TTS_MODELS", ["configured:late"])
        return ["parsed:late"]

    def get(*_args, **_options):
        monkeypatch.setattr(tts_handler, "_extract_models_from_openai_payload", parse)
        return _response(payload=payload)

    monkeypatch.setattr(tts_handler.requests, "get", get)
    assert tts_handler.get_kobold_qwen_models(BASE_URL) == ["configured:late", "parsed:late"]
    assert calls == [("parse", payload), ("merge", ["configured:late"], ["parsed:late"])]
    assert key_resolver == [((), {})]


def test_catalogue_keeps_provider_seeds_live_after_http(monkeypatch, key_resolver):
    def get(*_args, **_options):
        monkeypatch.setattr(tts_handler, "KOBOLD_QWEN_TTS_VOICES", ["Late:Preset", "Seed:Only"])
        monkeypatch.setattr(tts_handler, "KOBOLD_QWEN_DEFAULT_MODEL", "late/default")
        monkeypatch.setattr(tts_handler, "KOBOLD_QWEN_SAMPLE_VOICE", "late:sample")
        return _response(payload={"data": [{"id": "Late:Preset"}, {"id": "opaque/voice"}]})

    monkeypatch.setattr(tts_handler.requests, "get", get)
    assert tts_handler.get_kobold_qwen_voice_catalog(BASE_URL) == [
        {"id": "Late:Preset", "type": "preset", "model": ""},
        {"id": "opaque/voice", "type": "cloned", "model": ""},
        {"id": "Seed:Only", "type": "preset", "model": "late/default"},
        {"id": "late:sample", "type": "cloned", "model": "Voice Cloning"},
    ]
    assert key_resolver == [((), {})]
