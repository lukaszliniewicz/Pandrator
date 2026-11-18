"""Late-cue measures shared by timing repair and drift reporting."""

from __future__ import annotations

import pytest

from pandrator.logic.dubbing.timing_drift import (
    PlacedSpeechBlock,
    block_lag_metrics,
    drift_warnings,
    late_cue_metrics,
)


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


def _detail(block: str, before: object, after: object = 0, **extra: object) -> dict:
    return {
        "block": block,
        "drift_before_ms": before,
        "drift_after_ms": after,
        **extra,
    }


def test_block_lag_metrics_names_the_worst_block_and_applies_strict_thresholds():
    metrics = block_lag_metrics(
        [
            _detail("0001", 0, 0),
            _detail("0002", 1_000, 0),
            _detail("0003", 3_000, 500),
            _detail("0004", 10_000, 900),
            _detail("0005", 14_500, 4_200),
        ]
    )
    assert metrics["worst_block_lag_ms"] == 14_500
    assert metrics["worst_block"] == "0005"
    # Exactly 1 s, 3 s and 10 s late are not "more than" those limits.
    assert metrics["blocks_late_over_1s"] == 3
    assert metrics["blocks_late_over_3s"] == 2
    assert metrics["blocks_late_over_10s"] == 1
    assert metrics["final_drift_ms"] == 4_200
    assert metrics["blocks"] == 5


def test_block_lag_metrics_counts_a_block_just_past_each_threshold():
    metrics = block_lag_metrics(
        [
            _detail("0001", 1_001),
            _detail("0002", 3_001),
            _detail("0003", 10_001),
        ]
    )
    assert metrics["blocks_late_over_1s"] == 3
    assert metrics["blocks_late_over_3s"] == 2
    assert metrics["blocks_late_over_10s"] == 1


def test_block_lag_metrics_without_late_cue_keys_reports_none():
    metrics = block_lag_metrics([_detail("0001", 4_000), _detail("0002", 0)])
    assert metrics["late_cues"] is None
    assert metrics["worst_cue_lag_ms"] is None
    assert metrics["cues"] is None


def test_block_lag_metrics_sums_and_maxes_late_cues():
    metrics = block_lag_metrics(
        [
            _detail("0001", 0, late_cue_count=2, worst_cue_lag_ms=1_500, cue_count=6),
            _detail("0002", 500, late_cue_count=1, worst_cue_lag_ms=5_700, cue_count=4),
        ]
    )
    assert metrics["late_cues"] == 3
    assert metrics["worst_cue_lag_ms"] == 5_700
    assert metrics["cues"] == 10


def test_block_lag_metrics_reads_missing_or_nonsense_values_as_zero():
    metrics = block_lag_metrics(
        [
            _detail("0001", None),
            {"block": "0002"},
            _detail("0003", "not a number", "nonsense"),
        ]
    )
    assert metrics["worst_block_lag_ms"] == 0
    assert metrics["worst_block"] is None
    assert metrics["final_drift_ms"] == 0
    assert metrics["blocks"] == 3


def test_block_lag_metrics_on_empty_input():
    metrics = block_lag_metrics([])
    assert metrics == {
        "worst_block_lag_ms": 0,
        "worst_block": None,
        "blocks_late_over_1s": 0,
        "blocks_late_over_3s": 0,
        "blocks_late_over_10s": 0,
        "final_drift_ms": 0,
        "blocks": 0,
        "late_cues": None,
        "worst_cue_lag_ms": None,
        "cues": None,
    }


def test_drift_warning_names_the_worst_block_and_the_speed():
    metrics = block_lag_metrics(
        [_detail("0001", 0)] + [_detail(f"{index:04d}", 20_000) for index in range(2, 4)]
    )
    warnings = drift_warnings(metrics, speed=130)
    assert warnings == [
        "Speech drifts behind the subtitles: the worst block starts 20.0 s late, "
        "and 2 of 3 blocks start more than 3 s late. Raising synchronization_speed "
        "(now 130) helps; above 1.2 speech may sound rushed, so for drift this "
        "large, condensing the text is the better fix."
    ]


def _blocks_around(first_lag: int) -> list[dict]:
    """One late block among twenty-one, so the late-block share stays under 5%."""

    return [_detail("0001", first_lag)] + [
        _detail(f"{index:04d}", 0) for index in range(2, 22)
    ]


def test_drift_warning_is_silent_at_the_ten_second_edge():
    at_edge = block_lag_metrics(_blocks_around(10_000))
    assert at_edge["blocks_late_over_10s"] == 0
    assert drift_warnings(at_edge, speed=1.15) == []
    just_past = block_lag_metrics(_blocks_around(10_001))
    assert len(drift_warnings(just_past, speed=1.15)) == 1


def test_drift_warning_is_silent_just_under_the_late_block_share():
    under = block_lag_metrics(
        [_detail("0001", 3_500)]
        + [_detail(f"{index:04d}", 0) for index in range(2, 22)]
    )
    assert under["blocks_late_over_3s"] == 1
    assert drift_warnings(under, speed=1.15) == []
    # 1 of 20 blocks is exactly the 5% share, which warns.
    at_share = block_lag_metrics(
        [_detail("0001", 3_500)]
        + [_detail(f"{index:04d}", 0) for index in range(2, 21)]
    )
    assert len(drift_warnings(at_share, speed=1.15)) == 1


def test_late_cue_warning_fires_at_the_share_and_stays_silent_below_it():
    below = block_lag_metrics(
        [
            _detail("0001", 0, late_cue_count=1, worst_cue_lag_ms=5_700, cue_count=21),
            _detail("0002", 0, late_cue_count=0, worst_cue_lag_ms=0, cue_count=19),
        ]
    )
    assert drift_warnings(below, speed=1.15) == []
    at_share = block_lag_metrics(
        [
            _detail("0001", 0, late_cue_count=1, worst_cue_lag_ms=5_700, cue_count=10),
            _detail("0002", 0, late_cue_count=0, worst_cue_lag_ms=0, cue_count=10),
        ]
    )
    assert drift_warnings(at_share, speed=1.15) == [
        "1 of 20 subtitle cues appear after their speech has ended (worst 5.7 s), "
        "inside speech blocks that merge several cues. Timing repair "
        "(repair-timing) splits and regenerates such blocks."
    ]


def test_late_cue_warning_needs_a_known_cue_total():
    metrics = {
        "worst_block_lag_ms": 0,
        "blocks_late_over_3s": 0,
        "blocks": 2,
        "late_cues": 4,
        "worst_cue_lag_ms": 5_700,
        "cues": None,
    }
    assert drift_warnings(metrics, speed=1.15) == []
