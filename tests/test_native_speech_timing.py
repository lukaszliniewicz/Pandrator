"""Fixture-only coverage for native timing and its audio compatibility boundary."""

import base64
import io
from unittest.mock import Mock, patch

import pytest
from pydub import AudioSegment

from pandrator.logic import tts_handler
from pandrator.logic.speech_timing import elevenlabs_speech_timing
from pandrator.web.generation_rendering import execute_render_parts
from pandrator.web.tts_provider_contracts import (
    TtsBatchItem,
    TtsSynthesisResult,
    synthesize_with_optional_timing,
)
from pandrator.web.tts_providers import TtsProviderRegistry


def alignment(text):
    return {
        "characters": list(text),
        "character_start_times_seconds": [index * 0.01 for index in range(len(text))],
        "character_end_times_seconds": [(index + 1) * 0.01 for index in range(len(text))],
    }


def settings():
    return {
        "service": "ElevenLabs",
        "speaker": "voice/one",
        "language": "en",
        "elevenlabs_output_format": "wav",
        "max_attempts": 3,
        "provider_configs": [{"id": "elevenlabs", "api_key": "fixture-key"}],
    }


def timed_response(text="Hello", timing=None):
    buffer = io.BytesIO()
    AudioSegment.silent(duration=300, frame_rate=16000).export(buffer, format="wav")
    response = Mock()
    response.json.return_value = {
        "audio_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "alignment": alignment(text) if timing is None else timing,
    }
    return response


def test_native_route_decode_and_legacy_compatibility():
    response = timed_response()
    with patch.object(tts_handler.requests, "post", return_value=response) as post:
        registry = TtsProviderRegistry()
        result = registry.synthesize_with_timing("Hello", settings())
        assert isinstance(result, TtsSynthesisResult)
        assert len(result.audio) == 300
        assert result.speech_timing["text"] == "Hello"
        assert post.call_args.args[0].endswith("/voice%2Fone/with-timestamps")
        assert post.call_args.kwargs["params"] == {"output_format": "wav"}
        assert post.call_args.kwargs["headers"]["Accept"] == "application/json"
        assert post.call_count == 1
        assert "_timing_sink" not in settings()
        registry.close()
    with patch.object(tts_handler.requests, "post", return_value=response) as post:
        with patch.object(
            tts_handler, "_decode_audio_response", return_value=AudioSegment.silent(duration=100)
        ) as decode:
            audio = tts_handler.text_to_audio("Hello", settings())
        assert len(audio) == 100
        assert not post.call_args.args[0].endswith("/with-timestamps")
        decode.assert_called_once_with(response)


def test_encoded_audio_decoder_receives_exact_requested_format_and_provider_input():
    configured = settings()
    configured["elevenlabs_output_format"] = "mp3_44100_128"
    response = timed_response("Hello world")
    with patch.object(tts_handler.requests, "post", return_value=response) as post:
        with patch.object(
            tts_handler, "_decode_audio_bytes", wraps=tts_handler._decode_audio_bytes
        ) as decode:
            sink = {}
            audio = tts_handler.text_to_audio(" Hello\n world ", configured, _timing_sink=sink)
    assert len(audio) == 300
    assert decode.call_args.kwargs["format_hint"] == "mp3_44100_128"
    assert sink["speech_timing"]["text"] == post.call_args.kwargs["json"]["text"] == "Hello world"


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"characters": ["H"]},
        {**alignment("Hello"), "character_end_times_seconds": [0] * 5},
        alignment("Hell"),
        {**alignment("Hello"), "character_end_times_seconds": [float("nan")] * 5},
        {**alignment("Hello"), "character_end_times_seconds": [1] * 5},
        {**alignment("Hello"), "character_start_times_seconds": [0, 0.02, 0.01, 0.03, 0.04]},
    ],
)
def test_malformed_alignment_keeps_paid_audio_without_retry(invalid):
    with patch.object(
        tts_handler.requests, "post", return_value=timed_response(timing=invalid)
    ) as post:
        registry = TtsProviderRegistry()
        result = registry.synthesize_with_timing("Hello", settings())
        registry.close()
    assert len(result.audio) == 300
    assert result.speech_timing is None
    assert post.call_count == 1


def test_normalization_preserves_char_spans_and_cjk_graphemes():
    text = "Ｆoo  cafe\u0301 中文か\u3099"
    native = "foo CAFÉ 中文が"
    timing = elevenlabs_speech_timing(text, alignment(native), duration_ms=300)
    assert timing is not None
    assert [word["text"] for word in timing["words"]] == [
        "Ｆoo",
        "cafe\u0301",
        "中",
        "文",
        "か\u3099",
    ]
    assert timing["diagnostics"]["complete"]
    for word in timing["words"]:
        assert text[word["start_char"] : word["end_char"]] == word["text"]
        assert isinstance(word["start_ms"], int) and isinstance(word["end_ms"], int)
        assert word["end_ms"] > word["start_ms"]
    assert (
        elevenlabs_speech_timing(text, alignment(native.replace("文", "字")), duration_ms=300)
        is None
    )


def test_zero_duration_characters_are_accepted_only_for_positive_words():
    metadata = alignment("ab c")
    metadata["character_end_times_seconds"][0] = 0
    metadata["character_end_times_seconds"][2] = 0.02
    timing = elevenlabs_speech_timing("ab c", metadata, duration_ms=100)
    assert [word["text"] for word in timing["words"]] == ["ab", "c"]
    metadata["character_end_times_seconds"][3] = 0.03
    assert elevenlabs_speech_timing("ab c", metadata, duration_ms=100) is None


