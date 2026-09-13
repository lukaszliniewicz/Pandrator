from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pandrator.logic.dubbing.early_repair import (
    RepairBoundary,
    find_repair_boundary,
)


def _provenance(
    display_parts: list[str],
    *,
    speech_parts: list[str] | None = None,
    timings: list[tuple[int, int]] | None = None,
) -> tuple[str, str, dict[str, object]]:
    speech_parts = speech_parts or display_parts
    timings = timings or [(index * 2500, (index + 1) * 2500) for index in range(len(display_parts))]
    display_text = " ".join(display_parts)
    speech_text = " ".join(speech_parts)
    display_spans: list[list[int]] = []
    speech_spans: list[list[int]] = []
    cursor = 0
    for part in display_parts:
        display_spans.append([cursor, cursor + len(part)])
        cursor += len(part) + 1
    cursor = 0
    for part in speech_parts:
        speech_spans.append([cursor, cursor + len(part)])
        cursor += len(part) + 1
    source_cues = [
        {
            "reference": index + 1,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "display_text": display_parts[index],
            "speech_text": speech_parts[index],
            "display_spans": [display_spans[index]],
            "speech_spans": [speech_spans[index]],
        }
        for index, (start_ms, end_ms) in enumerate(timings)
    ]
    return display_text, speech_text, {"source_cues": source_cues}


def _two_cue_input(
    *,
    display_parts: list[str] | None = None,
    speech_parts: list[str] | None = None,
    timings: list[tuple[int, int]] | None = None,
    audio_duration_ms: int = 1500,
) -> tuple[str, str, dict[str, object], int]:
    display_parts = display_parts or [
        "The first sentence ends here.",
        "The second sentence has enough words.",
    ]
    text, spoken, provenance = _provenance(
        display_parts,
        speech_parts=speech_parts,
        timings=timings or [(0, 2000), (2200, 5000)],
    )
    return text, spoken, provenance, audio_duration_ms


def test_repair_boundary_is_frozen_and_has_expected_fields() -> None:
    result = RepairBoundary(1, 2, 3, 4, 5, 6)

    assert result == RepairBoundary(
        display_cursor=1,
        speech_cursor=2,
        start_ms=3,
        boundary_ms=4,
        end_ms=5,
        estimated_advance_ms=6,
    )
    with pytest.raises(FrozenInstanceError):
        result.boundary_ms = 7  # type: ignore[misc]


def test_finds_boundary_at_complete_cue_with_independent_cursors() -> None:
    text, spoken, provenance, duration = _two_cue_input(
        display_parts=[
            "Display first sentence ends here.",
            "Spoken and display second sentence has words.",
        ],
        speech_parts=[
            "Speech first sentence ends here.",
            "Speech second sentence has enough words.",
        ],
    )

    result = find_repair_boundary(text, spoken, provenance, duration)

    assert result is not None
    assert result.display_cursor == len("Display first sentence ends here.")
    assert result.speech_cursor == len("Speech first sentence ends here.")
    assert (result.start_ms, result.boundary_ms, result.end_ms) == (0, 2200, 5000)
    assert result.estimated_advance_ms == 1533


@pytest.mark.parametrize("duration", [0, -1])
def test_rejects_zero_or_negative_audio(duration: int) -> None:
    text, spoken, provenance, _ = _two_cue_input()

    assert find_repair_boundary(text, spoken, provenance, duration) is None


def test_incoming_delay_disables_repair() -> None:
    text, spoken, provenance, duration = _two_cue_input()

    assert find_repair_boundary(
        text, spoken, provenance, duration, incoming_delay_ms=1
    ) is None


def test_default_thresholds_match_explicit_defaults() -> None:
    text, spoken, provenance, duration = _two_cue_input()

    implicit = find_repair_boundary(text, spoken, provenance, duration)
    explicit = find_repair_boundary(
        text,
        spoken,
        provenance,
        duration,
        min_shortfall_ms=1000,
        min_shortfall_percent=20,
        min_advance_ms=1000,
        min_child_span_ms=1000,
    )

    assert explicit == implicit


def test_min_shortfall_ms_can_admit_a_shortfall_below_default() -> None:
    text, spoken, provenance, _ = _two_cue_input(
        timings=[(0, 1500), (3000, 4000)],
        audio_duration_ms=3200,
    )

    assert find_repair_boundary(text, spoken, provenance, 3200) is None
    assert find_repair_boundary(
        text,
        spoken,
        provenance,
        3200,
        min_shortfall_ms=800,
    ) is not None


