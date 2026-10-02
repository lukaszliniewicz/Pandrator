"""CJK correction split anchors use exact source text offsets."""

import pytest

from pandrator.logic.dubbing.correction_splits import split_boundaries


def _evidence(
    text: str,
    word_texts: list[str],
    *,
    source_word_ids: list[str] | None = None,
    intervals: list[tuple[object, object]] | None = None,
) -> tuple[dict, list[dict]]:
    words = []
    for index, word_text in enumerate(word_texts):
        start_ms, end_ms = (
            intervals[index]
            if intervals is not None
            else (100 + index * 200, 190 + index * 200)
        )
        words.append(
            {
                "id": f"w{index}",
                "text": word_text,
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )
    passage = {
        "text": text,
        "source_word_ids": (
            source_word_ids
            if source_word_ids is not None
            else [word["id"] for word in words]
        ),
        "start_ms": 0,
        "end_ms": 1000,
    }
    return passage, words


@pytest.mark.parametrize(
    ("text", "words", "expected_left", "expected_right", "second_prefix"),
    [
        (
            "今日は、猫が来た。次へ。",
            ["今日は", "猫が来た", "次へ"],
            "今日は、",
            "猫が来た。",
            "今日は、猫が来た。",
        ),
        (
            "今天，下雨了；我们走。",
            ["今天", "下雨了", "我们走"],
            "今天，",
            "下雨了；",
            "今天，下雨了；",
        ),
        (
            "今日は 猫です",
            ["今日は", "猫", "です"],
            "今日は ",
            "猫です",
            "今日は 猫",
        ),
    ],
)
def test_cjk_boundaries_use_exact_text_prefixes_and_keep_separators_left(
    text: str,
    words: list[str],
    expected_left: str,
    expected_right: str,
    second_prefix: str,
):
    passage, source_words = _evidence(text, words)

    boundaries = split_boundaries(passage, source_words, "r1")

    assert [boundary["after_word"] for boundary in boundaries] == [1, 2]
    assert boundaries[0]["left_text"].endswith(expected_left)
    assert boundaries[0]["right_text"].startswith(expected_right)
    assert boundaries[0]["text_offset_utf16"] == len(expected_left.encode("utf-16-le")) // 2
    assert boundaries[1]["text_offset_utf16"] == len(second_prefix.encode("utf-16-le")) // 2


def test_supplementary_character_before_boundary_uses_utf16_code_units():
    text = "😀今日は猫"
    passage, words = _evidence(text, ["今日は", "猫"])

    boundaries = split_boundaries(passage, words, "r1")

    assert len(boundaries) == 1
    assert boundaries[0]["left_text"] == "😀今日は"
    assert boundaries[0]["right_text"] == "猫"
    assert len("😀今日は") == 4
    assert boundaries[0]["text_offset_utf16"] == 5
    assert boundaries[0]["text_offset_utf16"] == len("😀今日は".encode("utf-16-le")) // 2


def test_repeated_lexemes_map_by_exact_word_sequence_and_require_fullmatch():
    passage, words = _evidence("猫猫猫", ["猫猫", "猫"])

    boundaries = split_boundaries(passage, words, "r1")

    assert len(boundaries) == 1
    assert boundaries[0]["after_word"] == 1
    assert boundaries[0]["text_offset_utf16"] == 2
    unmatched, unmatched_words = _evidence("猫猫犬", ["猫猫", "猫"])
    assert split_boundaries(unmatched, unmatched_words, "r1") == []


def test_casefold_expansion_cannot_create_a_cut_inside_one_source_character():
    passage, words = _evidence("Straße語", ["Stras", "se語"])

    assert split_boundaries(passage, words, "r1") == []


@pytest.mark.parametrize(
    ("text", "word_texts", "source_word_ids", "intervals"),
    [
        ("今日は猫", ["今日は", "猫"], [], None),
        ("今日は猫", ["今日は", "猫"], ["w0", "missing"], None),
        ("今日は猫", ["今日は", "猫"], ["w0", "w0"], None),
        ("今日は猫", ["今日は", "犬"], None, None),
        ("今日は、猫", ["今日は", "、", "猫"], None, None),
        ("今日は猫", ["今日は", "猫"], None, [(True, 190), (300, 390)]),
        ("今日は猫", ["今日は", "猫"], None, [(100, 100), (300, 390)]),
        ("今日は猫", ["今日は", "猫"], None, [(100, 1001), (1100, 1200)]),
        ("今日は猫", ["今日は", "猫"], None, [(100, 300), (250, 390)]),
        ("今日は猫", ["今日は", "猫"], None, [(300, 390), (100, 190)]),
    ],
)
def test_invalid_or_ambiguous_cjk_evidence_is_refused(
    text: str,
    word_texts: list[str],
    source_word_ids: list[str] | None,
    intervals: list[tuple[object, object]] | None,
):
    passage, words = _evidence(
        text,
        word_texts,
        source_word_ids=source_word_ids,
        intervals=intervals,
    )

    assert split_boundaries(passage, words, "r1") == []


def test_non_cjk_whitespace_count_mismatch_does_not_use_character_fallback():
    passage, words = _evidence("hello world", ["hel", "lo", "world"])

    assert split_boundaries(passage, words, "r1") == []


def test_cjk_text_with_matching_whitespace_count_keeps_legacy_matching_rules():
    passage, words = _evidence("今日は 猫", ["今日", "は猫"])

    assert split_boundaries(passage, words, "r1") == []


def test_legacy_whitespace_boundary_shape_and_timing_are_unchanged():
    passage, words = _evidence("one two three", ["one", "two", "three"])

    boundaries = split_boundaries(passage, words, "r1")

    assert len(boundaries) == 2
    assert boundaries[0] == {
        "id": boundaries[0]["id"],
        "after_word": 1,
        "left_text": "one",
        "right_text": "two three",
        "left_end_ms": 190,
        "right_start_ms": 300,
    }
    assert "text_offset_utf16" not in boundaries[0]
