"""No-network regression tests for native Qwen alignment and its safety boundary."""

import hashlib
import io
import json
import subprocess
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing import qwen_alignment as qwen
from pandrator.logic.dubbing.caption_alignment import (
    align_caption_cues,
    map_ctc_words_to_cues,
)
from pandrator.logic.dubbing.crispasr import ctc_language_problem, _align_moss_segments
from pandrator.logic.media_edit import MediaCue


def wav(path, seconds=5):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\0\0" * 16000 * seconds)
    return path


@pytest.mark.parametrize("language", list(qwen.LANGUAGES))
def test_all_eleven_declared_languages_are_supported(language):
    settings = {
        "source_language": language,
        "caption_alignment_ctc_model": qwen.MODEL_ID,
    }
    assert qwen.uses_qwen(settings)
    assert qwen.language_problem(settings) is None
    assert ctc_language_problem(settings) is None


@pytest.mark.parametrize(
    "language",
    ["Japanese", "ja-JP", "jpn", "zh-Hant", "zh-Hans", "ko_KR", "Cantonese", "yue"],
)
def test_auto_routes_cjk_and_cantonese_to_qwen(language):
    assert qwen.uses_qwen({"stt_language": language})


def test_explicit_selection_and_audio_language_are_authoritative():
    assert not qwen.uses_qwen(
        {"source_language": "en", "target_language": "ja"}, "English source"
    )
    assert not qwen.uses_qwen(
        {"source_language": "ja", "caption_alignment_ctc_model": "canary-ctc-aligner"}
    )
    assert "unsupported_qwen" in qwen.language_problem({"source_language": "pl"})
    assert "unknown" in qwen.language_problem({}, "An English-looking caption")
    assert qwen.uses_qwen({}, "日本語の字幕")


def test_native_sample_offsets_are_seconds_and_confidence_is_not_fabricated():
    result = qwen.parse_words(
        [{"word": "日本", "start_sample": 1600, "end_sample": 8000, "confidence": 0.0}],
        1,
    )
    assert result == [{"word": "日本", "start": 0.1, "end": 0.5}]


@pytest.mark.parametrize(
    "start,end", [(float("nan"), 100), (-1, 100), (100, 100), (100, 16001)]
)
def test_invalid_or_out_of_audio_native_timing_is_rejected(start, end):
    with pytest.raises(qwen.QwenAlignmentError):
        qwen.parse_words(
            [{"word": "日本", "start_sample": start, "end_sample": end}], 1
        )


def test_original_punctuation_and_decomposed_kana_are_restored():
    text = "「か\u3099くせい」は日本語を学びます。"
    values = [
        {"word": word, "start": n, "end": n + 0.8}
        for n, word in enumerate(
            ["がくせい", "は", "日", "本", "語", "を", "学", "びます"]
        )
    ]
    restored = qwen.restore_surfaces(text, values)
    assert "".join(item["word"] for item in restored) == text
    assert [(r["start"], r["end"]) for r in restored] == [
        (r["start"], r["end"]) for r in values
    ]


@pytest.mark.parametrize("word", ["日本", "日本語です違う", "にほんごです"])
def test_changed_missing_or_romanized_surfaces_are_not_accepted(word):
    with pytest.raises(qwen.QwenAlignmentError):
        qwen.restore_surfaces("日本語です。", [{"word": word, "start": 0, "end": 1}])


def test_alignment_only_kana_segmentation_preserves_lexical_content():
    text = "きょうは、にほんごのじかんです。"
    prepared = qwen.alignment_text(text, "ja")
    assert " " in prepared
    assert qwen.alignment_key(prepared) == qwen.alignment_key(text)
    assert "きょ" in prepared.split()


