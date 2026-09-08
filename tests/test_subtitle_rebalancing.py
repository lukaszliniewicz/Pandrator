from __future__ import annotations

from collections.abc import Iterable

from pandrator.logic.dubbing.subtitle_finalization import SubtitleFinalizationConfig
from pandrator.logic.dubbing.subtitle_rebalancing import (
    _SourceWord,
    _TokenTime,
    _candidate_range,
    _estimated_token_times,
    _ends_clause,
    _ends_sentence,
    _source_silences,
    _stranded_fragment,
    rebalance_display_cues,
)


SPEAKER = "Luke Liniewicz"
OTHER_SPEAKER = "Pascal Schilling"


def _cue(start_ms: int, end_ms: int, text: str, *, speaker: str = SPEAKER, **extra):
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
        "speaker": speaker,
        "review_state": "clear",
        "review_note": "",
        "evidence_ids": [],
        "uncertain_source_cue_ids": [],
        **extra,
    }


def _word(text: str, start_ms: int, end_ms: int, *, speaker: str = SPEAKER):
    return {"text": text, "start_ms": start_ms, "end_ms": end_ms, "speaker": speaker}


def _fixture_values():
    return [
        _cue(
            76920,
            85516,
            "The recordings will be available online, so if you have friends "
            "or anyone who might be interested, please recommend",
        ),
        _cue(85516, 87160, "it. Thank you, Pascal."),
        _cue(87160, 88600, "The floor is yours."),
        _cue(
            88760,
            100760,
            "Thank you. If someone has a question during the lecture, please "
            "help me notice it, because I'll be quite focused on",
            speaker=OTHER_SPEAKER,
        ),
        _cue(103320, 104460, "my notes.", speaker=OTHER_SPEAKER),
    ]


def _fixture_words():
    words: list[dict[str, object]] = []
    first = [
        ("The", 77560, 77640),
        ("recordings", 77800, 78200),
        ("will", 78760, 78840),
        ("be", 78920, 79000),
        ("available", 79080, 79400),
        ("online,", 79640, 79960),
        ("so", 79960, 80040),
        ("if", 80120, 80200),
        ("you", 80280, 80360),
        ("have", 80360, 80440),
        ("friends", 80600, 80840),
        ("or", 81000, 81080),
        ("anyone", 81640, 81880),
        ("who", 82040, 82120),
        ("might", 82200, 82440),
        ("be", 82440, 82520),
        ("interested,", 82760, 83320),
        ("please", 83400, 83640),
        ("recommend", 83800, 84200),
        ("it.", 84280, 85080),
        ("Thank", 85880, 86040),
        ("you,", 86120, 86360),
        ("Pascal.", 86520, 87160),
        ("The", 87160, 87240),
        ("floor", 87320, 87560),
        ("is", 87640, 87800),
        ("yours.", 87880, 88600),
    ]
    second = [
        ("Thank", 88760, 88920),
        ("you.", 89000, 89800),
        ("I'd", 90020, 90340),
        ("like", 90420, 90500),
        ("to", 90580, 90660),
        ("ask", 90820, 90980),
        ("you", 91060, 91140),
        ("kindly", 91540, 91860),
        ("if", 92180, 92260),
        ("someone", 92580, 92820),
        ("has", 93140, 93220),
        ("a", 93380, 93460),
        ("question", 93540, 93620),
        ("in", 94260, 94340),
        ("between", 94580, 94660),
        ("the", 95060, 95140),
        ("lecture.", 95460, 97220),
        ("Um,", 97180, 97740),
        ("please", 97740, 97980),
        ("help", 98140, 98220),
        ("me", 98460, 98540),
        ("to", 98700, 98780),
        ("identify.", 99340, 100620),
        ("uh,", 100620, 100860),
        ("the", 100860, 100940),
        ("question,", 101020, 101420),
        ("because", 101580, 101660),
        ("I", 101740, 101820),
        ("will", 101900, 101980),
        ("be", 102140, 102220),
        ("quite", 102460, 102540),
        ("focused", 102780, 103100),
        ("on", 103260, 103340),
        ("my", 103500, 103580),
        ("notes,", 103740, 104460),
    ]
    words.extend(_word(text, start, end) for text, start, end in first)
    words.extend(
        _word(text, start, end, speaker=OTHER_SPEAKER) for text, start, end in second
    )
    return words


