"""Natural-speech-first splitting: shared helper, blocks, and repair."""

import pytest

from pandrator.logic.dubbing import speech_blocks
from pandrator.logic.dubbing.early_repair import find_repair_boundary
from pandrator.logic.dubbing.natural_boundaries import (
    boundary_strength_rank,
    classify_boundary,
    conjunction_split_positions,
    conjunction_tiers,
    natural_split_candidates,
    period_is_non_boundary,
    resolve_speech_language,
    starts_with_safe_conjunction,
)
from pandrator.logic.dubbing.speech_blocks import (
    UnsplittableSpeechBlockError,
    _break_cost,
    _split_further,
    create_speech_blocks,
)


def _srt(rows):
    lines = []
    for index, (start, end, text) in enumerate(rows, start=1):
        lines.extend([str(index), f"{start} --> {end}", text, ""])
    return "\n".join(lines)


# -- shared language handling -------------------------------------------------


def test_resolve_speech_language_covers_xtts_variants():
    assert resolve_speech_language("en-US") == "en"
    assert resolve_speech_language("pt-BR") == "pt"
    assert resolve_speech_language("zh") == "zh-cn"
    assert resolve_speech_language("zh-cn") == "zh-cn"
    assert resolve_speech_language("hi") == "hi"
    assert resolve_speech_language("xx") == "xx"
    # Long unknown codes fall back to the default instead of confusing bans.
    assert resolve_speech_language("mystery-lang") == "en"


def test_every_xtts_language_has_a_conjunction_table():
    from pandrator.constants import XTTS_LANGUAGES

    for code in XTTS_LANGUAGES:
        resolved = resolve_speech_language(code)
        assert resolved in speech_blocks.CONJUNCTIONS, code


def test_conjunction_positions_accept_language_code_keyword():
    text = "Elle reste ici parce que la nuit tombe vite"
    assert conjunction_split_positions(text, language_code="fr") == (
        conjunction_split_positions(text, "fr")
    )
    assert conjunction_split_positions(text, language_code="fr") == [15]


def test_blanket_coordinators_never_license_a_cut():
    assert conjunction_split_positions("I ordered fish and chips", "en") == []
    assert conjunction_split_positions("Tom und Jerry gehen heim", "de") == []
    assert conjunction_split_positions("This and that belong together", "en") == []


def test_conjunction_tiers_separate_authored_breath_marks_from_bare():
    assert (
        conjunction_tiers("The clause stands alone, because it continues", "en")[25]
        == "comma_led"
    )
    assert (
        conjunction_tiers(
            "The results were unclear although the data was complete", "en"
        )[25]
        == "bare"
    )
    assert (
        conjunction_tiers("Er kommt heute nicht, weil er krank ist", "de")[22]
        == "comma_led"
    )


def test_overlapping_conjunctions_keep_the_longest_match():
    assert conjunction_split_positions(
        "Elle reste parce que la nuit tombe vite maintenant", "fr"
    ) == [11]


def test_safe_conjunction_openings_and_boundary_ranks():
    assert starts_with_safe_conjunction(
        "Because the story continues", language_code="en"
    )
    assert starts_with_safe_conjunction("weil die Geschichte weitergeht", "de")
    assert not starts_with_safe_conjunction("And the story continues", "en")
    assert not starts_with_safe_conjunction("The story continues", "en")
    assert boundary_strength_rank(".") == 0
    assert boundary_strength_rank(";") == 1
    assert boundary_strength_rank(",") == 2
    assert boundary_strength_rank("x") is None
    assert classify_boundary("Ends here.", "Next", language_code="en") == 0
    assert classify_boundary("Pause here;", "Next", language_code="en") == 1
    assert classify_boundary("No terminal", "Next", language_code="en") is None
    assert (
        classify_boundary("No terminal", "because reasons follow", language_code="en")
        == 3
    )
    assert classify_boundary("No terminal", "and more follows", language_code="en") is (
        None
    )


def test_decimal_and_abbreviation_periods_are_not_boundaries():
    assert period_is_non_boundary("The number is 3.", "14 items follow")
    assert period_is_non_boundary("Dr.", "Smith is speaking")
    assert not period_is_non_boundary("The sentence ends.", "Next begins")
    candidates = natural_split_candidates("The value is 3.14 meters today", "en")
    assert all(kind != "sentence" for _, kind in candidates)
    candidates = natural_split_candidates("Dr. Smith arrived early today", "en")
    assert all(kind != "sentence" for _, kind in candidates)


# -- speech blocks -------------------------------------------------------------


def test_single_token_beyond_cap_raises_actionable_error():
    content = _srt([("00:00:00,000", "00:00:05,000", "a" * 300)])
    with pytest.raises(UnsplittableSpeechBlockError) as excinfo:
        create_speech_blocks(content, target_language="en", min_chars=10, max_chars=220)
    assert isinstance(excinfo.value, ValueError)
    assert "220" in str(excinfo.value)
    assert excinfo.value.details["subtitles"] == [1]
    assert excinfo.value.details["speech_length"] == 300


