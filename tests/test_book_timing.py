"""Fixture-only book alignment checks; models and providers are never invoked."""

import json
import threading
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest

from pandrator.logic import book_timing
from pandrator.logic.book_timing import (
    AlignedBookText,
    BookWord,
    align_book_text,
    alignment_identity,
    map_original_text,
)
from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing.caption_alignment import CaptionAlignmentError


def wav(path: Path, seconds=3, channels=1, rate=16000):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * int(rate * seconds) * channels)
    return path


@pytest.fixture
def alignment_setup(tmp_path, monkeypatch):
    source = wav(tmp_path / "source.wav", channels=2, rate=22050)
    normalized_commands = []

    def normalize(command, **kwargs):
        assert kwargs["cancel_event"] is event
        assert kwargs["check"] and kwargs["capture_output"]
        assert command[command.index("-i") + 1] == str(source)
        assert command[command.index("-ac") + 1] == "1"
        assert command[command.index("-ar") + 1] == "16000"
        assert command[command.index("-c:a") + 1] == "pcm_s16le"
        normalized_commands.append(command)
        wav(Path(command[-1]))

    event = threading.Event()
    monkeypatch.setattr(book_timing, "run_cancellable", normalize)
    return source, tmp_path / "scratch", event, normalized_commands


def aligned(text, payload):
    return AlignedBookText(
        text,
        book_timing._restore_words(text, payload),
        "fixture",
        {
            "duration_ms": 10000,
        },
    )


def payload(*surfaces):
    return [
        {"word": surface, "start": index + 0.1, "end": index + 0.8}
        for index, surface in enumerate(surfaces)
    ]


def test_normalization_and_runner_preserve_exact_source(alignment_setup):
    source, work, event, commands = alignment_setup
    before = source.read_bytes()
    text = '  "Héllo," world!  '
    runner = Mock(return_value=payload("héllo", "world"))
    result = align_book_text(source, text, {"language": "English"}, work, event, runner=runner)
    assert source.read_bytes() == before
    assert len(commands) == 1
    args, kwargs = runner.call_args
    assert args[0] == work / "book-alignment.wav"
    assert args[1].read_text(encoding="utf-8") == text
    assert args[3]["original_language"] == "en"
    assert kwargs["cancel_event"] is event
    assert result.text == text
    assert [word.text for word in result.words] == ['"Héllo,"', "world!"]
    assert [(word.start_char, word.end_char) for word in result.words] == [(2, 10), (11, 17)]
    assert result.engine == "crispasr"
    assert result.diagnostics["duration_ms"] == 3000


@pytest.mark.parametrize(
    "engine,language,model,expected",
    [
        ("auto", "ja", "auto", "qwen"),
        ("qwen", "en", "auto", "qwen"),
        ("crispasr", "en", "qwen3-forced-aligner", "crispasr"),
        ("auto", "en", "canary-ctc-aligner", "crispasr"),
    ],
)
def test_engine_choice_is_visible_to_injected_runner(
    alignment_setup, engine, language, model, expected
):
    source, work, event, _ = alignment_setup
    runner = Mock(return_value=payload("hello"))
    result = align_book_text(
        source,
        "hello",
        {
            "book_alignment_engine": engine,
            "stt_language": language,
            "caption_alignment_ctc_model": model,
        },
        work,
        event,
        runner=runner,
    )
    assert result.engine == expected
    options = runner.call_args.args[3]
    assert options["original_language"] == language
    if engine == "qwen":
        assert options["caption_alignment_ctc_model"] == "qwen3-forced-aligner"
    if engine == "crispasr":
        assert options["caption_alignment_ctc_model"] == "canary-ctc-aligner"


def test_default_runner_is_existing_align_only_contract(alignment_setup, monkeypatch):
    source, work, event, _ = alignment_setup
    runner = Mock(return_value=payload("hello"))
    monkeypatch.setattr(book_timing.crispasr, "run_ctc_alignment", runner)
    assert (
        align_book_text(source, "hello", {"language": "en"}, work, event).words[0].text == "hello"
    )
    runner.assert_called_once()


