import threading
import wave

import pytest

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing.caption_alignment import (
    CaptionAlignmentError,
    SpeechSpan,
    align_caption_cues,
    build_alignment_batches,
    build_overlap_clusters,
    normalize_alignment_settings,
    parse_ctc_words,
    parse_vad_export,
    validate_cue_words,
)
from pandrator.logic.media_edit import MediaCue


def _cue(cue_id, start_ms, end_ms, text="word", speaker=None):
    return MediaCue(cue_id, start_ms, end_ms, text, speaker=speaker)


def _normalized_wav(path, duration_seconds=12):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000 * duration_seconds)
    return path


def test_alignment_settings_enforce_the_timing_quality_safety_floor():
    normalized = normalize_alignment_settings(
        {
            "caption_alignment_min_confidence": 0.1,
            "caption_alignment_fallback_coverage": 0.1,
        }
    )

    assert normalized["caption_alignment_min_confidence"] == 0.5
    assert normalized["caption_alignment_fallback_coverage"] == 0.1


def test_overlap_clusters_are_strict_and_keep_original_order_and_outside_cues():
    cues = (
        _cue("late", 100, 300),
        _cue("first", 0, 200),
        _cue("touching", 300, 450),
        _cue("outside", 1000, 1200),
    )

    clusters, outside = build_overlap_clusters(cues, duration_ms=1000)

    assert [[cue.id for cue in cluster] for cluster in clusters] == [
        ["late", "first"],
        ["touching"],
    ]
    assert [cue.id for cue in outside] == ["outside"]


def test_first_pass_batches_bound_duration_tokens_and_indivisible_clusters():
    overlap = (
        _cue("a", 1000, 3000, "a"),
        _cue("b", 2000, 4000, "b"),
    )
    second = (_cue("c", 7000, 8000, "c"),)
    third = (_cue("d", 12000, 13000, "d"),)
    oversized = (_cue("over", 20000, 21000, " ".join(f"w{i}" for i in range(161))),)

    batches = build_alignment_batches(
        (overlap, second, third, oversized),
        duration_ms=30000,
        padding_ms=1000,
        batch_seconds=10,
    )

    assert [[cue.id for cue in batch.cues] for batch in batches] == [
        ["a", "b", "c"],
        ["d"],
    ]
    assert all(
        "over" not in {cue.id for cue in batch.cues} for batch in batches
    )
    assert all(
        batch.end_ms - batch.start_ms <= 10000 and batch.token_count <= 160
        for batch in batches
    )
    assert batches[0].end_ms - batches[0].start_ms <= 10000
    assert batches[0].token_count <= 160


def test_oversized_cluster_retries_only_bounded_individual_cues(tmp_path):
    normalized = _normalized_wav(tmp_path / "normalized.wav")
    first_tokens = " ".join(f"a{i}" for i in range(81))
    second_tokens = " ".join(f"b{i}" for i in range(81))
    cues = (
        _cue("first", 1000, 3000, first_tokens),
        _cue("second", 2000, 4000, second_tokens),
    )
    calls = []

    def runner(_clip, text_path, *_args, **_kwargs):
        tokens = text_path.read_text(encoding="utf-8").split()
        calls.append(tokens)
        return [
            {"word": token, "start": 1.0 + index * 0.01, "end": 1.005 + index * 0.01}
            for index, token in enumerate(tokens)
        ]

    result = align_caption_cues(normalized, cues, ctc_runner=runner)

    assert result.diagnostics.first_pass_batch_count == 0
    assert result.diagnostics.oversized_cluster_count == 1
    assert result.diagnostics.cluster_retries == 0
    assert result.diagnostics.individual_retries == 2
    assert len(calls) == 2
    assert all(len(tokens) == 81 for tokens in calls)
    assert result.diagnostics.accepted_cue_count == 2
    assert result.diagnostics.accepted_token_count == 162
    assert result.diagnostics.alignment_coverage == 1.0