def _config() -> SubtitleFinalizationConfig:
    return SubtitleFinalizationConfig(
        max_chars_per_line=60,
        max_lines=2,
        min_duration_ms=833,
        max_duration_ms=12000,
        max_chars_per_second=20,
        min_gap_ms=80,
        phrase_gap_ms=900,
        hard_gap_ms=1500,
    )


def _tokens(items: Iterable[dict[str, object]]) -> list[str]:
    return " ".join(str(item["text"]) for item in items).split()


def test_fixture_rebalances_weak_boundaries_and_repairs_the_artificial_gap():
    values = _fixture_values()
    result = rebalance_display_cues(
        values,
        _config(),
        timing_words=_fixture_words(),
        match_source_words=True,
    )

    assert _tokens(result) == _tokens(values)
    assert any(
        ["please", "recommend", "it."]
        == cue["text"]
        .replace("\n", " ")
        .split()[
            cue["text"].replace("\n", " ").split().index("please") : cue["text"]
            .replace("\n", " ")
            .split()
            .index("please")
            + 3
        ]
        for cue in result
        if "please" in cue["text"]
        and "recommend" in cue["text"]
        and "it." in cue["text"]
    )
    because_cue = next(
        cue for cue in result if "because" in cue["text"] and "my notes." in cue["text"]
    )
    assert because_cue["start_ms"] == 97740
    assert because_cue["end_ms"] == 104460
    assert "my notes." in because_cue["text"].replace("\n", " ")
    assert any((cue["start_ms"], cue["end_ms"]) == (77560, 79960) for cue in result)
    assert all(cue["speaker"] in {SPEAKER, OTHER_SPEAKER} for cue in result)
    assert all(cue["review_state"] == "clear" for cue in result)
    assert all(cue["end_ms"] - cue["start_ms"] <= 12000 for cue in result)
    assert all(len(line) <= 60 for cue in result for line in cue["text"].splitlines())

    assert result == rebalance_display_cues(
        values,
        _config(),
        timing_words=_fixture_words(),
        match_source_words=True,
    )


def test_true_source_silence_refuses_estimated_window():
    values = [_cue(0, 1500, "A continuing"), _cue(1500, 3000, "thought here.")]
    words = [
        _word("A", 0, 100),
        _word("thought", 2000, 2100),
        _word("here.", 2200, 2300),
    ]

    result = rebalance_display_cues(values, _config(), timing_words=words)

    assert [cue["text"] for cue in result] == [cue["text"] for cue in values]
    assert [(cue["start_ms"], cue["end_ms"]) for cue in result] == [
        (0, 1500),
        (1500, 3000),
    ]


def test_gap_coverage_requires_speech_inside_the_apparent_gap():
    values = [_cue(0, 1000, "A continuing"), _cue(2000, 3000, "thought here.")]
    bordering_words = [_word("continuing", 900, 1000), _word("thought", 2000, 2100)]

    result = rebalance_display_cues(values, _config(), timing_words=bordering_words)

    assert [(cue["start_ms"], cue["end_ms"]) for cue in result] == [
        (0, 1000),
        (2000, 3000),
    ]


def test_speaker_and_review_metadata_boundaries_are_untouched():
    values = [
        _cue(0, 1000, "A continuing", speaker=SPEAKER),
        _cue(1000, 2000, "thought here.", speaker=OTHER_SPEAKER),
        _cue(2000, 3000, "More words", review_note="keep this note"),
    ]

    result = rebalance_display_cues(values, _config())

    assert len(result) == len(values)
    assert _tokens(result) == _tokens(values)
    assert [cue["speaker"] for cue in result] == [cue["speaker"] for cue in values]
    assert [cue["review_note"] for cue in result] == [
        cue["review_note"] for cue in values
    ]