def test_unspaced_script_without_punctuation_raises_instead_of_char_cut():
    content = _srt([("00:00:00,000", "00:00:05,000", "汉" * 120)])
    with pytest.raises(UnsplittableSpeechBlockError):
        create_speech_blocks(
            content, target_language="zh-cn", min_chars=10, max_chars=100
        )


def test_cjk_punctuation_is_a_valid_split_point():
    sentence = "这是第一个分句，这是第二个分句，这是第三个分句，还有补充说明"
    content = _srt([("00:00:00,000", "00:00:05,000", sentence)])
    blocks = create_speech_blocks(
        content, target_language="zh-cn", min_chars=5, max_chars=20
    )
    assert len(blocks) > 1
    assert all(len(block["text"]) <= 20 for block in blocks)


def test_comma_led_conjunction_beats_plain_whitespace_balance():
    content = _srt(
        [
            (
                "00:00:00,000",
                "00:00:05,000",
                "We will meet at the end of the day, "
                "and the next session starts at dawn.",
            )
        ]
    )
    blocks = create_speech_blocks(
        content, target_language="en", min_chars=10, max_chars=45
    )
    assert len(blocks) == 2
    assert blocks[1]["text"].startswith("and ")
    assert blocks[0]["provenance"]["risk_flags"] == []
    assert "hard_capacity_split" not in blocks[1]["provenance"]["risk_flags"]


def test_safe_conjunction_bonus_is_tiered_and_conservative():
    # A comma-led safe onset earns the full breath bonus.
    led_text = "Dinner is served, although guests arrive"
    assert _break_cost(
        led_text, 18, set(), frozenset({18}), 2.0, frozenset({18})
    ) == _break_cost(led_text, 18, set(), frozenset({18})) - 8.0
    # A bare safe onset with a tiny prefix earns nothing beyond the
    # recognition that an onset is not a mid-word cut.
    assert _break_cost(
        "Go although rain falls", 3, set(), frozenset({3})
    ) == _break_cost("Go although rain falls", 3, set()) - 18.0
    # A bare safe onset with a substantial prefix earns only the small bonus.
    long_text = "Dinner was served late although guests arrived early"
    assert _break_cost(
        long_text, 24, set(), frozenset({24})
    ) == _break_cost(long_text, 24, set()) - 21.0  # 18 onset + 3 bare


def test_bare_conjunction_needs_substantial_sides_in_hint_split():
    assert _split_further("Tom and Jerry", "en", 60, 5) == ["Tom and Jerry"]
    assert _split_further("Go although rain falls", "en", 60, 5) == [
        "Go although rain falls"
    ]


def test_partition_avoids_tiny_fragments_when_a_natural_option_exists():
    content = _srt(
        [("00:00:00,000", "00:00:05,000", "First sentence ends. Second carries on.")]
    )
    blocks = create_speech_blocks(
        content, target_language="en", min_chars=10, max_chars=40
    )
    assert all(len(block["text"]) >= 10 for block in blocks)
    # Already inside the cap: do not manufacture extra requests merely
    # because the utterance contains two sentences.
    assert [block["text"] for block in blocks] == [
        "First sentence ends. Second carries on.",
    ]


def test_reviewed_translation_is_cut_on_its_own_linguistic_boundaries():
    display = _srt(
        [
            ("00:00:00,000", "00:00:02,000", "The first half of the thought is here,"),
            ("00:00:02,100", "00:00:04,000", "and the second half continues here."),
        ]
    )
    reviewed = _srt(
        [
            ("00:00:00,000", "00:00:02,000", "Erste Hälfte des Gedankens,"),
            ("00:00:02,100", "00:00:04,000", "und zweite Hälfte folgt hier."),
        ]
    )
    blocks = create_speech_blocks(
        display,
        target_language="de",
        min_chars=5,
        max_chars=32,
        speech_srt_content=reviewed,
    )
    speech = [block["_optimized_text"] for block in blocks]
    assert len(blocks) == 2
    # The cut follows the authored comma, not the stale display-cue seam.
    assert speech[0].endswith(",")
    assert speech[1].startswith("und ")
    assert all(len(part) <= 32 for part in speech)


def test_hindi_safe_conjunctions_are_recognized():
    positions = conjunction_split_positions(
        "वह रुका क्योंकि वह थक गया था और विश्राम किया", language_code="hi"
    )
    assert len(positions) == 1  # only क्योंकि; और is not a safe opener


@pytest.mark.parametrize("text", [
    "This deliberately unpunctuated thought contains many ordinary words but no safe clause boundary at which to cut it.",
    "The collection contains many beautifully illustrated books and numerous carefully handwritten letters from the nineteenth century.",
    "The meeting was cancelled because of the exceptionally severe weather conditions throughout the entire surrounding region.",
])
def test_words_are_not_a_fallback_for_an_unsplittable_utterance(text):
    content = _srt([("00:00:00,000", "00:00:20,000", text)])
    with pytest.raises(UnsplittableSpeechBlockError, match="natural boundary"):
        create_speech_blocks(content, target_language="en", max_chars=40)