def test_submillisecond_word_collapsing_under_canonical_quantization_is_rejected():
    metadata = {
        "characters": ["a"],
        "character_start_times_seconds": [0],
        "character_end_times_seconds": [0.0004],
    }
    assert elevenlabs_speech_timing("a", metadata, duration_ms=100) is None


def test_quoted_punctuation_surface_is_complete_with_unchanged_lexical_timestamps():
    text = "  “Hello,” world! 中文か\u3099。  "
    timing = elevenlabs_speech_timing(text, alignment(text), duration_ms=400)
    assert timing is not None
    words = timing["words"]
    assert [word["text"] for word in words] == ["“Hello,”", "world!", "中", "文", "か\u3099。"]
    assert (words[0]["start_ms"], words[0]["end_ms"]) == (30, 80)
    assert (words[1]["start_ms"], words[1]["end_ms"]) == (110, 160)
    for word in words:
        assert text[word["start_char"] : word["end_char"]] == word["text"]
    assert " ".join(word["text"] for word in words) == "“Hello,” world! 中 文 か\u3099。"


def test_overlapping_native_words_are_rejected_but_touching_is_valid():
    metadata = {
        "characters": list("a b"),
        "character_start_times_seconds": [0, 0.005, 0.01],
        "character_end_times_seconds": [0.02, 0.02, 0.03],
    }
    assert elevenlabs_speech_timing("a b", metadata, duration_ms=100) is None
    metadata["character_start_times_seconds"] = [0, 0.02, 0.02]
    timing = elevenlabs_speech_timing("a b", metadata, duration_ms=100)
    assert timing is not None
    assert timing["words"][0]["end_ms"] == timing["words"][1]["start_ms"] == 20


def test_optional_hook_uses_concrete_type_and_old_provider_path():
    audio = AudioSegment.silent(duration=10)
    provider = Mock()
    provider.synthesize.return_value = audio
    result = synthesize_with_optional_timing(provider, "Hello", {})
    assert result.audio is audio and result.speech_timing is None
    provider.synthesize.assert_called_once()
    provider.synthesize_with_timing.assert_not_called()

    class OldAdapter:
        service_id = "fixture"

        def synthesize(self, text, settings, **options):
            assert "_timing_sink" not in options
            return audio

    registry = TtsProviderRegistry()
    registry.register(OldAdapter())
    result = registry.synthesize_with_timing("Hello", {"service_id": "fixture"})
    assert result.audio is audio and result.speech_timing is None
    registry.close()


def test_parallel_batch_preserves_native_timing():
    registry = TtsProviderRegistry()
    with patch.object(tts_handler.requests, "post", return_value=timed_response()):
        results = list(
            registry.synthesize_batch(
                [TtsBatchItem("a", "Hello", settings()), TtsBatchItem("b", "Hello", settings())],
                batch_size=2,
            )
        )
    assert [item.id for item in results] == ["a", "b"]
    assert all(item.speech_timing is not None for item in results)
    registry.close()


def test_cast_manifest_offsets_include_one_pause_and_final_pcm_rate():
    parts = [
        {"text": "A", "start": 0, "end": 1, "settings": {"voice": "first"}},
        {
            "text": "B",
            "start": 2,
            "end": 3,
            "settings": {"voice": "second"},
            "silence_before_ms": 20,
        },
        {
            "text": "C",
            "start": 4,
            "end": 5,
            "settings": {"voice": "third"},
            "silence_before_ms": 30,
        },
    ]
    audios = [AudioSegment.silent(duration=100, frame_rate=rate) for rate in (16000, 22050, 48000)]
    results = iter(
        [
            TtsSynthesisResult(
                audio, elevenlabs_speech_timing(text, alignment(text), duration_ms=100)
            )
            for text, audio in zip("ABC", audios, strict=True)
        ]
    )
    combined, manifest = execute_render_parts(
        parts, synthesize=lambda *_: next(results), cancelled=lambda: False
    )
    expected = audios[0] + AudioSegment.silent(duration=20, frame_rate=16000) + audios[1]
    expected += AudioSegment.silent(duration=30, frame_rate=22050)
    expected += audios[2]
    assert combined.raw_data == expected.raw_data
    assert combined.frame_rate == 48000
    assert all(part["sample_rate_hz"] == 48000 for part in manifest)
    assert manifest[0]["start_frame"] == 0
    assert (
        manifest[0]["end_frame"]
        == audios[0].set_frame_rate(22050).set_frame_rate(48000).frame_count()
    )
    prefix = audios[0] + AudioSegment.silent(duration=20, frame_rate=16000) + audios[1]
    assert manifest[1]["end_frame"] == prefix.set_frame_rate(48000).frame_count()
    prefix += AudioSegment.silent(duration=30, frame_rate=22050)
    assert manifest[2]["start_frame"] == prefix.set_frame_rate(48000).frame_count()
    assert manifest[-1]["end_frame"] == combined.frame_count()
    for previous, current in zip(manifest, manifest[1:], strict=False):
        assert (
            abs(current["start_frame"] - previous["end_frame"] - current["silence_before_ms"] * 48)
            <= 2
        )
    assert [part["speech_timing"]["text"] for part in manifest] == list("ABC")