def test_unmatched_source_words_use_estimated_monotonic_times_without_text_loss():
    values = [
        _cue(1000, 3000, "Hallo zusammen"),
        _cue(3000, 5000, "wir beginnen jetzt."),
    ]
    unrelated_words = [
        _word("completely", 1000, 1100),
        _word("different", 1200, 1300),
        _word("language", 1400, 1500),
    ]

    result = rebalance_display_cues(
        values,
        _config(),
        timing_words=unrelated_words,
        match_source_words=True,
    )

    assert _tokens(result) == _tokens(values)
    assert all(cue["start_ms"] < cue["end_ms"] for cue in result)


def test_no_window_change_still_normalizes_whitespace_and_wraps():
    values = [_cue(0, 1000, "  Keep\nthis   text.  ", speaker="")]

    result = rebalance_display_cues(values, _config())

    assert result[0]["text"] == "Keep this text."
    assert result[0]["start_ms"] == 0
    assert result[0]["end_ms"] == 1000


def test_candidate_preserves_zero_gap_and_nested_spoken_endpoints():
    times = [
        _TokenTime("hello", 0, 1500),
        _TokenTime("there", 500, 1000),
        _TokenTime("friend", 1500, 2500),
    ]
    result = _candidate_range(0, 2, times, [v.text for v in times], _config(), [], 2500)
    assert result is not None
    assert result.end_ms == 1500
    final = _candidate_range(2, 3, times, [v.text for v in times], _config(), [], 2500)
    assert final is not None
    assert final.end_ms == 2500


def test_reading_extension_cannot_consume_a_long_trailing_pause():
    times = [_TokenTime("x" * 40, 0, 100)]
    assert _candidate_range(0, 1, times, [times[0].text], _config(), [], 5000) is None


def test_nested_words_do_not_invent_silence_and_estimates_stay_in_window():
    words = [
        _SourceWord("one", 0, 3000),
        _SourceWord("two", 100, 200),
        _SourceWord("three", 2000, 3100),
    ]
    assert _source_silences(words, 0, 3100, 1500) == []
    times = _estimated_token_times(["longword", "a", "b"], 0, 3)
    assert times is not None
    assert [(v.start_ms, v.end_ms) for v in times] == [(0, 1), (1, 2), (2, 3)]


def test_stranded_sentence_prefix_is_scored_only_after_weak_boundaries():
    assert _stranded_fragment("recommend", "it. Thank you, Pascal.") == 40
    assert _stranded_fragment("interested,", "please recommend it.") == 0


def test_new_boundaries_require_punctuation_and_do_not_split_german_ordinals():
    values = [
        _cue(
            0,
            6500,
            "Philosophisch klingt darin Baruch de Spinozas pantheistische Tradition an. "
            "Dort ist das Göttliche die",
        ),
        _cue(
            6500,
            14000,
            "allumfassende Ordnung der Natur. Ebenso gibt es Anklänge an Hegels "
            "Vorstellung der Wirklichkeit als geschichtlichen",
        ),
        _cue(14000, 16500, "Prozess des Geistes."),
    ]
    result = rebalance_display_cues(values, _config())
    assert _tokens(result) == _tokens(values)
    original_ends = set()
    end = 0
    for cue in values:
        end += len(cue["text"].split())
        original_ends.add(end)
    end = 0
    for cue in result[:-1]:
        end += len(cue["text"].split())
        assert (
            end in original_ends
            or _ends_sentence(cue["text"])
            or _ends_clause(cue["text"])
        )
    assert not _ends_sentence("19.")
    assert not _ends_sentence("20.”")
