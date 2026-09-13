from copy import deepcopy

import pytest

from pandrator.logic.dubbing.logical_passages import build_source_passages


def _cue(
    cue_id: str = "c1",
    ordinal: int = 0,
    text: str = "Alpha beta.",
    start_ms: int = 0,
    end_ms: int = 5000,
    speaker: str = "A",
) -> dict[str, object]:
    return {
        "id": cue_id,
        "ordinal": ordinal,
        "text": text,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "speaker": speaker,
    }


def _word(
    word_id: str,
    ordinal: int,
    text: str,
    start_ms: int,
    end_ms: int,
    *,
    segment_id: str | None = "c1",
    speaker: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "id": word_id,
        "ordinal": ordinal,
        "text": text,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }
    if segment_id is not None:
        row["segment_id"] = segment_id
    if speaker is not None:
        row["speaker"] = speaker
    return row


def test_punctuation_boundary_uses_matched_word_endpoints() -> None:
    cue = _cue(text="Hello world. Next phrase.")
    words = [
        _word("w0", 0, "Hello", 100, 200),
        _word("w1", 1, "world.", 220, 320),
        _word("w2", 2, "Next", 500, 600),
        _word("w3", 3, "phrase.", 620, 720),
    ]

    passages = build_source_passages([cue], words)

    assert [passage["text"] for passage in passages] == [
        "Hello world.",
        "Next phrase.",
    ]
    assert [passage["start_ms"] for passage in passages] == [100, 500]
    assert [passage["end_ms"] for passage in passages] == [320, 720]
    assert passages[0]["boundary_after"] == "sentence"
    assert all(passage["timing_basis"] == "word_boundaries" for passage in passages)


def test_unmatched_cue_text_is_conserved() -> None:
    cue = _cue(text="Hello brave new world.")
    words = [
        _word("w0", 0, "Hello", 100, 200),
        _word("w1", 1, "world.", 800, 900),
    ]

    passages = build_source_passages([cue], words)

    assert len(passages) == 1
    assert passages[0]["text"] == "Hello brave new world."
    assert passages[0]["source_word_ids"] == ["w0", "w1"]
    assert passages[0]["word_match_coverage"] == pytest.approx(2 / 4)


def test_temporal_fallback_recovers_words_with_an_ancestor_segment_id() -> None:
    cue = _cue(text="Alpha beta.", start_ms=1000, end_ms=2000)
    words = [
        _word("w0", 0, "Alpha", 1100, 1200, segment_id="ancestor"),
        _word("w1", 1, "beta.", 1300, 1400, segment_id="ancestor"),
    ]

    passages = build_source_passages([cue], words)

    assert passages[0]["source_word_ids"] == ["w0", "w1"]
    assert passages[0]["start_ms"] == 1100
    assert passages[0]["end_ms"] == 1400
    assert passages[0]["timing_basis"] == "word_boundaries"


def test_temporal_fallback_excludes_a_differently_labeled_speaker() -> None:
    cue = _cue(text="Alpha beta.", speaker="A", start_ms=1000, end_ms=2000)
    words = [
        _word("w0", 0, "Alpha", 1100, 1200, segment_id="other", speaker="B"),
        _word("w1", 1, "beta.", 1300, 1400, segment_id="other", speaker="B"),
    ]

    passages = build_source_passages([cue], words)

    assert passages == [
        {
            "id": "u0001-000-002",
            "text": "Alpha beta.",
            "speaker": "A",
            "start_ms": 1000,
            "end_ms": 2000,
            "source_cue_ids": ["c1"],
            "source_token_range": [0, 2],
            "source_word_ids": [],
            "word_match_coverage": 0.0,
            "timing_basis": "cue_window",
            "boundary_after": "cue",
            "source_unit_ids": ["u0001-000-002"],
        }
    ]


def test_empty_words_keep_the_complete_cue_window() -> None:
    cue = _cue(text="A naturally long source passage.", start_ms=100, end_ms=700)

    passages = build_source_passages([cue], [])

    assert len(passages) == 1
    assert passages[0]["text"] == cue["text"]
    assert passages[0]["start_ms"] == 100
    assert passages[0]["end_ms"] == 700
    assert passages[0]["timing_basis"] == "cue_window"


def test_zero_width_and_out_of_cue_word_endpoints_do_not_split_or_escape_bounds() -> (
    None
):
    cue = _cue(text="Alpha beta", start_ms=100, end_ms=500)
    words = [
        _word("w0", 0, "Alpha", 0, 0),
        _word("w1", 1, "beta", 450, 700),
    ]

    passages = build_source_passages([cue], words)

    assert len(passages) == 1
    assert passages[0]["source_word_ids"] == ["w0", "w1"]
    assert passages[0]["start_ms"] == 100
    assert passages[0]["end_ms"] == 500
    assert passages[0]["timing_basis"] == "cue_window"


def test_overlapping_candidate_windows_fall_back_to_one_nonoverlapping_cue_window() -> (
    None
):
    cue = _cue(text="Intro Alpha. Beta tail", start_ms=100, end_ms=1000)
    words = [
        _word("w0", 0, "Alpha.", 200, 600),
        _word("w1", 1, "Beta", 500, 700),
    ]

    passages = build_source_passages([cue], words)

    assert len(passages) == 1
    assert passages[0]["start_ms"] == 100
    assert passages[0]["end_ms"] == 1000
    assert passages[0]["timing_basis"] == "cue_window"


def test_output_is_stable_and_inputs_are_not_mutated() -> None:
    cues = [
        _cue("c2", 1, "Second.", 1000, 1500),
        _cue("c1", 0, "First.", 0, 500),
    ]
    words = [
        _word("w1", 1, "Second.", 1100, 1200, segment_id="c2"),
        _word("w0", 0, "First.", 100, 200, segment_id="c1"),
    ]
    cues_before = deepcopy(cues)
    words_before = deepcopy(words)

    first = build_source_passages(cues, words)
    second = build_source_passages(cues, words)

    assert first == second
    assert cues == cues_before
    assert words == words_before


def test_natural_long_cue_is_not_capped_or_split_without_trusted_boundaries() -> None:
    cue = _cue(
        text="This whole passage remains together for the source record.",
        end_ms=13000,
    )

    passages = build_source_passages([cue], [])

    assert len(passages) == 1
    assert passages[0]["start_ms"] == 0
    assert passages[0]["end_ms"] == 13000
    assert passages[0]["text"] == cue["text"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chars": True},
        {"max_span_ms": False},
        {"pause_ms": True},
    ],
)
def test_limits_reject_bool_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        build_source_passages([_cue()], [], **kwargs)
