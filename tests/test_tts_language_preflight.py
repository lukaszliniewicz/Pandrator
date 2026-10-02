"""Provider/model-specific language preflight for synthesis routes."""

from unittest.mock import Mock

import pytest

from pandrator.logic import tts_handler
from pandrator.logic.speech_performance import compile_performance, resolve_capabilities
from pandrator.logic.tts_language_preflight import (
    resolve_tts_language_support,
    validate_tts_language,
)


def test_registry_alias_does_not_use_asr_normalization(monkeypatch):
    from pandrator.logic.dubbing import languages as asr_languages

    monkeypatch.setattr(
        asr_languages,
        "normalize_language_code",
        Mock(side_effect=AssertionError("ASR language normalization was used")),
    )

    result = validate_tts_language(
        {
            "service": "FishS2",
            "model": "fishaudio/s2-pro",
            "language": "Filipino",
        }
    )

    assert result["language"] == "fil"
    assert result["decision"] == "unverified"
    assert result["native_language"] == "fil"
    assert len(result["language_support"]["languages"]) == 83


def test_empty_model_is_unverified_without_fabricating_support():
    result = validate_tts_language({"service": "custom-service", "language": "en"})

    assert result == {
        "language": "en",
        "decision": "unverified",
        "language_support": None,
        "native_language": "en",
    }


def test_custom_openai_compatible_route_keeps_custom_provider_identity():
    endpoint = {
        "id": "private speech service",
        "provider": "openai",
        "adapter": "openai_compatible",
        "base_url": "https://private.example/api/v1?token=do-not-copy",
        "api_key": "do-not-copy",
        "model_catalog": [{"id": "future-model", "languages": ["en"]}],
    }

    record = resolve_tts_language_support(
        {"service": "private speech service", "model": "future-model"},
        endpoint=endpoint,
    )

    assert record["provider_id"] == "openai_compatible"
    assert record["model_id"] == "future-model"
    assert record["coverage"] == "claim"
    assert "private.example" not in str(record)
    assert "do-not-copy" not in str(record)

    unknown = validate_tts_language(
        {"service": "future-service", "model": "future-model", "language": "fil"},
        endpoint={"id": "future-service", "model_catalog": [{"id": "future-model"}]},
    )
    assert unknown["language_support"]["coverage"] == "unknown"
    assert unknown["decision"] == "unverified"
    assert unknown["language"] == "fil"


def test_silero_model_priority_and_native_language_aliases():
    endpoint = {"id": "silero", "provider": "silero"}
    settings = {
        "service": "Silero",
        "silero_model": "v5_cis_base",
        "xtts_model": "v3_en",
        "model": "v3_fr",
        "language": "myv",
    }

    support = resolve_tts_language_support(settings, endpoint=endpoint)
    result = validate_tts_language(settings, endpoint=endpoint)

    assert support["model_id"] == "v5_cis_base"
    assert result["decision"] == "supported"
    assert result["native_language"] == "erz"

    rejected = {**settings, "silero_model": "v3_en", "language": "az"}
    with pytest.raises(ValueError, match="does not support 'az'"):
        validate_tts_language(rejected, endpoint=endpoint)

    capability = resolve_capabilities(rejected, endpoint)
    assert capability["model"] == "v3_en"


def test_audio_cpp_package_must_have_tts_operation():
    with pytest.raises(ValueError, match="has no text-to-speech request route"):
        validate_tts_language(
            {
                "service": "audio.cpp",
                "model": "dots_tts_edit_q8_0",
                "language": "en",
            }
        )


def test_audio_cpp_preflight_preserves_regional_locales_and_firered_dialects():
    assert tts_handler._audio_cpp_language("magpie_tts_q8_0", "ar-AE") == "ar-AE"
    assert tts_handler._audio_cpp_language("magpie_tts_q8_0", "pt-BR") == "pt-BR"
    assert (
        tts_handler._audio_cpp_language("fireredtts3_base_q8_0", "ZH_Anhui")
        == "ZH_Anhui"
    )