def test_japanese_native_units_map_to_cues_without_whitespace_assumptions():
    cues = (
        MediaCue("a", 0, 1800, "日本語です。"),
        MediaCue("b", 2000, 3400, "学びます。"),
    )
    words = [
        {"word": w, "start": start, "end": start + 0.3}
        for w, start in [
            ("日", 0.1),
            ("本", 0.4),
            ("語", 0.7),
            ("です", 1),
            ("学", 2.1),
            ("びます", 2.4),
        ]
    ]
    mapped = map_ctc_words_to_cues(
        cues,
        words,
        clip_start_ms=0,
        padding_ms=1000,
        vad_spans=(),
        vad_enabled=False,
        min_confidence=0.5,
        duration_ms=4000,
        qwen_units=True,
    )
    assert [cue.text for cue in mapped] == [cue.text for cue in cues]
    assert all(cue.timing_source == "qwen3_alignment" for cue in mapped)
    assert [len(cue.words) for cue in mapped] == [4, 2]
    assert "".join(word.text for word in mapped[0].words) == cues[0].text


def test_units_crossing_caption_boundary_are_not_interpolated():
    from pandrator.logic.dubbing.caption_alignment import CaptionAlignmentError

    cues = (MediaCue("a", 0, 1000, "日本"), MediaCue("b", 1000, 2000, "語です"))
    with pytest.raises(CaptionAlignmentError):
        map_ctc_words_to_cues(
            cues,
            [{"word": "日本語です", "start": 0, "end": 2}],
            clip_start_ms=0,
            padding_ms=1000,
            vad_spans=(),
            vad_enabled=False,
            min_confidence=0.5,
            qwen_units=True,
        )


