import pytest

from pandrator.logic.media_edit import (
    KeepRange,
    MediaCue,
    MediaWord,
    align_cues_to_words,
    caption_to_srt,
    keep_ranges_from_cuts,
    normalize_keep_ranges,
    parse_caption_text,
    refine_boundary,
    retime_cues,
)


def test_parse_vtt_sniffs_content_and_preserves_overlap_and_speakers():
    cues = parse_caption_text(
        """WEBVTT

00:00:01 --> 00:00:03
<v Alice>Hello
there</v>

second-cue
00:00:02.500 --> 00:00:04
Name: overlapping cue
"""
    )

    assert [cue.id for cue in cues] == ["cue-000001", "cue-000002"]
    assert cues[0].speaker == "Alice"
    assert cues[0].text == "Hello there"
    assert cues[1].speaker == "Name"
    assert cues[1].text == "overlapping cue"
    assert cues[0].end_ms > cues[1].start_ms


def test_parse_srt_accepts_comma_milliseconds_and_long_hours():
    cues = parse_caption_text(
        "1\n24:00:01,250 --> 24:00:02,500\nA sentence: with punctuation.\n"
    )

    assert cues[0].start_ms == 86_401_250
    assert cues[0].end_ms == 86_402_500
    assert cues[0].speaker is None
    assert cues[0].text == "A sentence: with punctuation."


def test_parse_repeated_lowercase_speaker_but_reject_one_off_lowercase_prose():
    cues = parse_caption_text(
        """1
00:00:01,000 --> 00:00:02,000
wytsk: First repeated utterance.

2
00:00:03,000 --> 00:00:04,000
WYTSK: Second repeated utterance.

3
00:00:05,000 --> 00:00:06,000
note: this is ordinary content.

4
00:00:07,000 --> 00:00:08,000
Name: One-off title-like label.
"""
    )

    assert [cue.speaker for cue in cues] == ["wytsk", "WYTSK", None, "Name"]
    assert [cue.text for cue in cues] == [
        "First repeated utterance.",
        "Second repeated utterance.",
        "note: this is ordinary content.",
        "One-off title-like label.",
    ]


def test_parse_unicode_speaker_label():
    cues = parse_caption_text(
        "1\n00:00:01,000 --> 00:00:02,000\nŁukasz Żółć: Dzień dobry.\n"
    )

    assert cues[0].speaker == "Łukasz Żółć"
    assert cues[0].text == "Dzień dobry."


def test_alignment_handles_offset_and_punctuation_differences():
    cue = MediaCue("caption", 0, 2_000, "Hello, world!")
    words = (
        MediaWord("HELLO", 3_500, 3_800),
        MediaWord("world", 3_900, 4_300),
    )

    aligned = align_cues_to_words((cue,), words)[0]

    assert (aligned.start_ms, aligned.end_ms) == (3_500, 4_300)
    assert aligned.words == words
    assert aligned.timing_source == "asr_alignment"
    assert aligned.timing_confidence == 1


def test_alignment_reuses_anchor_for_overlapping_cues():
    cues = (
        MediaCue("one", 0, 2_000, "hello world"),
        MediaCue("two", 1_000, 3_000, "world again"),
    )
    words = (
        MediaWord("hello", 3_000, 3_300),
        MediaWord("world", 3_400, 3_700),
        MediaWord("again", 3_800, 4_100),
    )

    aligned = align_cues_to_words(cues, words)

    assert aligned[1].start_ms == words[1].start_ms
    assert aligned[0].end_ms > aligned[1].start_ms


def test_alignment_rejects_sparse_or_distant_lexical_coincidence():
    sparse = MediaCue("sparse", 1_000, 2_000, "alpha missing words here")
    distant = MediaCue("distant", 3_000, 4_000, "target")
    words = (
        MediaWord("alpha", 10, 20),
        *(
            MediaWord(f"word-{index}", 30 + index * 10, 35 + index * 10)
            for index in range(450)
        ),
        MediaWord("target", 5_000, 5_100),
    )

    aligned_sparse, aligned_distant = align_cues_to_words((sparse, distant), words)

    assert aligned_sparse.timing_source == "caption"
    assert aligned_sparse.start_ms == sparse.start_ms
    assert aligned_sparse.timing_confidence == 0.25
    assert aligned_distant.timing_source == "caption"
    assert aligned_distant.start_ms == distant.start_ms


def test_keep_ranges_normalize_clamp_merge_and_label():
    ranges = normalize_keep_ranges(
        (
            KeepRange("a", 900, 1_500, "intro"),
            KeepRange("b", 1_450, 2_100, "body"),
            KeepRange("c", 2_150, 2_500, "tail"),
        ),
        2_300,
        merge_gap_ms=100,
    )

    assert ranges == (KeepRange("keep-000001", 900, 2_300, "intro / body / tail"),)


def test_keep_ranges_from_cuts_returns_complement():
    ranges = keep_ranges_from_cuts(((2_000, 3_000), (2_500, 4_000)), 10_000)

    assert [(item.start_ms, item.end_ms) for item in ranges] == [
        (0, 2_000),
        (4_000, 10_000),
    ]
    assert keep_ranges_from_cuts((), 1_000)[0].end_ms == 1_000
    assert [
        (item.start_ms, item.end_ms)
        for item in keep_ranges_from_cuts(((-10, 50),), 1_000)
    ] == [(50, 1_000)]


def test_retime_cues_splits_cue_and_words_across_kept_ranges():
    cue = MediaCue(
        "cue-1",
        0,
        4_000,
        "one two three",
        words=(
            MediaWord("one", 0, 1_000),
            MediaWord("two", 1_000, 2_000),
            MediaWord("three", 3_000, 4_000),
        ),
    )
    ranges = (
        KeepRange("left", 0, 1_500),
        KeepRange("right", 2_500, 4_000),
    )

    retimed = retime_cues((cue,), ranges)

    assert [(item.start_ms, item.end_ms) for item in retimed] == [
        (0, 1_500),
        (1_500, 3_000),
    ]
    assert [item.text for item in retimed] == ["one two", "three"]
    assert [word.text for word in retimed[0].words] == ["one", "two"]
    assert [word.text for word in retimed[1].words] == ["three"]
    assert [item.id for item in retimed] == ["cue-1-part-001", "cue-1-part-002"]


def test_boundary_refinement_uses_silence_and_never_cuts_inside_word():
    words = (MediaWord("first", 0, 100), MediaWord("second", 300, 400))

    start = refine_boundary(250, words, side="start")
    end = refine_boundary(250, words, side="end")
    inside = refine_boundary(350, words, side="start")
    clamped = refine_boundary(-50, words, side="start")

    assert 100 <= start.refined_ms <= 300
    assert 100 <= end.refined_ms <= 300
    assert start.method == "word_gap_start"
    assert end.method == "word_gap_end"
    assert inside.refined_ms == 300
    assert inside.warnings
    assert clamped.original_ms == 0


def test_caption_to_srt_keeps_native_speaker_out_of_text():
    cue = MediaCue("cue", 3_600_000, 3_601_250, "Hello", speaker="Alice")

    assert caption_to_srt((cue,)) == ("1\n01:00:00,000 --> 01:00:01,250\nHello\n")


def test_invalid_caption_and_keep_duration_are_rejected():
    with pytest.raises(ValueError):
        parse_caption_text("not a caption")
    with pytest.raises(ValueError):
        normalize_keep_ranges((KeepRange("x", 0, 1),), 0)
