"""Word-aware JA/ZH display without changing other languages or source text."""

from copy import deepcopy
from unittest.mock import patch

import pytest
import regex

from pandrator.logic.dubbing import cjk_words
from pandrator.logic.dubbing import subtitle_finalization as subtitles
from pandrator.logic.dubbing.text_units import (
    NO_LINE_END,
    NO_LINE_START,
    clean_text,
    display_length,
)


@pytest.mark.parametrize("language,text,protected", [
    ("ja", "特定の科学的アプローチによって、意識について研究します。", ["科学的", "アプローチ"]),
    ("ja", "グローバルワークスペース理論について説明します。", ["グローバル", "ワークスペース"]),
    ("zh", "我们使用科学方法，研究意识与大脑之间的关系。", ["科学", "意识", "大脑", "关系"]),
    ("zh", "我們使用科學方法，研究意識與大腦之間的關係。", ["科學", "意識", "大腦", "關係"]),
])
def test_real_dictionaries_keep_recognised_words_intact(language, text, protected):
    units = cjk_words.layout_units(text, language, 16)
    assert "".join(units) == text
    ends = set()
    offset = 0
    for unit in units:
        offset += len(unit)
        ends.add(offset)
    for word in protected:
        start = text.index(word)
        assert not ends.intersection(range(start + 1, start + len(word)))


@pytest.mark.parametrize("language,text", [
    ("ja", "附属のＡＢＣとシュミレーション　か\u3099きくけこ👩‍💻について。"),
    ("ja", "科学的アプローチ English words　２０２６年の研究。"),
    ("zh", "繁體中文的科學研究，意識與大腦👩‍💻以及ＡＢＣ。"),
    ("zh", "我们研究意识与大脑。 English words　か\u3099𠮷"),
])
def test_analysis_preserves_script_spaces_and_graphemes(language, text):
    for width in (8, 16):
        units = cjk_words.layout_units(text, language, width)
        assert "".join(units) == text
        grapheme_ends = {item.end() for item in regex.finditer(r"\X", text)}
        offset = 0
        for unit in units:
            offset += len(unit)
            assert offset in grapheme_ends


def test_particles_and_auxiliaries_stay_with_japanese_host():
    units = cjk_words.layout_units("私たちは意識について考えています。", "ja", 16)
    assert "私たちは" in units
    assert not any(unit.startswith(("は", "ます", "です")) for unit in units)


def test_overlong_word_uses_safe_finer_boundaries_without_losing_text():
    text = "スーパーウルトラハイパーミラクルサイエンス　科学的研究。"
    units = cjk_words.layout_units(text, "ja", 8)
    assert "".join(units) == text
    assert all(display_length(unit.strip(" \t"), cjk=True) <= 8 for unit in units)
    assert all(unit[0] not in NO_LINE_START and unit[-1] not in NO_LINE_END for unit in units)


def test_unavailable_dictionary_retains_existing_safe_units():
    cjk_words.word_boundaries.cache_clear()
    with patch.object(cjk_words, "_tokenizer", return_value=None):
        text = "未知の環境でも字幕を失わない。"
        assert cjk_words.layout_units(text, "ja", 16) == cjk_words.subtitle_units(text)
    cjk_words.word_boundaries.cache_clear()


def test_chinese_length_changing_analysis_maps_offsets_to_original():
    class Converter:
        def convert(self, text):
            return text.replace("甲", "甲乙")

    class Segmenter:
        def tokenize(self, text):
            return [(char, index, index + 1) for index, char in enumerate(text)]

    cjk_words.word_boundaries.cache_clear()
    with patch.object(cjk_words, "_tokenizer", return_value=(Segmenter(), Converter())):
        assert cjk_words.word_boundaries("甲丙", "zh") == frozenset({1, 2})
    cjk_words.word_boundaries.cache_clear()