def test_preexisting_cancellation_skips_normalization(alignment_setup):
    source, work, event, commands = alignment_setup
    event.set()
    runner = Mock()
    with pytest.raises(ProcessCancelled):
        align_book_text(source, "hello", {}, work, event, runner=runner)
    assert not commands
    runner.assert_not_called()


def test_cancellation_during_normalization_never_starts_aligner(alignment_setup, monkeypatch):
    source, work, event, _ = alignment_setup

    def cancel(command, **kwargs):
        assert kwargs["cancel_event"] is event
        event.set()

    monkeypatch.setattr(book_timing, "run_cancellable", cancel)
    runner = Mock()
    with pytest.raises(ProcessCancelled):
        align_book_text(source, "hello", {}, work, event, runner=runner)
    runner.assert_not_called()


def test_cancellation_after_engine_result_is_propagated(alignment_setup):
    source, work, event, _ = alignment_setup

    def cancel(*args, **kwargs):
        event.set()
        return payload("hello")

    with pytest.raises(ProcessCancelled):
        align_book_text(source, "hello", {}, work, event, runner=cancel)


def test_language_guard_cannot_be_bypassed_by_test_runner(alignment_setup):
    source, work, event, commands = alignment_setup
    runner = Mock()
    with pytest.raises(ValueError, match="unsupported_qwen"):
        align_book_text(
            source,
            "hello",
            {"language": "pl", "book_alignment_engine": "qwen"},
            work,
            event,
            runner=runner,
        )
    assert not commands
    runner.assert_not_called()


def test_qwen_duration_guard_precedes_engine(alignment_setup, monkeypatch):
    source, work, event, _ = alignment_setup
    monkeypatch.setattr(
        book_timing, "run_cancellable", lambda command, **kwargs: wav(Path(command[-1]), 300.01)
    )
    runner = Mock()
    with pytest.raises(ValueError, match="300 seconds"):
        align_book_text(
            source,
            "hello",
            {"language": "en", "book_alignment_engine": "qwen"},
            work,
            event,
            runner=runner,
        )
    runner.assert_not_called()


def test_qwen_transcript_bound_precedes_normalization(alignment_setup):
    source, work, event, commands = alignment_setup
    runner = Mock()
    with pytest.raises(ValueError, match="16000"):
        align_book_text(
            source,
            "a" * 16001,
            {"language": "en", "book_alignment_engine": "qwen"},
            work,
            event,
            runner=runner,
        )
    assert not commands
    runner.assert_not_called()


@pytest.mark.parametrize(
    "words",
    [
        payload("hello"),
        payload("hello", "invented"),
        [{"word": "hello", "start": float("nan"), "end": 0.8}, *payload("world")],
        [{"word": "hello", "start": 0.1, "end": float("inf")}, *payload("world")],
        [{"word": "hello", "start": 0.1, "end": 0.1}, *payload("world")],
        [
            {"word": "hello", "start": 0.1, "end": 0.8},
            {"word": "world", "start": 1.1, "end": 3.001},
        ],
        [{"word": "hello", "start": 0.1, "end": 0.8}, {"word": "world", "start": 0, "end": 0.7}],
        [{"word": "hello", "start": 0.1, "end": 2}, {"word": "world", "start": 1, "end": 1.8}],
        [
            {"word": "hello", "start": 0.0001, "end": 0.0002},
            {"word": "world", "start": 1, "end": 1.8},
        ],
    ],
)
def test_incomplete_or_invalid_acoustic_evidence_is_rejected(alignment_setup, words):
    source, work, event, _ = alignment_setup
    with pytest.raises((ValueError, CaptionAlignmentError)):
        align_book_text(
            source,
            "hello world",
            {"language": "en"},
            work,
            event,
            runner=lambda *args, **kwargs: words,
        )


