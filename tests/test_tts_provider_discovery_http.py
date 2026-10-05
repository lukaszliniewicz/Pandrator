"""Literal public provider discovery contracts using real requests responses."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest
import requests

from pandrator.logic import tts_handler

BASE = "http://provider.invalid"
DEFAULT_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"
PLACEHOLDER_HEADERS = {"Authorization": "Bearer sk-placeholder"}
KEY_HEADERS = {"Authorization": "Bearer fixture-key"}
MODELS_URLS = ("http://provider.invalid/v1/models", "http://provider.invalid/models")
VOICES_URLS = (
    "http://provider.invalid/v1/audio/voices",
    "http://provider.invalid/audio/voices",
    "http://provider.invalid/v1/voices",
    "http://provider.invalid/voices",
)
FILES_URLS = ("http://provider.invalid/v1/files", "http://provider.invalid/files")
Result = requests.Response | Exception


def response(status: int = 200, payload: object = None, *, malformed: bool = False):
    result = requests.Response()
    result.status_code = status
    result.encoding = "utf-8"
    result._content = b"not JSON" if malformed else json.dumps(payload).encode("utf-8")
    return result


@dataclass(frozen=True)
class Step:
    url: str
    options: dict[str, object]
    result: Result


class GetScript:
    """A finite GET script: exact order/options, no extra or unconsumed calls."""

    def __init__(self, steps: list[Step], hook: Callable[[], None] | None = None):
        self.steps = steps
        self.index = 0
        self.hook = hook

    def get(self, url: str, **options: object) -> requests.Response:
        assert self.index < len(self.steps), f"Surplus GET: {url}"
        step = self.steps[self.index]
        assert (url, options) == (step.url, step.options)
        self.index += 1
        if self.index == 1 and self.hook:
            self.hook()
        if isinstance(step.result, Exception):
            raise step.result
        return step.result

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tts_handler.requests, "get", self.get)

    def assert_consumed(self) -> None:
        assert self.index == len(self.steps), "Unconsumed scripted GETs"


@dataclass(frozen=True)
class Provider:
    name: str
    default_base: str
    paths: tuple[str, ...]
    v1_paths: tuple[str, ...]
    headers: dict[str, str] | None = None
    timeout: int = 4


COMMON_PATHS = (
    "/health",
    "/v1/models",
    "/models",
    "/v1/audio/voices",
    "/audio/voices",
    "/v1/voices",
    "/voices",
    "/v1/files",
    "/files",
)
COMMON_V1_PATHS = ("/health", "/models", "/audio/voices", "/voices", "/files")
PROVIDERS = (
    Provider("voxcpm", "http://127.0.0.1:8020", COMMON_PATHS, COMMON_V1_PATHS, KEY_HEADERS),
    Provider("fishs2", "http://127.0.0.1:8020", COMMON_PATHS, COMMON_V1_PATHS, KEY_HEADERS),
    Provider(
        "chatterbox", "http://127.0.0.1:8040", COMMON_PATHS, COMMON_V1_PATHS, PLACEHOLDER_HEADERS
    ),
    Provider(
        "voxtral",
        "http://127.0.0.1:8000",
        (
            "/health",
            "/v1/audio/models",
            "/audio/models",
            "/v1/models",
            "/models",
            "/v1/audio/voices",
            "/audio/voices",
            "/v1/voices",
            "/voices",
        ),
        ("/health", "/audio/models", "/models", "/audio/voices", "/voices"),
        KEY_HEADERS,
    ),
    Provider(
        "kokoro",
        "http://127.0.0.1:8880",
        (
            "/health",
            "/v1/models",
            "/models",
            "/v1/audio/voices",
            "/audio/voices",
            "/v1/voices",
            "/voices",
        ),
        ("/health", "/models", "/audio/voices", "/voices"),
        KEY_HEADERS,
    ),
    Provider(
        "xtts",
        "http://127.0.0.1:8020",
        ("/health", "/v1/models", "/docs", "/"),
        ("/health", "/v1/models", "/docs", "/"),
        timeout=3,
    ),
    Provider(
        "silero",
        "http://127.0.0.1:8001",
        ("/ready", "/health", "/v1/models"),
        ("/ready", "/health", "/v1/models"),
    ),
)


@pytest.fixture(autouse=True)
def fixture_credentials(monkeypatch: pytest.MonkeyPatch):
    for name in ("voxcpm", "fishs2", "voxtral", "kokoro"):
        monkeypatch.setattr(tts_handler, f"_resolve_{name}_api_key", lambda: "fixture-key")


def health_steps(provider: Provider, urls: list[str], results: list[Result]) -> list[Step]:
    options: dict[str, object] = {"timeout": provider.timeout}
    if provider.headers is not None:
        options["headers"] = provider.headers
    assert len(urls) == len(results)
    return [Step(url, options, result) for url, result in zip(urls, results, strict=True)]


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
@pytest.mark.parametrize(
    "scenario", (200, 302, 404, 500, "timeout", "connection", 401, 403, 405, 501)
)
def test_health_matrix(monkeypatch: pytest.MonkeyPatch, provider: Provider, scenario: int | str):
    urls = [BASE + path for path in provider.paths]
    if scenario in ("timeout", "connection"):
        error = (
            requests.exceptions.Timeout
            if scenario == "timeout"
            else (requests.exceptions.ConnectionError)
        )
        results: list[Result] = [error("fixture failure"), response(200)]
        expected = True
    else:
        assert isinstance(scenario, int)
        expected = scenario in (200, 302) or (
            provider.name == "xtts" and scenario in (401, 403, 405)
        )
        results = [response(scenario)] * (1 if expected else len(urls))
    script = GetScript(health_steps(provider, urls[: len(results)], results))
    script.install(monkeypatch)
    check = getattr(tts_handler, f"check_{provider.name}_connection")
    assert check(BASE) is expected
    script.assert_consumed()


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
def test_health_default_normalization_and_v1_paths(
    monkeypatch: pytest.MonkeyPatch, provider: Provider
):
    check = getattr(tts_handler, f"check_{provider.name}_connection")
    default = GetScript(
        health_steps(provider, [provider.default_base + provider.paths[0]], [response(200)])
    )
    default.install(monkeypatch)
    assert check() is True
    default.assert_consumed()

    normalized = GetScript(health_steps(provider, [BASE + provider.paths[0]], [response(302)]))
    normalized.install(monkeypatch)
    assert check("  http://provider.invalid///  ") is True
    normalized.assert_consumed()

    urls = ["http://provider.invalid/v1" + path for path in provider.v1_paths]
    v1 = GetScript(health_steps(provider, urls, [response(404) for _ in urls]))
    v1.install(monkeypatch)
    assert check("  http://provider.invalid/v1///  ") is False
    v1.assert_consumed()


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
def test_health_unexpected_get_error_propagates(
    monkeypatch: pytest.MonkeyPatch, provider: Provider
):
    script = GetScript(
        health_steps(
            provider, [BASE + provider.paths[0]], [RuntimeError("unexpected fixture error")]
        )
    )
    script.install(monkeypatch)
    with pytest.raises(RuntimeError, match="unexpected fixture error"):
        getattr(tts_handler, f"check_{provider.name}_connection")(BASE)
    script.assert_consumed()


def test_health_headers_replaced_during_first_get_are_used_later(
    monkeypatch: pytest.MonkeyPatch,
):
    resolver_calls: list[None] = []
    header_keys: list[str] = []

    def resolve() -> str:
        resolver_calls.append(None)
        return "fixture-key"

    def replacement(key: str) -> dict[str, str]:
        header_keys.append(key)
        return {"Authorization": "Bearer late-key"}

    def replace() -> None:
        monkeypatch.setattr(tts_handler, "_openai_auth_headers", replacement)

    monkeypatch.setattr(tts_handler, "_resolve_voxcpm_api_key", resolve)
    script = GetScript(
        [
            Step(
                "http://provider.invalid/health",
                {"headers": KEY_HEADERS, "timeout": 4},
                response(404),
            ),
            Step(
                "http://provider.invalid/v1/models",
                {"headers": {"Authorization": "Bearer late-key"}, "timeout": 4},
                response(200),
            ),
        ],
        hook=replace,
    )
    script.install(monkeypatch)
    assert tts_handler.check_voxcpm_connection(BASE) is True
    assert resolver_calls == [None]
    assert header_keys == ["fixture-key"]
    script.assert_consumed()


def test_health_status_policy_replaced_during_get_is_used(
    monkeypatch: pytest.MonkeyPatch,
):
    status_calls: list[int] = []

    def replacement(status: int) -> bool:
        status_calls.append(status)
        return status == 200

    def replace() -> None:
        monkeypatch.setattr(tts_handler, "_should_try_next_openai_candidate", replacement)

    provider = PROVIDERS[0]
    urls = [BASE + path for path in provider.paths]
    script = GetScript(health_steps(provider, urls, [response(200) for _ in urls]), hook=replace)
    script.install(monkeypatch)
    assert tts_handler.check_voxcpm_connection(BASE) is False
    assert status_calls == [200] * len(urls)
    script.assert_consumed()


def catalog_step(url: str, result: Result, purpose: str | None = None) -> Step:
    options: dict[str, object] = {"headers": PLACEHOLDER_HEADERS, "timeout": 8}
    if purpose is not None:
        options["params"] = {"purpose": purpose, "limit": 10000}
    return Step(url, options, result)


def failed_response(kind: int | str) -> Result:
    if kind == "malformed":
        return response(malformed=True)
    if kind == "timeout":
        return requests.exceptions.Timeout("fixture timeout")
    assert isinstance(kind, int)
    return response(kind)


@pytest.mark.parametrize("failure", (404, 405, 501, 401, 403, 500, "malformed", "timeout"))
def test_models_fallback(monkeypatch: pytest.MonkeyPatch, failure: int | str):
    script = GetScript(
        [
            catalog_step(MODELS_URLS[0], failed_response(failure)),
            catalog_step(MODELS_URLS[1], response(payload={"data": [{"id": "discovered"}]})),
        ]
    )
    script.install(monkeypatch)
    assert tts_handler.get_xtts_models(BASE) == [DEFAULT_MODEL, "discovered"]
    script.assert_consumed()


def test_models_default_preferred_exact_case_sorted_dedup(monkeypatch: pytest.MonkeyPatch):
    payload = {
        "data": [
            {"id": " zulu "},
            {"id": "beta"},
            {"id": "Beta"},
            {"id": " beta "},
            {"id": DEFAULT_MODEL},
            {"id": ""},
            "ignored",
            {},
            {"id": 7},
        ]
    }
    script = GetScript([catalog_step("http://127.0.0.1:8020/v1/models", response(payload=payload))])
    script.install(monkeypatch)
    assert tts_handler.get_xtts_models() == [DEFAULT_MODEL, "7", "Beta", "beta", "zulu"]
    script.assert_consumed()


@pytest.mark.parametrize("payload", ({"data": []}, {"unknown": ["model"]}))
def test_models_successful_empty_or_unknown_stops(monkeypatch: pytest.MonkeyPatch, payload: object):
    script = GetScript([catalog_step(MODELS_URLS[0], response(payload=payload))])
    script.install(monkeypatch)
    assert tts_handler.get_xtts_models("  http://provider.invalid///  ") == [DEFAULT_MODEL]
    script.assert_consumed()


def test_models_all_fail_and_v1_single_candidate(monkeypatch: pytest.MonkeyPatch):
    script = GetScript([catalog_step(url, response(500)) for url in MODELS_URLS])
    script.install(monkeypatch)
    assert tts_handler.get_xtts_models(BASE) == [DEFAULT_MODEL]
    script.assert_consumed()
    prefixed = GetScript([catalog_step("http://provider.invalid/v1/models", response(404))])
    prefixed.install(monkeypatch)
    assert tts_handler.get_xtts_models(" http://provider.invalid/v1/// ") == [DEFAULT_MODEL]
    prefixed.assert_consumed()


def test_speakers_default_catalog_and_file_order_purposes(monkeypatch: pytest.MonkeyPatch):
    voices = {
        "data": [{"voice_id": "zulu"}, {"id": "Alpha"}, {"id": "Alpha"}],
        "voices": [" beta ", {"name": "named"}, ""],
    }
    # Both allowed purposes apply to EACH file response, regardless of query purpose.
    user_files = {
        "data": [
            {"id": "second", "purpose": "assistants"},
            {"id": "first", "purpose": "user_data"},
            {"id": "no-purpose"},
            {"id": "ignored", "purpose": "fine-tune"},
            {"id": "Alpha", "purpose": "user_data"},
            {"id": "second"},
            "ignored",
        ]
    }
    assistant_files = {
        "data": [
            {"id": "last", "purpose": "user_data"},
            {"id": "assistant", "purpose": "assistants"},
            {"id": "first"},
            {"id": "ignored-too", "purpose": "batch"},
        ]
    }
    script = GetScript(
        [
            catalog_step("http://127.0.0.1:8020/v1/audio/voices", response(payload=voices)),
            catalog_step(
                "http://127.0.0.1:8020/v1/files", response(payload=user_files), "user_data"
            ),
            catalog_step(
                "http://127.0.0.1:8020/v1/files", response(payload=assistant_files), "assistants"
            ),
        ]
    )
    script.install(monkeypatch)
    assert tts_handler.get_xtts_speakers() == [
        "Alpha",
        "beta",
        "named",
        "zulu",
        "second",
        "first",
        "no-purpose",
        "last",
        "assistant",
    ]
    script.assert_consumed()


@pytest.mark.parametrize("failure", (404, 405, 501, 401, 500, "malformed", "timeout"))
def test_speakers_fallback_order(monkeypatch: pytest.MonkeyPatch, failure: int | str):
    script = GetScript(
        [
            *[catalog_step(url, failed_response(failure)) for url in VOICES_URLS[:3]],
            catalog_step(VOICES_URLS[3], response(payload={"voices": ["voice"]})),
            catalog_step(FILES_URLS[0], failed_response(failure), "user_data"),
            catalog_step(FILES_URLS[1], response(payload={"data": [{"id": "file"}]}), "user_data"),
            catalog_step(FILES_URLS[0], failed_response(failure), "assistants"),
            catalog_step(FILES_URLS[1], response(payload={"data": []}), "assistants"),
        ]
    )
    script.install(monkeypatch)
    assert tts_handler.get_xtts_speakers(BASE) == ["voice", "file"]
    script.assert_consumed()


def test_speakers_successful_empty_catalogs_stop_but_still_fetch_files(
    monkeypatch: pytest.MonkeyPatch,
):
    script = GetScript(
        [
            catalog_step(VOICES_URLS[0], response(payload={"data": []})),
            catalog_step(FILES_URLS[0], response(payload={"data": []}), "user_data"),
            catalog_step(FILES_URLS[0], response(payload={"data": []}), "assistants"),
        ]
    )
    script.install(monkeypatch)
    assert tts_handler.get_xtts_speakers("  http://provider.invalid///  ") == []
    script.assert_consumed()


def test_speakers_all_fail_and_v1_fewer_candidates(monkeypatch: pytest.MonkeyPatch):
    script = GetScript(
        [
            *[catalog_step(url, response(404)) for url in VOICES_URLS],
            *[
                catalog_step(url, response(500), purpose)
                for purpose in ("user_data", "assistants")
                for url in FILES_URLS
            ],
        ]
    )
    script.install(monkeypatch)
    assert tts_handler.get_xtts_speakers(BASE) == []
    script.assert_consumed()
    prefixed = GetScript(
        [
            catalog_step("http://provider.invalid/v1/audio/voices", response(404)),
            catalog_step("http://provider.invalid/v1/voices", response(404)),
            catalog_step("http://provider.invalid/v1/files", response(404), "user_data"),
            catalog_step("http://provider.invalid/v1/files", response(404), "assistants"),
        ]
    )
    prefixed.install(monkeypatch)
    assert tts_handler.get_xtts_speakers(" http://provider.invalid/v1/// ") == []
    prefixed.assert_consumed()


def test_models_parser_replaced_during_get_is_used(monkeypatch: pytest.MonkeyPatch):
    payload = {"data": [{"id": "original"}]}
    parsed: list[object] = []

    def replacement(value: object) -> list[str]:
        parsed.append(value)
        return ["late-model"]

    def replace() -> None:
        monkeypatch.setattr(tts_handler, "_extract_models_from_openai_payload", replacement)

    script = GetScript([catalog_step(MODELS_URLS[0], response(payload=payload))], hook=replace)
    script.install(monkeypatch)
    assert tts_handler.get_xtts_models(BASE) == [DEFAULT_MODEL, "late-model"]
    assert parsed == [payload]
    script.assert_consumed()


def test_speakers_file_parser_replaced_during_voice_get_is_used(
    monkeypatch: pytest.MonkeyPatch,
):
    parsed: list[tuple[object, object]] = []
    user_payload = {"data": [{"id": "user-original"}]}
    assistant_payload = {"data": [{"id": "assistant-original"}]}

    def replacement(value: object, *, allowed_purposes: object) -> list[str]:
        parsed.append((value, allowed_purposes))
        return ["late-file"]

    def replace() -> None:
        monkeypatch.setattr(tts_handler, "_extract_file_ids_from_openai_payload", replacement)

    script = GetScript(
        [
            catalog_step(VOICES_URLS[0], response(payload={"voices": ["voice"]})),
            catalog_step(FILES_URLS[0], response(payload=user_payload), "user_data"),
            catalog_step(FILES_URLS[0], response(payload=assistant_payload), "assistants"),
        ],
        hook=replace,
    )
    script.install(monkeypatch)
    assert tts_handler.get_xtts_speakers(BASE) == ["voice", "late-file"]
    assert parsed == [
        (user_payload, {"user_data", "assistants"}),
        (assistant_payload, {"user_data", "assistants"}),
    ]
    script.assert_consumed()