def test_min_shortfall_percent_can_admit_a_ratio_below_default() -> None:
    text, spoken, provenance, _ = _two_cue_input(
        timings=[(0, 2000), (4000, 6000)],
        audio_duration_ms=5000,
    )

    assert find_repair_boundary(text, spoken, provenance, 5000) is None
    assert find_repair_boundary(
        text,
        spoken,
        provenance,
        5000,
        min_shortfall_percent=16,
    ) is not None


def test_min_advance_ms_can_admit_an_advance_below_default() -> None:
    text, spoken, provenance, _ = _two_cue_input(
        timings=[(0, 1000), (1000, 5000)],
    )

    assert find_repair_boundary(text, spoken, provenance, 1500) is None
    result = find_repair_boundary(
        text,
        spoken,
        provenance,
        1500,
        min_advance_ms=100,
    )

    assert result is not None
    assert result.estimated_advance_ms >= 100


def test_min_child_span_ms_can_admit_a_child_span_below_default() -> None:
    text, spoken, provenance, _ = _two_cue_input(
        timings=[(0, 2000), (4200, 5000)],
    )

    assert find_repair_boundary(text, spoken, provenance, 1500) is None
    assert find_repair_boundary(
        text,
        spoken,
        provenance,
        1500,
        min_child_span_ms=800,
    ) is not None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_shortfall_ms": True},
        {"min_shortfall_ms": 99},
        {"min_shortfall_ms": 60001},
        {"min_shortfall_percent": False},
        {"min_shortfall_percent": 0},
        {"min_shortfall_percent": 96},
        {"min_advance_ms": True},
        {"min_advance_ms": 99},
        {"min_advance_ms": 60001},
        {"min_child_span_ms": False},
        {"min_child_span_ms": 249},
        {"min_child_span_ms": 60001},
    ],
)
def test_invalid_threshold_arguments_fail_closed(kwargs: dict[str, object]) -> None:
    text, spoken, provenance, duration = _two_cue_input()

    assert find_repair_boundary(
        text,
        spoken,
        provenance,
        duration,
        **kwargs,
    ) is None


def test_permissive_thresholds_do_not_override_incoming_delay() -> None:
    text, spoken, provenance, duration = _two_cue_input()

    assert find_repair_boundary(
        text,
        spoken,
        provenance,
        duration,
        incoming_delay_ms=1,
        min_shortfall_ms=100,
        min_shortfall_percent=1,
        min_advance_ms=100,
        min_child_span_ms=250,
    ) is None


@pytest.mark.parametrize(
    ("duration", "start_delay"),
    [
        (3000, 1000),  # exactly 1000 ms and exactly 20% shortfall
        (3501, 1000),  # one millisecond below the absolute threshold
    ],
)
def test_shortfall_requires_both_absolute_and_ratio_thresholds(
    duration: int, start_delay: int
) -> None:
    text, spoken, provenance, _ = _two_cue_input(
        timings=[(0, 3000), (4000, 5000)],
    )

    result = find_repair_boundary(
        text,
        spoken,
        provenance,
        duration,
        start_delay_ms=start_delay,
    )

    if duration == 3000:
        assert result is not None
    else:
        assert result is None


def test_shortfall_rejects_when_ratio_is_below_twenty_percent() -> None:
    text, spoken, provenance, _ = _two_cue_input(
        timings=[(0, 3000), (4000, 5000)],
    )

    assert find_repair_boundary(text, spoken, provenance, 4100) is None


def test_delay_can_make_an_otherwise_useful_anchor_unusable() -> None:
    text, spoken, provenance, _ = _two_cue_input()

    assert find_repair_boundary(text, spoken, provenance, 1500, start_delay_ms=1300) is None


def test_requires_useful_internal_anchor_and_child_durations() -> None:
    text, spoken, provenance, _ = _two_cue_input(
        display_parts=[
            "The first sentence ends here.",
            "The middle sentence ends here.",
            "The final sentence has enough words.",
        ],
        timings=[(0, 1100), (1200, 1800), (1900, 5000)],
        audio_duration_ms=1500,
    )

    assert find_repair_boundary(text, spoken, provenance, 1500) is None


