"""Preserve endpoint candidate, fallback and header policy across ownership."""

import subprocess
import sys

import pytest

from pandrator.logic import tts_handler


@pytest.mark.parametrize(
    ("base_url", "fallback", "expected"),
    [
        (None, "http://fallback.invalid", "http://fallback.invalid"),
        ("", "http://fallback.invalid", "http://fallback.invalid"),
        ("   ", "http://fallback.invalid", "http://fallback.invalid"),
        (" http://qwen.invalid:8042/// ", "http://fallback.invalid", "http://qwen.invalid:8042"),
    ],
)
def test_base_normalization_preserves_fallback(base_url, fallback, expected):
    assert tts_handler._normalize_base_url(base_url, fallback) == expected


@pytest.mark.parametrize("api_key", ["fixture-key", "", " fixture-key "])
def test_auth_headers_preserve_explicit_key(api_key):
    assert tts_handler._openai_auth_headers(api_key) == {"Authorization": f"Bearer {api_key}"}


def test_auth_headers_keep_the_captured_placeholder_default():
    assert tts_handler._openai_auth_headers() == {"Authorization": "Bearer sk-placeholder"}


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (
            "http://qwen.invalid",
            ["http://qwen.invalid/v1/audio/speech", "http://qwen.invalid/audio/speech"],
        ),
        (
            "http://qwen.invalid///",
            ["http://qwen.invalid/v1/audio/speech", "http://qwen.invalid/audio/speech"],
        ),
        ("http://qwen.invalid/v1/", ["http://qwen.invalid/v1/audio/speech"]),
        (
            "http://qwen.invalid/v10",
            ["http://qwen.invalid/v10/v1/audio/speech", "http://qwen.invalid/v10/audio/speech"],
        ),
    ],
)
def test_url_candidates_keep_versioned_first_order(base_url, expected):
    assert tts_handler._openai_url_candidates(base_url, "audio/speech") == expected


@pytest.mark.parametrize(
    ("name", "suffix"),
    [
        ("_openai_models_urls", "models"),
        ("_openai_voices_urls", "voices"),
        ("_openai_audio_voices_urls", "audio/voices"),
        ("_openai_audio_speech_urls", "audio/speech"),
        ("_openai_audio_speech_batch_urls", "audio/speech/batch"),
        ("_openai_capabilities_urls", "capabilities"),
        ("_openai_files_urls", "files"),
    ],
)
@pytest.mark.parametrize("versioned", [False, True])
def test_operation_urls_keep_their_suffix_and_candidate_order(name, suffix, versioned):
    base_url = "http://qwen.invalid/v1" if versioned else "http://qwen.invalid"
    expected = [f"http://qwen.invalid/v1/{suffix}"]
    if not versioned:
        expected.append(f"http://qwen.invalid/{suffix}")
    assert getattr(tts_handler, name)(base_url) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, False),
        (400, False),
        (401, False),
        (404, True),
        (405, True),
        (500, False),
        (501, True),
        (503, False),
    ],
)
def test_only_endpoint_compatibility_errors_try_another_candidate(status, expected):
    assert tts_handler._should_try_next_openai_candidate(status) is expected


def test_policy_owner_preserves_facade_function_and_constant_identity():
    from pandrator.logic import tts_openai_http_policy as policy

    names = (
        "_normalize_base_url",
        "_openai_auth_headers",
        "_should_try_next_openai_candidate",
        "_openai_url_candidates",
        "_openai_models_urls",
        "_openai_voices_urls",
        "_openai_audio_voices_urls",
        "_openai_audio_speech_urls",
        "_openai_audio_speech_batch_urls",
        "_openai_capabilities_urls",
        "_openai_files_urls",
        "XTTS_OPENAI_PLACEHOLDER_API_KEY",
        "OPENAI_CANDIDATE_FALLBACK_STATUS_CODES",
    )
    for name in names:
        assert getattr(tts_handler, name) is getattr(policy, name), name


def test_policy_owner_imports_without_loading_the_handler_or_requests():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pandrator.logic import tts_openai_http_policy; assert 'pandrator.logic.tts_handler' not in sys.modules; assert 'requests' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
