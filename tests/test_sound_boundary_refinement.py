import numpy as np
import pytest

from pandrator.logic.sound_boundary_refinement import (
    BOUNDARY_REFINEMENT_VERSION,
    locate_sound_anchor,
    refine_sound_boundary,
)

RATE = 10_000


def frame(seconds: float) -> int:
    return round(seconds * RATE)


def voiced_clip(
    intervals: list[tuple[float, float]], *, duration: float = 2.0, level: float = 0.2
) -> np.ndarray:
    audio = np.zeros(frame(duration), dtype=np.float32)
    for start, end in intervals:
        audio[frame(start) : frame(end)] = level
    return audio


def refine(
    audio: np.ndarray,
    *,
    estimate: float,
    next_word: float = 2.0,
    words: list[tuple[float, float]],
    speech: list[tuple[float, float]] | None,
    **settings: object,
) -> dict[str, object]:
    return refine_sound_boundary(
        audio,
        sample_rate=RATE,
        estimated_end_frame=frame(estimate),
        next_word_start_frame=frame(next_word),
        word_intervals=[(frame(start), frame(end)) for start, end in words],
        speech_intervals=(
            None if speech is None else [(frame(start), frame(end)) for start, end in speech]
        ),
        **settings,
    )


def test_refinement_chooses_a_safe_gap_near_the_estimate_and_keeps_voiced_sides():
    audio = voiced_clip([(0.2, 0.8), (1.0, 1.3)])
    result = refine(
        audio,
        estimate=0.81,
        next_word=1.0,
        words=[(0.2, 0.8), (1.0, 1.3)],
        speech=[(0.2, 0.8), (1.0, 1.3)],
    )

    assert result["status"] == "ready"
    assert result["algorithm_version"] == BOUNDARY_REFINEMENT_VERSION
    assert frame(0.8) < result["chosen_frame"] < frame(1.0)
    assert frame(0.83) <= result["gap_start_frame"]
    assert result["gap_end_frame"] <= frame(0.97)
    center = (result["gap_start_frame"] + result["gap_end_frame"]) // 2
    assert abs(result["chosen_frame"] - center) <= frame(0.002)
    assert np.all(audio[frame(0.2) : frame(0.8)] == 0.2)
    assert np.all(audio[frame(1.0) : frame(1.3)] == 0.2)
    assert result["shift_frames"] == result["chosen_frame"] - frame(0.81)
    assert result["evidence"] == {"vad": True, "energy": True, "word_guard": True}


def test_equal_distance_gaps_choose_the_earlier_candidate():
    intervals = [(0.1, 0.37), (0.52, 0.5201), (0.6701, 0.9)]
    result = refine(
        voiced_clip(intervals),
        estimate=0.52,
        next_word=1.0,
        words=intervals,
        speech=intervals,
    )
    assert result["status"] == "ready"
    assert result["gap_start_frame"] == frame(0.4)
    assert result["gap_end_frame"] == frame(0.49)


def test_no_safe_gap_in_continuous_speech():
    audio = voiced_clip([(0.0, 2.0)])
    result = refine(
        audio, estimate=0.8, words=[(0.0, 2.0)], speech=[(0.0, 2.0)]
    )
    assert result["status"] == "needs_placement"
    assert result["reason"] == "no_safe_quiet_gap"
    assert result["chosen_frame"] is None


def test_missing_vad_remains_pending_even_when_energy_is_quiet():
    result = refine(
        np.zeros(frame(2.0), dtype=np.float32),
        estimate=0.5,
        words=[],
        speech=None,
    )
    assert result["status"] == "needs_placement"
    assert result["reason"] == "non_speech_evidence_unavailable"
    assert result["evidence"] == {"vad": False, "energy": True, "word_guard": False}


def test_empty_vad_result_is_available_evidence_and_word_interval_stays_guarded():
    result = refine(
        np.zeros(frame(1.0), dtype=np.float32),
        estimate=0.4,
        next_word=0.8,
        words=[(0.2, 0.4)],
        speech=[],
    )
    assert result["status"] == "ready"
    assert result["evidence"] == {"vad": True, "energy": True, "word_guard": True}
    assert result["chosen_frame"] > frame(0.43)


@pytest.mark.parametrize("level", [0.0032, 0.008])
def test_absolute_threshold_does_not_reclassify_background_or_whisper_as_silence(level):
    audio = np.full(frame(2.0), level, dtype=np.float32)
    before = audio.copy()
    result = refine(audio, estimate=0.5, words=[], speech=[])
    assert result["status"] == "needs_placement"
    assert result["reason"] == "no_safe_quiet_gap"
    assert result["settings"]["quiet_dbfs"] == -55.0
    np.testing.assert_array_equal(audio, before)


