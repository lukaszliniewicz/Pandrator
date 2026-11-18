"""Late-cue measures shared by timing repair and drift reporting."""

from __future__ import annotations

import pytest

from pandrator.logic.dubbing.timing_drift import PlacedSpeechBlock, late_cue_metrics


def test_cue_starting_after_the_speech_ends_is_late():
    blocks = [
        PlacedSpeechBlock(
            start_ms=0,
            end_ms=3000,
            cues=[(0, 2000), (5000, 8000)],
        )
    ]
    metrics = late_cue_metrics(blocks)
    assert metrics["late_cues"] == 1
    assert metrics["worst_lag_ms"] == 2000
    assert metrics["cues"] == 2
    assert metrics["blocks"] == 1


def test_cue_starting_at_the_speech_end_is_not_late():
    metrics = late_cue_metrics(
        [PlacedSpeechBlock(start_ms=1000, end_ms=4000, cues=[(1000, 2000), (4000, 6000)])]
    )
    assert metrics["late_cues"] == 0
    assert metrics["worst_lag_ms"] == 0


def test_worst_lag_and_counts_across_blocks():
    metrics = late_cue_metrics(
        [
            PlacedSpeechBlock(start_ms=0, end_ms=2000, cues=[(0, 1000), (2500, 3000)]),
            PlacedSpeechBlock(start_ms=3000, end_ms=9000, cues=[(3000, 4000), (6000, 7000)]),
            PlacedSpeechBlock(start_ms=9000, end_ms=9500, cues=[(12000, 13000)]),
        ]
    )
    assert metrics["late_cues"] == 2
    assert metrics["worst_lag_ms"] == 2500
    assert metrics["cues"] == 5
    assert metrics["blocks"] == 3


def test_empty_input_reports_zeroes():
    assert late_cue_metrics([]) == {
        "late_cues": 0,
        "worst_lag_ms": 0,
        "cues": 0,
        "blocks": 0,
    }


@pytest.mark.parametrize("cue_start", [-5, 0, 1])
def test_only_strictly_positive_lags_count(cue_start: int):
    metrics = late_cue_metrics([PlacedSpeechBlock(start_ms=0, end_ms=0, cues=[(cue_start, 10)])])
    assert metrics["late_cues"] == (1 if cue_start > 0 else 0)