def test_native_batch_uses_one_unicode_json_sequence_and_isolates_bad_output(
    tmp_path, monkeypatch
):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    monkeypatch.setattr(qwen, "ensure_model", lambda *args: model)
    monkeypatch.setattr(
        qwen, "resolve_executable", lambda settings: tmp_path / "audiocpp_cli"
    )
    audio = wav(tmp_path / "音声.wav")
    text = tmp_path / "字幕.txt"
    text.write_text("日本語です。", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        assert "--text" not in command
        sequence = json.loads(
            Path(command[command.index("--request-sequence") + 1]).read_text()
        )
        assert len(sequence["requests"]) == 2
        assert all(row["language"] == "Japanese" for row in sequence["requests"])
        base = Path(command[command.index("--words-out") + 1])
        for index in range(2):
            payload = [
                {
                    "word": "日本語です" if index == 0 else "wrong",
                    "start_sample": 0,
                    "end_sample": 16000,
                }
            ]
            (base.parent / f"words_request_{index}.json").write_text(
                json.dumps(payload)
            )
        return subprocess.CompletedProcess(command, 0)

    output_a, output_b = tmp_path / "a.json", tmp_path / "b.json"
    result = qwen.run_batch(
        [(audio, text, output_a), (audio, text, output_b)],
        {"source_language": "ja"},
        run_func=runner,
    )
    assert len(calls) == 1
    assert output_a.is_file() and not output_b.exists()
    assert isinstance(result[1], qwen.QwenAlignmentError)


def test_long_or_unsupported_input_does_not_download_models(tmp_path, monkeypatch):
    ensure = Mock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(qwen, "ensure_model", ensure)
    audio = wav(tmp_path / "long.wav", 301)
    text = tmp_path / "words.txt"
    text.write_text("hello")
    with pytest.raises(qwen.QwenAlignmentError, match="300 seconds"):
        qwen.run_alignment(
            audio, text, tmp_path / "out.json", {"source_language": "en"}
        )
    with pytest.raises(qwen.QwenAlignmentError, match="unsupported_qwen"):
        qwen.run_alignment(
            audio, text, tmp_path / "out.json", {"source_language": "pl"}
        )
    ensure.assert_not_called()


def test_cancellation_prevents_download_and_execution(tmp_path):
    event = threading.Event()
    event.set()
    with pytest.raises(ProcessCancelled):
        qwen.ensure_model({}, event)
    with pytest.raises(ProcessCancelled):
        qwen.run_batch(
            [(tmp_path / "a.wav", tmp_path / "t.txt", tmp_path / "o.json")],
            {},
            cancel_event=event,
        )


def test_cache_download_checks_digest_and_reuses_verified_file(tmp_path, monkeypatch):
    data = b"GGUF-test-model"
    monkeypatch.setattr(qwen, "MODEL_SIZE", len(data))
    monkeypatch.setattr(qwen, "MODEL_SHA256", hashlib.sha256(data).hexdigest())
    qwen._VERIFIED.clear()
    opener = Mock(side_effect=lambda *args, **kwargs: io.BytesIO(data))
    settings = {"qwen_aligner_cache_dir": str(tmp_path)}
    model = qwen.ensure_model(settings, opener=opener)
    assert model.read_bytes() == data
    assert qwen.ensure_model(settings, opener=opener) == model
    assert opener.call_count == 1


def test_partial_or_corrupt_download_never_replaces_cached_model(tmp_path, monkeypatch):
    monkeypatch.setattr(qwen, "MODEL_SIZE", 30)
    monkeypatch.setattr(qwen, "MODEL_SHA256", "0" * 64)
    settings = {"qwen_aligner_cache_dir": str(tmp_path)}
    with pytest.raises(qwen.QwenAlignmentError, match="verification"):
        qwen.ensure_model(
            settings, opener=lambda *args, **kwargs: io.BytesIO(b"partial")
        )
    assert not qwen.cache_path(settings).exists()
    assert not list(tmp_path.glob(".qwen-download-*"))


def test_caption_native_batches_are_bounded_and_coverage_does_not_exceed_one(
    tmp_path, monkeypatch
):
    audio = wav(tmp_path / "source.wav", 40)
    cues = tuple(
        MediaCue(str(i), i * 2000, i * 2000 + 1000, "日本語です。") for i in range(17)
    )
    calls = []

    def batch_runner(requests, settings, **kwargs):
        calls.append(len(requests))
        assert len(requests) <= 8
        outputs = []
        for _audio, transcript, _output in requests:
            count = transcript.read_text().count("日本語です。")
            outputs.append(
                [
                    {"word": "日本語です", "start": j * 2 + 0.1, "end": j * 2 + 0.9}
                    for j in range(count)
                ]
            )
        return outputs

    monkeypatch.setattr(qwen, "run_batch", batch_runner)
    result = align_caption_cues(
        audio, cues, {"source_language": "ja", "caption_alignment_padding_ms": 250}
    )
    assert calls == [8, 8, 1]
    assert result.diagnostics.model_session_count == 3
    assert result.diagnostics.ctc_engine == "audio.cpp"
    assert result.diagnostics.method == "qwen3_forced_alignment"
    assert 0 <= result.diagnostics.alignment_coverage <= 1


def test_missing_runtime_preserves_caption_timing_with_diagnostic(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        qwen,
        "run_batch",
        Mock(side_effect=qwen.QwenAlignmentError("audio.cpp missing")),
    )
    cue = MediaCue("a", 1000, 3000, "日本語です。")
    result = align_caption_cues(wav(tmp_path / "a.wav"), (cue,), {"stt_language": "ja"})
    assert result.cues[0].timing_source == "caption"
    assert (result.cues[0].start_ms, result.cues[0].end_ms) == (1000, 3000)
    assert "audio.cpp missing" in result.diagnostics.failed_cue_ids["a"]


def test_moss_qwen_failure_retains_native_turns(tmp_path, monkeypatch):
    audio = wav(tmp_path / "source.wav")
    metadata = tmp_path / "moss.json"
    payload = {
        "transcription": [
            {
                "text": "日本語です。",
                "offsets": {"from": 1000, "to": 3000},
                "speaker": "A",
            }
        ]
    }
    metadata.write_text(json.dumps(payload))
    monkeypatch.setattr(
        qwen, "run_batch", Mock(side_effect=qwen.QwenAlignmentError("model missing"))
    )
    _align_moss_segments(
        audio,
        metadata,
        {"stt_language": "ja"},
        executable="unused",
        run_func=subprocess.run,
    )
    updated = json.loads(metadata.read_text())
    assert updated["transcription"][0]["text"] == "日本語です。"
    assert updated["transcription"][0]["offsets"] == {"from": 1000, "to": 3000}
    assert updated["pandrator_forced_alignment"]["failed_turns"] == 1


def test_moss_qwen_words_keep_speaker_and_absolute_sample_offsets(
    tmp_path, monkeypatch
):
    audio = wav(tmp_path / "source.wav")
    payload = {
        "transcription": [
            {
                "text": "日本語です。",
                "offsets": {"from": 1000, "to": 3000},
                "speaker": "A",
            }
        ]
    }
    monkeypatch.setattr(
        qwen,
        "run_batch",
        lambda *args, **kwargs: [[{"word": "日本語です", "start": 0.5, "end": 2.0}]],
    )
    qwen.align_moss_turns(
        audio, payload, {"stt_language": "ja", "moss_ctc_padding_seconds": 0.5}
    )
    word = payload["transcription"][0]["words"][0]
    assert word["text"] == "日本語です。"
    assert word["offsets"] == {"from": 1000, "to": 2500}
    assert word["speaker"] == "A"


def test_mcp_explicit_qwen_choice_is_accepted():
    from pandrator_mcp.schemas.media_edit_workflow import PlanMediaEditWorkflowInput

    value = PlanMediaEditWorkflowInput(
        session_id="test",
        instructions="Inspect cuts",
        caption_alignment_ctc_model=qwen.MODEL_ID,
    )
    assert value.caption_alignment_ctc_model == qwen.MODEL_ID


def test_pre_aligned_japanese_units_are_reused_by_media_edit():
    from pandrator.web.media_edit import MediaEditService

    text = "日本語です。"
    words = tuple(
        SimpleNamespace(
            text=value,
            start_ms=100 + index * 300,
            end_ms=400 + index * 300,
            confidence=0.75,
        )
        for index, value in enumerate(["日", "本", "語", "です。"])
    )
    segment = SimpleNamespace(
        identifier="ja",
        text=text,
        start_ms=100,
        end_ms=1300,
        words=words,
        metadata={"timing_source": "qwen3_alignment", "timing_confidence": 0.75},
    )
    original = MediaCue("ja", 0, 1500, text)
    result = MediaEditService._consume_pre_aligned_cues(
        [original], SimpleNamespace(segments=(segment,))
    )
    assert result is not None
    assert result[0].timing_source == "qwen3_alignment"
    assert len(result[0].words) == 4
    assert "".join(word.text for word in result[0].words) == text
    assert MediaEditService._token_coverage(list(result)) == 1.0


def test_capability_discovery_does_not_download_or_hash_models(tmp_path, monkeypatch):
    monkeypatch.setattr(
        qwen, "resolve_executable", lambda settings: tmp_path / "audiocpp_cli"
    )
    monkeypatch.setattr(
        qwen, "ensure_model", Mock(side_effect=AssertionError("read-only discovery"))
    )
    result = qwen.capabilities({"qwen_aligner_cache_dir": str(tmp_path)})
    assert result["available"]
    assert result["download_on_demand"]
    assert "ja" in result["supported_languages"]
    assert result["algorithm"] == "non_autoregressive"


@pytest.mark.parametrize(
    "key",
    ["qwen_aligner_executable", "qwen_aligner_cache_dir", "qwen_aligner_model_path"],
)
def test_mcp_cannot_override_local_aligner_paths_or_executable(key):
    from pandrator_mcp.schemas.sessions import _guard_settings

    with pytest.raises(ValueError, match="cannot contain paths"):
        _guard_settings({key: "/arbitrary/operator-only/path"})