@pytest.mark.parametrize(
    "punctuation",
    [".", "!", "?", ";", ":", ",", "。", "！", "？", "；", "：", "，", "—"],
)
def test_accepts_sentence_clause_and_unicode_punctuation(punctuation: str) -> None:
    text, spoken, provenance, duration = _two_cue_input(
        display_parts=[
            f"The first cue ends with punctuation{punctuation}",
            "The second cue contains enough words.",
        ],
    )

    assert find_repair_boundary(text, spoken, provenance, duration) is not None


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("The number is 3.", "14 items follow in this cue."),
        ("Dr.", "Smith is speaking in this cue."),
        ("For example e.g.", "an example follows in this cue."),
    ],
)
def test_does_not_treat_decimal_or_common_abbreviation_as_boundary(
    left: str, right: str
) -> None:
    text, spoken, provenance, duration = _two_cue_input(display_parts=[left, right])

    assert find_repair_boundary(text, spoken, provenance, duration) is None


def test_rejects_overlapping_cue_timings_as_a_whole_block() -> None:
    text, spoken, provenance, duration = _two_cue_input(
        timings=[(0, 2500), (2000, 5000)],
    )

    assert find_repair_boundary(text, spoken, provenance, duration) is None


def test_rejects_stale_optimized_text_mapping() -> None:
    text, spoken, provenance, duration = _two_cue_input(
        speech_parts=[
            "The first spoken sentence ends here.",
            "The second spoken sentence has enough words.",
        ],
    )
    provenance["source_cues"][0]["speech_text"] = "An old optimized sentence."

    assert find_repair_boundary(text, spoken, provenance, duration) is None


def test_accepts_differing_text_layer_lengths_and_unicode_offsets() -> None:
    text, spoken, provenance, duration = _two_cue_input(
        display_parts=["Unicode café cue ends here.", "The next display cue has words."],
        speech_parts=["The first spoken cue ends here.", "Next spoken cue has enough words."],
    )

    result = find_repair_boundary(text, spoken, provenance, duration)

    assert result is not None
    assert result.display_cursor == len("Unicode café cue ends here.")
    assert result.speech_cursor == len("The first spoken cue ends here.")


def test_speech_text_falls_back_to_display_text_when_empty() -> None:
    text, spoken, provenance, duration = _two_cue_input()
    for cue in provenance["source_cues"]:
        cue["speech_text"] = ""

    assert find_repair_boundary(text, spoken, provenance, duration) is not None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda cue: cue.update({"display_spans": []}),
        lambda cue: cue.update({"speech_spans": [[0, 1], [1, 2]]}),
        lambda cue: cue.update({"display_spans": [[-1, 5]]}),
        lambda cue: cue.update({"speech_spans": [[0.0, 5]]}),
        lambda cue: cue.update({"display_spans": [[0, 9999]]}),
    ],
)
def test_rejects_malformed_spans(mutate) -> None:
    text, spoken, provenance, duration = _two_cue_input()
    mutate(provenance["source_cues"][0])

    assert find_repair_boundary(text, spoken, provenance, duration) is None


def test_rejects_non_whitespace_gaps_and_uncovered_text() -> None:
    text, spoken, provenance, duration = _two_cue_input()
    provenance["source_cues"][1]["display_spans"] = [
        [provenance["source_cues"][0]["display_spans"][0][1] + 2, len(text)]
    ]

    assert find_repair_boundary(text, spoken, provenance, duration) is None


def test_rejects_single_cue() -> None:
    text, spoken, provenance, duration = _two_cue_input()
    provenance["source_cues"] = provenance["source_cues"][:1]

    assert find_repair_boundary(text, spoken, provenance, duration) is None


def test_selects_strongest_anchor_and_prefers_earlier_boundary_on_tie() -> None:
    text, spoken, provenance, duration = _two_cue_input(
        display_parts=[
            "The first cue ends here.",
            "The second cue ends here.",
            "The third cue has enough words.",
        ],
        timings=[(0, 2000), (2200, 3500), (3600, 6000)],
        audio_duration_ms=1800,
    )

    result = find_repair_boundary(text, spoken, provenance, duration)

    assert result is not None
    assert result.boundary_ms == 3600
    assert result.display_cursor == len("The first cue ends here. The second cue ends here.")