@pytest.mark.parametrize(("language", "native"), [("az", "aze"), ("myv", "erz")])
def test_silero_request_uses_native_alias_from_model_evidence(monkeypatch, language, native):
    response = Mock()
    response.raise_for_status.return_value = None
    post = Mock(return_value=response)
    monkeypatch.setattr(tts_handler.requests, "post", post)
    monkeypatch.setattr(tts_handler, "_decode_audio_response", lambda _response: object())

    tts_handler.text_to_audio(
        "Text",
        {
            "service": "Silero",
            "silero_model": "v5_cis_base",
            "speaker": "ru_0",
            "language": language,
        },
        silero_base_url="http://silero",
        max_attempts=1,
    )

    assert post.call_args.kwargs["json"]["language"] == native


def test_unknown_silero_model_uses_record_alias_without_legacy_rewrite(monkeypatch):
    from pandrator.logic import tts_language_preflight

    response = Mock()
    response.raise_for_status.return_value = None
    post = Mock(return_value=response)
    monkeypatch.setattr(tts_handler.requests, "post", post)
    monkeypatch.setattr(tts_handler, "_decode_audio_response", lambda _response: object())
    monkeypatch.setattr(
        tts_handler,
        "normalize_silero_language_code",
        Mock(side_effect=AssertionError("legacy normalization replaced a model alias")),
    )
    monkeypatch.setattr(
        tts_language_preflight,
        "validate_tts_language",
        lambda *_args, **_kwargs: {
            "language": "qaa",
            "decision": "unverified",
            "native_language": "provider-token",
            "language_support": {
                "coverage": "unknown",
                "request_aliases": {"qaa": "provider-token"},
            },
        },
    )

    tts_handler.text_to_audio(
        "Text",
        {
            "service": "Silero",
            "silero_model": "future-model",
            "speaker": "voice",
            "language": "qaa",
        },
        silero_base_url="http://silero",
        max_attempts=1,
    )

    assert post.call_args.kwargs["json"]["language"] == "provider-token"


def test_exact_audio_cpp_language_rejection_happens_before_compile_or_http(monkeypatch):
    settings = {
        "service": "audio.cpp",
        "model": "qwen3_tts_1_7b_base_q8_0",
        "language": "az",
        "performance_enabled": False,
    }
    endpoint = {"id": "audio_cpp", "adapter": "audio_cpp"}

    with pytest.raises(ValueError, match="does not support 'az'"):
        compile_performance("Hello", settings, endpoint)

    post = Mock(side_effect=AssertionError("unsupported language reached HTTP"))
    monkeypatch.setattr(tts_handler.requests, "post", post)
    with pytest.raises(ValueError, match="does not support 'az'"):
        tts_handler.text_to_audio("Hello", settings, _audio_cpp_lock_held=True)
    post.assert_not_called()


def test_claim_outside_language_list_is_reported_unverified_not_rejected():
    compiled = compile_performance(
        "Hello",
        {
            "service": "FishS2",
            "model": "fishaudio/s2-pro",
            "language": "fil",
            "performance_enabled": False,
        },
    )

    assert any(
        item["control"] == "language" and item["status"] == "unverified"
        for item in compiled.report
    )


def test_kobold_qwen_builder_validates_before_compiler(monkeypatch):
    from pandrator.logic import speech_performance, tts_language_preflight

    def reject(*_args, **_kwargs):
        raise ValueError("unsupported language")

    compiler = Mock(side_effect=AssertionError("compiler ran before language guard"))
    post = Mock(side_effect=AssertionError("unsupported batch item reached HTTP"))
    monkeypatch.setattr(tts_language_preflight, "validate_tts_language", reject)
    monkeypatch.setattr(speech_performance, "compile_for_provider", compiler)
    monkeypatch.setattr(tts_handler.requests, "post", post)

    with pytest.raises(ValueError, match="unsupported language"):
        tts_handler._build_kobold_qwen_payload(
            "Hello", {"model": "Prebuilt Voices", "language": "en"}
        )

    with pytest.raises(ValueError, match="unsupported language"):
        list(
            tts_handler._iter_kobold_qwen_batch_audio_http(
                [
                    {
                        "id": "item-1",
                        "text": "Hello",
                        "settings": {"model": "Prebuilt Voices", "language": "en"},
                    }
                ],
                base_url="http://qwen",
                api_key="test",
            )
        )

    compiler.assert_not_called()
    post.assert_not_called()