@pytest.mark.parametrize("left,right,language", [
    ("The total is 1,", "234 units", "en"),
    ("Wir beginnen um 12:", "30 Uhr", "de"),
    ("The number is 3.", "14159", "en"),
    ("We have preserved the collection", "since 1944", "en"),
    ("Wir sprechen über diese Sammlung", "während des Besuchs", "de"),
])
def test_numeric_and_prepositional_seams_are_not_clauses(left, right, language):
    assert classify_boundary(left, right, language_code=language) is None


def test_ellipsis_and_east_asian_closing_quote_stay_with_the_left_fragment():
    text = "We pause here... then continue speaking."
    offsets = {offset for offset, _kind in natural_split_candidates(text, "en")}
    begin = text.index("...")
    assert begin + 1 not in offsets
    assert begin + 2 not in offsets
    assert begin + 3 in offsets
    japanese = "「最初の文です。」次の文を話します。"
    cuts = {offset for offset, _kind in natural_split_candidates(japanese, "ja")}
    assert japanese.index("。」") + 2 in cuts
    assert japanese.index("。」") + 1 not in cuts


def test_reviewed_atomic_passages_do_not_get_repartitioned_at_internal_commas():
    content = _srt([
        ("00:00:00,000", "00:00:04,000", "An opening clause, followed by detail,"),
        ("00:00:04,100", "00:00:08,000", "and its continuation, with context."),
    ])
    blocks = create_speech_blocks(
        content, target_language="en", max_chars=45,
        speech_srt_content=content, preserve_source_boundaries=True,
    )
    assert [block["subtitles"] for block in blocks] == [[1], [2]]
    assert [block["_optimized_text"] for block in blocks] == [
        "An opening clause, followed by detail,",
        "and its continuation, with context.",
    ]


# -- early repair ---------------------------------------------------------------

from tests.test_dubbing_early_repair import (  # noqa: E402
    _provenance,
    _two_cue_input,
)


def test_repair_rejects_non_string_language_code():
    text, spoken, provenance, duration = _two_cue_input()
    assert (
        find_repair_boundary(text, spoken, provenance, duration, language_code=7)
        is None
    )


def test_repair_accepts_explicit_language_code():
    text, spoken, provenance, duration = _two_cue_input()
    assert (
        find_repair_boundary(text, spoken, provenance, duration, language_code="en")
        == find_repair_boundary(text, spoken, provenance, duration)
    )


def test_repair_rejects_fragments_below_natural_floor():
    text, spoken, provenance, duration = _two_cue_input(
        display_parts=[
            "Short end here.",
            "The second cue has enough words here.",
        ],
    )
    assert find_repair_boundary(text, spoken, provenance, duration) is None


def test_repair_prefers_sentence_over_comma_despite_smaller_advance():
    text, spoken, provenance = _provenance(
        [
            "The opening sentence ends here.",
            "a continuation with a comma,",
            "and the closing sentence ends here.",
        ],
        timings=[(0, 2000), (2200, 6000), (6200, 7000)],
    )
    result = find_repair_boundary(text, spoken, provenance, 1500)
    assert result is not None
    # The period boundary wins even though the comma anchor is longer.
    assert result.boundary_ms == 2200
    assert text[: result.display_cursor].endswith(".")


def test_repair_accepts_safe_conjunction_seam_without_punctuation():
    text, spoken, provenance = _provenance(
        [
            "The reason is still unclear",
            "because the data was incomplete",
        ],
        timings=[(0, 2000), (2200, 5000)],
    )
    result = find_repair_boundary(text, spoken, provenance, 1500)
    assert result is not None
    assert result.boundary_ms == 2200
    # Cursors only: the authored wording is preserved verbatim.
    assert text == (
        "The reason is still unclear because the data was incomplete"
    )


def test_repair_rejects_blanket_coordinator_seam_without_punctuation():
    text, spoken, provenance = _provenance(
        [
            "The reason is still unclear",
            "and the data was incomplete",
        ],
        timings=[(0, 2000), (2200, 5000)],
    )
    assert find_repair_boundary(text, spoken, provenance, 1500) is None


def test_repair_prefers_safe_conjunction_continuation_on_tie():
    first = "First test sentence ends right here, now!!"
    second = "Because second sentence ends right here!!!"
    third = "Final test sentence ends right here, now!!"
    assert len(first) == len(second) == len(third) == 42
    text, spoken, provenance = _provenance(
        [first, second, third],
        timings=[(0, 1000), (2000, 2336), (2336, 3336)],
    )
    result = find_repair_boundary(text, spoken, provenance, 1024)
    assert result is not None
    # Equal strength and equal estimated advance: the safe-conjunction
    # "Because"-led seam wins over the later anchor.
    assert result.boundary_ms == 2000
    assert result.display_cursor == len(first)