def test_cjk_and_decomposed_graphemes_have_exact_coverage():
    text = "「か\u3099くせい」は日本語。"
    result = aligned(text, payload("がくせい", "は", "日", "本", "語"))
    assert "".join(word.text for word in result.words) == text
    assert result.words[0].end_char == 7
    assert result.words[1].start_char == 7
    assert result.words[0].text == "「か\u3099くせい」"
    for word in result.words:
        assert text[word.start_char : word.end_char] == word.text


def test_expanding_nfkc_grapheme_cannot_be_split():
    with pytest.raises(ValueError, match="grapheme"):
        aligned("ﬃ", payload("f", "fi"))


def test_normalization_mapping_restores_original_without_offset_reuse():
    original = aligned("Cafe\u0301 ＦＦＩ says hello", payload("café", "ffi", "says", "hello"))
    display = "  Café, ﬃ says “hello!” "
    mapped = map_original_text(original, display)
    assert mapped is not None
    assert mapped.text == display
    assert [word.text for word in mapped.words] == ["Café,", "ﬃ", "says “", "hello!”"]
    assert [(word.start_ms, word.end_ms) for word in mapped.words] == [
        (word.start_ms, word.end_ms) for word in original.words
    ]
    for word in mapped.words:
        assert display[word.start_char : word.end_char] == word.text
    assert mapped.diagnostics["mapping_type"] == "normalization"
    assert map_original_text(original, original.text) is original


@pytest.mark.parametrize(
    "spoken,display,expected",
    [
        ("Hello Skrooj my friend", "Hello Scrooge my friend", "Scrooge"),
        ("Hello Scrooge my friend", "Hello Sk Rooj my friend", "Sk Rooj"),
        ("Hello Sk Rooj my friend", "Hello Scrooge my friend", "Scrooge"),
        ("Hello Jean Pawl my friend", "Hello John Paul my friend", "John Paul"),
    ],
)
def test_name_replacement_is_one_atomic_union(spoken, display, expected):
    original = aligned(spoken, payload(*spoken.split()))
    mapped = map_original_text(original, display)
    assert mapped is not None
    replacement = [word for word in mapped.words if word.text == expected]
    assert len(replacement) == 1
    assert replacement[0].start_ms == original.words[1].start_ms
    assert replacement[0].end_ms == original.words[-3].end_ms
    assert display[replacement[0].start_char : replacement[0].end_char] == expected
    assert mapped.diagnostics["replacement_count"] == 1


def test_single_token_full_replacement_is_atomic():
    original = aligned("Skrooj", payload("skrooj"))
    mapped = map_original_text(original, "Scrooge")
    assert mapped is not None
    assert mapped.words == (BookWord("Scrooge", 100, 800, 0, 7),)


@pytest.mark.parametrize(
    "spoken,display",
    [
        ("Hello my friend", "Hello good my friend"),
        ("Hello good my friend", "Hello my friend"),
        ("Hello my friend", "Farewell dear stranger"),
        ("a cat a", "a dog a"),
        ("one two three", "three two one"),
    ],
)
def test_structural_changes_and_ambiguous_anchors_rejected(spoken, display):
    assert map_original_text(aligned(spoken, payload(*spoken.split())), display) is None


def test_a_timed_unit_cannot_be_split_to_fit_replacement():
    source = aligned("Hello Skrooj friend", payload("HelloSkrooj", "friend"))
    assert map_original_text(source, "Hello Scrooge friend") is None