def test_low_zero_crossings_inside_vad_speech_are_not_candidates():
    audio = np.zeros(frame(1.0), dtype=np.float32)
    t = np.arange(frame(1.0), dtype=np.float32) / RATE
    audio[frame(0.2) : frame(0.8)] = 0.0005 * np.sin(2 * np.pi * 130 * t[frame(0.2) : frame(0.8)])
    result = refine(
        audio,
        estimate=0.5,
        next_word=0.8,
        words=[(0.2, 0.8)],
        speech=[(0.2, 0.8)],
    )
    assert result["status"] == "needs_placement"


def test_short_quiet_gap_is_not_accepted():
    audio = voiced_clip([(0.1, 0.5), (0.54, 0.9)])
    result = refine(
        audio,
        estimate=0.5,
        next_word=0.54,
        words=[(0.1, 0.5), (0.54, 0.9)],
        speech=[(0.1, 0.5), (0.54, 0.9)],
    )
    assert result["status"] == "needs_placement"
    assert result["reason"] == "no_safe_quiet_gap"


def test_after_word_stops_at_next_word_but_next_pause_can_use_a_later_gap():
    intervals = [(0.2, 0.55), (0.6, 0.9), (1.0, 1.2)]
    audio = voiced_clip(intervals)
    after_word = refine(
        audio,
        estimate=0.55,
        next_word=0.6,
        words=intervals,
        speech=intervals,
    )
    next_pause = refine(
        audio,
        estimate=0.55,
        next_word=0.6,
        words=intervals,
        speech=intervals,
        kind="next_pause",
    )
    assert after_word["status"] == "needs_placement"
    assert next_pause["status"] == "ready"
    assert next_pause["chosen_frame"] > frame(1.2)


def test_search_and_guard_clipping_keep_boundary_inside_clip():
    intervals = [(0.2, 0.3)]
    audio = voiced_clip(intervals, duration=1.0)
    result = refine(
        audio,
        estimate=0.0,
        next_word=0.2,
        words=intervals,
        speech=intervals,
    )
    assert result["status"] == "ready"
    assert 0 <= result["gap_start_frame"] < result["chosen_frame"] < result["gap_end_frame"] <= len(audio)


@pytest.mark.parametrize(
    "audio,rate,estimate,words,speech,settings",
    [
        (np.zeros((100, 2), dtype=np.float32), RATE, 0, [], [], {}),
        (np.zeros(100, dtype=np.int16), RATE, 0, [], [], {}),
        (np.full(100, np.nan, dtype=np.float32), RATE, 0, [], [], {}),
        (np.full(100, 1.1, dtype=np.float32), RATE, 0, [], [], {}),
        (np.zeros(60_001, dtype=np.float32), RATE, 0, [], [], {}),
        (np.zeros(100, dtype=np.float32), 7_999, 0, [], [], {}),
        (np.zeros(100, dtype=np.float32), RATE, -1, [], [], {}),
        (np.zeros(100, dtype=np.float32), RATE, 0, [(0, 101)], [], {}),
        (np.zeros(100, dtype=np.float32), RATE, 0, [(50, 60), (20, 30)], [], {}),
        (np.zeros(100, dtype=np.float32), RATE, 0, [], [(10, 10)], {}),
        (np.zeros(100, dtype=np.float32), RATE, 0, [], [], {"max_search_ms": 2001}),
        (np.zeros(100, dtype=np.float32), RATE, 0, [], [], {"kind": "other"}),
    ],
)
def test_invalid_audio_frames_intervals_and_settings_are_rejected(
    audio, rate, estimate, words, speech, settings
):
    with pytest.raises(ValueError):
        refine_sound_boundary(
            audio,
            sample_rate=rate,
            estimated_end_frame=estimate,
            next_word_start_frame=0,
            word_intervals=words,
            speech_intervals=speech,
            **settings,
        )


def test_anchor_uses_literal_occurrence_and_verifies_explicit_offsets():
    text = "echo echo"
    assert locate_sound_anchor(text, "echo", 2) == (5, 9)
    assert locate_sound_anchor(text, "echo", 2, start=5, end=9) == (5, 9)
    with pytest.raises(ValueError, match="match the resolved"):
        locate_sound_anchor(text, "echo", 2, start=0, end=4)
    with pytest.raises(ValueError, match="supplied together"):
        locate_sound_anchor(text, "echo", 2, start=5)


def test_anchor_preserves_combining_grapheme_and_rejects_partial_or_whitespace_quote():
    text = "x e\u0301 y e\u0301"
    assert locate_sound_anchor(text, "e\u0301", 2, start=7, end=9) == (7, 9)
    with pytest.raises(ValueError, match="complete Unicode"):
        locate_sound_anchor(text, "\u0301", 1)
    with pytest.raises(ValueError, match="non-whitespace"):
        locate_sound_anchor(text, " \t", 1)
    with pytest.raises(ValueError):
        locate_sound_anchor(text, "absent", 1)
    with pytest.raises(ValueError):
        locate_sound_anchor(text, "e\u0301", 3)