def test_translated_cjk_cards_use_sat_evidence_at_word_boundaries():
    text = "科学意識研究記憶経験情報認知哲学"
    probabilities = [0.0] * len(text)
    probabilities[5] = 0.9
    config = subtitles.SubtitleFinalizationConfig(
        language="ja", max_chars_per_line=12, max_lines=1, sentence_boundary_threshold=0.5,
    )
    with (
        patch.object(cjk_words, "word_boundaries", return_value=frozenset(range(2, len(text) + 1, 2))),
        patch.object(subtitles.sentence_segmenter, "predict_boundaries", return_value={
            "probabilities": probabilities, "boundaries": [], "threshold": 0.5,
        }) as predict,
    ):
        chunks = subtitles._split_words_to_capacity(text, config)
    assert chunks == ["科学意識研究", "記憶経験情報認知哲学"]
    predict.assert_called_once_with(text, threshold=0.5)


@pytest.mark.parametrize("language,text", [
    ("ja", "科学的な研究を続けますが、意識の問題はまだ残っています。"),
    ("zh", "我們使用科學方法，研究意識與大腦之間的關係。"),
])
def test_clause_punctuation_is_preferred_over_balanced_line_lengths(language, text):
    config = subtitles.SubtitleFinalizationConfig.from_settings({}, language=language)
    wrapped = subtitles.wrap_subtitle_text(text, config)
    assert wrapped.splitlines()[0].endswith(("、", "，"))
    assert clean_text(wrapped) == text
    assert all(config.character_count(line) <= 16 for line in wrapped.splitlines())


@pytest.mark.parametrize("language,text", [
    ("en", "This is a complete sentence, and another thought about consciousness."),
    ("de", "Das ist ein vollständiger Satz, und ein weiterer Gedanke über Bewusstsein."),
    ("pl", "To jest pełne zdanie, a tutaj zaczyna się kolejna myśl o świadomości."),
    ("ko", "우리는 종교의 자유를 존중하며 여러 나라의 사람들과 함께 배우고 있습니다."),
    ("en", "We discuss 科学的アプローチ alongside global workspace theory in the lecture."),
])
def test_other_languages_never_enter_the_new_analysis_path(language, text):
    config = subtitles.SubtitleFinalizationConfig.from_settings({}, language=language)
    with (
        patch.object(cjk_words, "layout_units", side_effect=AssertionError("JA/ZH only")),
        patch.object(cjk_words, "word_boundaries", side_effect=AssertionError("JA/ZH only")),
        patch.object(subtitles.sentence_segmenter, "predict_boundaries", side_effect=AssertionError("No new SaT calls")),
    ):
        chunks = subtitles._split_words_to_capacity(text, config, min_chunks=2)
        assert chunks
        for chunk in chunks:
            assert subtitles.wrap_subtitle_text(chunk, config)


def test_sat_failure_still_preserves_words_and_text():
    text = "特定の科学的アプローチによって意識の研究を進めます。" * 3
    config = subtitles.SubtitleFinalizationConfig.from_settings({}, language="ja")
    with patch.object(subtitles.sentence_segmenter, "predict_boundaries", side_effect=RuntimeError("offline")):
        chunks = subtitles._split_words_to_capacity(text, config)
    assert "".join(chunks) == text
    assert not any(chunk.endswith("科") for chunk in chunks)
    assert all(subtitles._fits_layout(chunk, config) for chunk in chunks)


def test_character_timed_asr_keeps_words_and_original_acoustic_ownership():
    text = "科学的なアプローチで意識と大脳について研究します。"
    words = [{"word": char, "start": index * 0.2, "end": (index + 1) * 0.2}
             for index, char in enumerate(text)]
    payload = {"language": "ja", "segments": [{
        "start": 0, "end": len(text) * 0.2, "text": text, "words": words,
    }]}
    original = deepcopy(payload)
    with patch.object(subtitles.sentence_segmenter, "predict_boundaries", return_value=None):
        result = subtitles.compose_transcript_segments_with_ownership(payload, {
            "subtitle_max_chars_per_line": 8, "subtitle_max_lines": 1,
        })
    assert "".join(clean_text(cue.text) for cue in result.segments) == text
    assert set(result.word_segment_ordinals) == set(range(len(words)))
    assert any("科学的" in cue.text for cue in result.segments)
    assert any("アプローチ" in cue.text for cue in result.segments)
    assert all(len(cue.text) <= 8 for cue in result.segments)
    assert payload == original