def test_json_roundtrip_and_detached_diagnostics():
    original = aligned("Hello, world!", payload("hello", "world"))
    values = original.as_dict()
    recovered = AlignedBookText.from_dict(json.loads(json.dumps(values)))
    assert recovered == original
    values["diagnostics"]["duration_ms"] = 1
    assert original.diagnostics["duration_ms"] == 10000
    assert BookWord.from_dict(original.words[0].as_dict()) == original.words[0]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["words"][0].update(start_ms=True),
        lambda value: value["words"][0].update(end_ms=0),
        lambda value: value["words"][0].update(end_char=500),
        lambda value: value["words"][0].update(text="different"),
        lambda value: value["words"][1].update(start_ms=0),
        lambda value: value["words"].pop(),
        lambda value: value["diagnostics"].update(duration_ms=1),
        lambda value: value["diagnostics"].update(score=float("nan")),
        lambda value: value.update(engine=""),
    ],
)
def test_json_validation_rejects_invalid_cached_evidence(mutation):
    values = aligned("hello world", payload("hello", "world")).as_dict()
    mutation(values)
    with pytest.raises(ValueError):
        AlignedBookText.from_dict(values)


def test_overlapping_intervals_rejected_and_touching_retained(alignment_setup):
    source, work, event, _ = alignment_setup
    evidence = [
        {"word": "hello", "start": 0.1, "end": 0.8},
        {"word": "world", "start": 0.8, "end": 1.8},
    ]
    result = align_book_text(
        source,
        "hello world",
        {"language": "en"},
        work,
        event,
        runner=lambda *args, **kwargs: evidence,
    )
    assert result.words[0].end_ms == result.words[1].start_ms == 800
    cached = result.as_dict()
    cached["words"][1]["start_ms"] = 799
    with pytest.raises(ValueError, match="overlap"):
        AlignedBookText.from_dict(cached)
    # Reject the native overlap even when rounding would conceal it in milliseconds.
    evidence[1]["start"] = 0.7999
    with pytest.raises(ValueError, match="nonmonotonic"):
        align_book_text(
            source,
            "hello world",
            {"language": "en"},
            work,
            event,
            runner=lambda *args, **kwargs: evidence,
        )


def test_cache_identity_whitelists_settings_and_fingerprints_files(tmp_path):
    executable = tmp_path / "crispasr"
    model = tmp_path / "custom.gguf"
    executable.write_bytes(b"binary")
    model.write_bytes(b"GGUF-model")
    settings = {
        "language": "English",
        "book_alignment_engine": "crispasr",
        "crispasr_executable": str(executable),
        "caption_alignment_ctc_model": str(model),
        "stt_threads": 3,
        "api_key": "hidden",
        "provider_configs": {"token": "hidden"},
    }
    identity = alignment_identity(settings, "hello")
    assert identity["language"] == "en"
    assert identity["model_file"]["size"] == 10
    assert identity["executable"]["size"] == 6
    assert "hidden" not in json.dumps(identity)
    assert "provider_configs" not in identity
    model.write_bytes(b"GGUF-new-long-model")
    assert alignment_identity(settings, "hello") != identity
    settings["api_key"] = "other secret"
    changed = alignment_identity(settings, "hello")
    settings["api_key"] = "yet another secret"
    assert alignment_identity(settings, "hello") == changed


def test_qwen_identity_records_pinned_revision(tmp_path):
    settings = {"language": "Japanese", "qwen_aligner_cache_dir": str(tmp_path)}
    identity = alignment_identity(settings, "日本")
    assert identity["engine"] == "qwen"
    assert identity["language"] == "ja"
    assert identity["model_revision"] == book_timing.qwen_alignment.MODEL_REVISION
    assert identity["model_sha256"] == book_timing.qwen_alignment.MODEL_SHA256
    assert identity["model_file"] is None


def test_custom_qwen_model_does_not_claim_the_bundled_pin(tmp_path):
    model = tmp_path / "custom-qwen.gguf"
    model.write_bytes(b"GGUF-custom")
    identity = alignment_identity({"language": "ja", "qwen_aligner_model_path": str(model)}, "日本")
    assert identity["model_revision"] == "custom"
    assert identity["model_sha256"] is None
    assert identity["model_file"]["path"] == str(model)