def test_individual_cue_over_token_budget_is_rejected_without_ctc_invocation(tmp_path):
    normalized = _normalized_wav(tmp_path / "normalized.wav")
    cue = _cue("too-big", 1000, 3000, " ".join(f"w{i}" for i in range(161)))
    invoked = False

    def runner(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        return []

    result = align_caption_cues(normalized, (cue,), ctc_runner=runner)

    assert not invoked
    assert result.diagnostics.oversized_cue_count == 1
    assert set(result.diagnostics.failed_cue_ids) == {"too-big"}
    assert "cue_exceeds_ctc_limits" in result.diagnostics.failed_cue_ids["too-big"]
    assert result.diagnostics.accepted_cue_count == 0


def test_ctc_validation_enforces_surfaces_count_windows_spans_and_quality():
    cue = _cue("cue", 1000, 2000, "Hello world")
    valid = [
        {"word": "Hello", "start": 1.1, "end": 1.3},
        {"word": "world", "start": 1.5, "end": 1.8},
    ]

    accepted, reason, quality = validate_cue_words(
        cue,
        valid,
        padding_ms=200,
        vad_spans=(SpeechSpan(0, 2000),),
        vad_enabled=True,
        min_confidence=0.5,
        duration_ms=3000,
    )

    assert reason is None
    assert quality == 1.0
    assert accepted.timing_source == "ctc_alignment"
    assert [word.text for word in accepted.words] == ["Hello", "world"]
    assert (accepted.start_ms, accepted.end_ms) == (1100, 1800)
    assert all(word.confidence == 1.0 for word in accepted.words)

    cases = (
        (valid[:1], "wrong_count"),
        ([{"word": "other", "start": 1.1, "end": 1.3}, valid[1]], "wrong_surface"),
        ([{"word": "Hello", "start": 0.7, "end": 1.0}, valid[1]], "out_of_window"),
        (
            [
                {"word": "Hello", "start": 1.3, "end": 1.7},
                {"word": "world", "start": 1.5, "end": 1.6},
            ],
            "nonmonotonic",
        ),
    )
    for words, expected_reason in cases:
        _rejected, rejected_reason, rejected_quality = validate_cue_words(
            cue,
            words,
            padding_ms=200,
            vad_spans=(SpeechSpan(0, 2000),),
            vad_enabled=True,
            min_confidence=0.5,
            duration_ms=3000,
        )
        assert rejected_reason == expected_reason
        assert rejected_quality == 0.0

    long_cue = _cue("long", 1000, 4500, "Hello world")
    _rejected, rejected_reason, rejected_quality = validate_cue_words(
        long_cue,
        [{"word": "Hello", "start": 1.0, "end": 3.6}, valid[1]],
        padding_ms=500,
        vad_spans=(SpeechSpan(0, 5000),),
        vad_enabled=True,
        min_confidence=0.5,
        duration_ms=6000,
    )
    assert rejected_reason == "overlong_word"
    assert rejected_quality == 0.0


def test_ctc_vad_tolerance_support_and_no_vad_quality_basis():
    cue = _cue("cue", 1000, 2000, "one two")
    words = [
        {"word": "one", "start": 1.0, "end": 1.2},
        {"word": "two", "start": 1.6, "end": 1.8},
    ]

    _accepted, reason, quality = validate_cue_words(
        cue,
        words,
        padding_ms=200,
        vad_spans=(SpeechSpan(1220, 1400),),
        vad_enabled=True,
        min_confidence=0.5,
        duration_ms=3000,
    )
    assert reason is None
    assert quality == 0.5  # first midpoint is supported by the 120 ms tolerance

    _rejected, reason, quality = validate_cue_words(
        cue,
        words,
        padding_ms=200,
        vad_spans=(SpeechSpan(1300, 1400),),
        vad_enabled=True,
        min_confidence=0.5,
        duration_ms=3000,
    )
    assert reason == "inadequate_speech_support"
    assert quality == 0.0

    _accepted, reason, quality = validate_cue_words(
        cue,
        words,
        padding_ms=200,
        vad_enabled=False,
        min_confidence=0.5,
        duration_ms=3000,
    )
    assert reason is None
    assert quality == 0.75


def test_ctc_parser_requires_the_crispasr_word_contract():
    with pytest.raises(CaptionAlignmentError):
        parse_ctc_words([{"text": "hello", "start": 0.0, "end": 0.2}])


def test_align_caption_cues_recovers_poisoned_batch_by_cluster_and_cue_retry(tmp_path):
    normalized = _normalized_wav(tmp_path / "normalized.wav")
    cues = (
        _cue("alpha", 1000, 2000, "Alpha"),
        _cue("beta", 1500, 2500, "Beta"),
        _cue("gamma", 8000, 9000, "Gamma"),
    )
    calls = []

    def runner(_clip, text_path, _output, _settings, _event):
        calls.append(text_path.read_text(encoding="utf-8"))
        if len(calls) == 1:
            return [
                {"word": "poison", "start": 1.1, "end": 1.3},
                {"word": "Beta", "start": 1.5, "end": 1.7},
                {"word": "Gamma", "start": 8.1, "end": 8.3},
            ]
        if len(calls) == 2:
            return [
                {"word": "poison", "start": 1.1, "end": 1.3},
                {"word": "Beta", "start": 1.5, "end": 1.7},
            ]
        return [{"word": "Alpha", "start": 1.1, "end": 1.3}]

    result = align_caption_cues(
        normalized,
        cues,
        {"caption_alignment_batch_seconds": 30},
        ctc_runner=runner,
    )

    assert calls == ["Alpha Beta Gamma", "Alpha Beta", "Alpha"]
    assert result.diagnostics.first_pass_batch_count == 1
    assert result.diagnostics.overlap_cluster_count == 2
    assert result.diagnostics.cluster_retries == 1
    assert result.diagnostics.individual_retries == 1
    assert result.diagnostics.accepted_cue_count == 3
    assert result.diagnostics.accepted_token_count == 3
    assert result.diagnostics.alignment_coverage == 1.0
    assert result.diagnostics.failed_cue_ids == {}
    assert all(cue.timing_source == "ctc_alignment" for cue in result.cues)
    assert [word.text for cue in result.cues for word in cue.words] == [
        "Alpha",
        "Beta",
        "Gamma",
    ]


def test_align_caption_cues_pre_cancel_never_invokes_runner(tmp_path):
    normalized = _normalized_wav(tmp_path / "normalized.wav", duration_seconds=2)
    event = threading.Event()
    event.set()
    invoked = False

    def runner(*_args):
        nonlocal invoked
        invoked = True
        return []

    with pytest.raises(ProcessCancelled):
        align_caption_cues(
            normalized,
            (_cue("cue", 100, 500, "word"),),
            ctc_runner=runner,
            cancel_event=event,
        )
    assert not invoked


def test_parse_real_crispasr_vad_export_schema():
    payload = {
        "crispasr_vad": {
            "version": 1,
            "kind": "vad_segments",
            "sample_rate": 16000,
            "num_slices": 2,
            "slices": [
                {"start": 1600, "end": 4800, "t0_cs": 10, "t1_cs": 30},
                {"start": 6400, "end": 8000, "t0_cs": 40, "t1_cs": 50},
            ],
        }
    }

    spans = parse_vad_export(payload, duration_ms=1000)
    # The raw sample offsets are authoritative and are converted to absolute
    # half-open milliseconds.
    assert tuple((span.start_ms, span.end_ms) for span in spans) == (
        (100, 300),
        (400, 500),
    )


def test_parse_vad_export_rejects_unwrapped_schema():
    try:
        parse_vad_export(
            {
                "version": 1,
                "kind": "vad_segments",
                "sample_rate": 16000,
                "segments": [{"start": 0, "end": 1600}],
            },
            duration_ms=1000,
        )
    except CaptionAlignmentError:
        pass
    else:
        raise AssertionError("unwrapped VAD payload must not be accepted")
