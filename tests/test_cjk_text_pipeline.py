"""CJK regression fixtures: text conservation before any paid/model calls."""

from unittest.mock import patch

import pytest
import regex

from pandrator.logic.dubbing.languages import normalize_language_code, ffmpeg_subtitle_language_code
from pandrator.logic.dubbing.models import SubtitleSegment
from pandrator.logic.dubbing.srt_utils import compose_srt, parse_srt
from pandrator.logic.dubbing.speech_blocks import _join_variant, create_speech_blocks, UnsplittableSpeechBlockError
from pandrator.logic.dubbing.subtitle_finalization import (
    SubtitleFinalizationConfig, finalize_srt_content, wrap_subtitle_text,
    compose_transcript_segments,
)
from pandrator.logic.dubbing.text_units import (
    clean_text, contains_cjk, display_length, join_fragments, subtitle_units,
    strip_latin_diacritics, NO_LINE_START, NO_LINE_END,
)


@pytest.mark.parametrize("value,expected", [
    ("Japanese", "ja"), ("ja_JP", "ja"), ("jpn", "ja"),
    ("Korean", "ko"), ("ko-KR", "ko"), ("kor", "ko"),
    ("zh-Hans", "zh-cn"), ("zh-Hant", "zh-tw"),
    ("zh-Hant-HK", "zh-tw"), ("zh_TW", "zh-tw"), ("cmn-Hans-CN", "zh-cn"),
])
def test_language_aliases(value, expected):
    assert normalize_language_code(value) == expected
    assert ffmpeg_subtitle_language_code(value) in {"jpn", "kor", "zho"}


@pytest.mark.parametrize("language,chars,cps", [("ja-JP", 16, 7), ("zh-Hant", 16, 9), ("ko", 16, 12), ("en", 60, 20)])
def test_automatic_subtitle_profiles(language, chars, cps):
    config = SubtitleFinalizationConfig.from_settings({
        "subtitle_language_defaults": True,
        "subtitle_max_chars_per_line": 60, "subtitle_max_cps": 20,
    }, language=language)
    assert (config.max_chars_per_line, config.max_chars_per_second) == (chars, cps)


def test_custom_japanese_delivery_limits_are_not_clamped_to_latin_minimums():
    config = SubtitleFinalizationConfig.from_settings({
        "subtitle_language": "ja", "subtitle_max_chars_per_line": 13, "subtitle_max_cps": 4,
    })
    assert (config.max_chars_per_line, config.max_chars_per_second) == (13, 4)


@pytest.mark.parametrize("parts,expected", [
    (["今日は", "日本語", "です", "。"], "今日は日本語です。"),
    (["「", "自由", "」", "を", "考える。"], "「自由」を考える。"),
    (["我們", "一起", "學習", "。"], "我們一起學習。"),
    (["한국어", "자막을", "검토합니다", "."], "한국어 자막을 검토합니다."),
    (["A", "normal", "English", "sentence", "."], "A normal English sentence."),
    (["２０２６年", "IARF", "の会議"], "２０２６年IARFの会議"),
])
def test_fragment_joining(parts, expected):
    assert join_fragments(parts) == expected


def test_cleaning_preserves_inline_ideographic_space_and_rejoins_japanese_lines():
    assert clean_text("日本語の\n字幕です。") == "日本語の字幕です。"
    assert clean_text("한국어\n자막입니다.") == "한국어 자막입니다."
    assert clean_text("First\nsecond") == "First second"
    assert clean_text("第一部　第二部") == "第一部　第二部"


@pytest.mark.parametrize("value,width", [
    ("か\u3099", 1), ("𠮷", 1), ("漢\U000E0100", 1), ("한", 1),
    ("日本ABC", 3.5), ("日本　語", 4), ("日本 語", 3.5),
])
def test_grapheme_width(value, width):
    assert contains_cjk(value)
    assert display_length(value) == width


def test_latin_accent_removal_does_not_romanize_or_destroy_kana():
    value = "café 日本語 か\u3099 韓国 한국어"
    assert strip_latin_diacritics(value) == "cafe 日本語 か\u3099 韓国 한국어"


@pytest.mark.parametrize("text", [
    "私たちは「信教の自由」を大切にし、世界の人々と共に考えます。",
    "我們尊重每個人的信仰自由，並且一起探討社會與宗教的關係。",
    "우리는 종교의 자유를 존중하며 여러 나라의 사람들과 함께 배우고 있습니다.",
    "日本語とEnglish wordsを混ぜて、２０２６年のIARF会議について話します。",
    "か\u3099きくけこ𠮷田さんの「信教の自由」についてお話しします。",
])
def test_cjk_subtitle_conservation_capacity_and_timing(text):
    source = compose_srt([SubtitleSegment(1, 1000, 17000, text, "")])
    output = parse_srt(finalize_srt_content(source, {"subtitle_language_defaults": True}))
    config = SubtitleFinalizationConfig.from_settings({}, text=text)
    compact = lambda value: regex.sub(r"\s+", "", value)
    assert compact("".join(cue.text for cue in output)) == compact(text)
    assert len(output) >= 3
    assert all(isinstance(cue.start_ms, int) and isinstance(cue.end_ms, int) for cue in output)
    assert all(1000 <= cue.start_ms < cue.end_ms <= 17000 for cue in output)
    assert all(cue.end_ms - cue.start_ms <= 7000 for cue in output)
    assert all(a.end_ms <= b.start_ms for a, b in zip(output, output[1:]))
    for cue in output:
        assert len(cue.text.splitlines()) <= 2
        for line in cue.text.splitlines():
            assert config.character_count(line) <= config.max_chars_per_line
            assert not line or line[0] not in NO_LINE_START
            assert not line or line[-1] not in NO_LINE_END


def test_layout_units_never_cut_clusters_or_lose_existing_spaces():
    text = "日本語 か\u3099きくけこ𠮷田　한국어 test words"
    units = subtitle_units(text)
    assert "".join(units) == text
    boundaries = {m.end() for m in regex.finditer(r"\X", text)}
    cursor = 0
    for unit in units:
        cursor += len(unit)
        assert cursor in boundaries
    assert "か\u3099" in units


def test_korean_word_spacing_is_retained_after_wrapping():
    text = "우리는 종교의 자유를 존중하며 여러 나라의 사람들과 함께 배우고 있습니다."
    config = SubtitleFinalizationConfig.from_settings({}, language="ko")
    wrapped = wrap_subtitle_text(text, config)
    assert clean_text(wrapped) == text


def test_speech_spans_use_codepoint_offsets_not_display_width():
    text, spans = _join_variant("今日は", [(0, 3, 1)], "日本語です。", [(0, 6, 2)])
    assert text == "今日は日本語です。"
    assert spans == [(0, 3, 1), (3, 9, 2)]
    assert text[spans[1][0]:spans[1][1]] == "日本語です。"


def test_speech_blocks_keep_japanese_sentences_and_native_punctuation():
    text = "私たちは信教の自由について考えます。世界の人々と共に学ぶことが大切です。"
    source = compose_srt([SubtitleSegment(1, 0, 14000, text, "")])
    blocks = create_speech_blocks(source, "ja-JP", max_chars=24)
    assert len(blocks) == 2
    assert "".join(str(block["text"]) for block in blocks) == text
    assert all(len(str(block["text"])) <= 24 for block in blocks)


def test_speech_never_uses_subtitle_character_fallback():
    source = compose_srt([SubtitleSegment(1, 0, 8000, "あいうえお" * 10, "")])
    with pytest.raises(UnsplittableSpeechBlockError):
        create_speech_blocks(source, "ja", max_chars=20)


def test_asr_japanese_tokens_are_not_space_separated():
    words = [{"word": text, "start": index * 0.3, "end": (index + 1) * 0.3}
             for index, text in enumerate(["今日", "は", "日本語", "です", "。"])]
    payload = {"language": "ja", "segments": [{"start": 0, "end": 1.5, "text": "今日は日本語です。", "words": words}]}
    with patch("pandrator.logic.dubbing.subtitle_finalization.sentence_segmenter.predict_boundaries", return_value=None):
        cues = compose_transcript_segments(payload, {"target_language": "en", "subtitle_language_defaults": True})
    assert "".join(clean_text(cue.text) for cue in cues) == "今日は日本語です。"


@pytest.mark.parametrize("language,text", [("ja-JP", "日本語です。次の文です。"), ("zh-Hant", "這是測試。下一句。")])
def test_narration_fallback_is_not_tied_to_tts_provider(language, text):
    from pandrator.logic.text_preprocessor import split_into_sentences
    with patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text", return_value=None):
        parts = split_into_sentences(text, language, "Qwen3 TTS")
    assert len(parts) == 2
    assert "".join(parts) == text


def test_narration_rejects_unsafe_character_cut():
    from pandrator.logic.text_preprocessor import split_long_sentences_2
    with pytest.raises(ValueError, match="natural sentence or clause"):
        split_long_sentences_2({"original_sentence": "あいうえお" * 20}, 32, "ja")


@pytest.mark.parametrize("language,expected", [("ja-JP", "JA"), ("jpn", "JA"), ("ko_KR", "KO"), ("zh-Hant", "ZH-HANT"), ("zh-Hans", "ZH-HANS")])
def test_deepl_cjk_target_scripts(language, expected):
    from pandrator.logic.dubbing.llm_translation import get_deepl_language_code
    assert get_deepl_language_code(language) == expected


@pytest.mark.parametrize("language,text", [("ja", "日本語です。"), ("zh-Hant", "信仰自由。"), ("ko", "한국어 자막입니다.")])
def test_canary_incompatibility_is_explicit(language, text):
    from pandrator.logic.dubbing.crispasr import ctc_language_problem
    problem = ctc_language_problem({"stt_language": language}, text)
    assert problem and "unsupported_ctc_language" in problem
    assert ctc_language_problem({"stt_language": "en", "target_language": "ja"}, "English source.") is None
    assert ctc_language_problem({"stt_language": language, "caption_alignment_ctc_model": "/custom/aligner.gguf"}, text) is None


def test_japanese_caption_does_not_call_incompatible_ctc_runner(tmp_path):
    import wave
    from unittest.mock import Mock
    from pandrator.logic.dubbing.caption_alignment import align_caption_cues
    from pandrator.logic.media_edit import MediaCue
    audio = tmp_path / "source.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000 * 5)
    original = MediaCue("ja-cue", 1000, 4000, "日本語の字幕です。")
    runner = Mock(side_effect=AssertionError("Unsupported model must not be called"))
    result = align_caption_cues(audio, (original,), {"stt_language": "auto"}, ctc_runner=runner)
    runner.assert_not_called()
    assert result.cues[0].text == original.text
    assert (result.cues[0].start_ms, result.cues[0].end_ms) == (1000, 4000)
    assert result.cues[0].timing_source == "caption"
    assert result.diagnostics.ctc_request_count == 0
    assert result.diagnostics.accepted_cue_count == 0
    assert any("unsupported_ctc_language" in reason for reason in result.diagnostics.failed_cue_ids["ja-cue"])


def test_oversized_asr_japanese_word_is_display_split_without_fake_word_anchors():
    from pandrator.logic.dubbing.subtitle_finalization import compose_transcript_segments_with_ownership
    text = "私たちは信教の自由について考えます。" * 4
    payload = {"language": "ja", "segments": [{"start": 0, "end": 20, "text": text, "words": [{"word": text, "start": 0, "end": 20}]}]}
    config = SubtitleFinalizationConfig.from_settings({}, language="ja")
    with patch("pandrator.logic.dubbing.subtitle_finalization.sentence_segmenter.predict_boundaries", return_value=None):
        result = compose_transcript_segments_with_ownership(payload, {"subtitle_language_defaults": True})
    assert len(result.segments) >= 3
    assert "".join(clean_text(cue.text) for cue in result.segments) == text
    assert set(result.word_segment_ordinals) == {0}
    assert all(config.character_count(line) <= 16 for cue in result.segments for line in cue.text.splitlines())
    assert result.segments[-1].end_ms == 20000


def test_native_and_passive_subtitle_projection_match_for_japanese():
    from pandrator.logic.dubbing.subtitle_projection import project_subtitle_display
    from pandrator.web.dispatch import _finalize_dispatch_values
    text = "私たちは信教の自由について考えます。世界の人々と共に学ぶことが大切です。"
    values = [{"start_ms": 0, "end_ms": 14000, "text": text, "speaker": "A"}]
    settings = {"subtitle_language": "ja", "subtitle_language_defaults": True}
    assert _finalize_dispatch_values(values, settings) == project_subtitle_display(values, settings)


def test_audio_cpp_and_kokoro_cjk_language_aliases():
    from pandrator.logic.tts_handler import normalize_kokoro_language_code, _audio_cpp_language
    assert normalize_kokoro_language_code("jpn") == "ja"
    assert normalize_kokoro_language_code("zh-Hant") == "zh-cn"
    with patch("pandrator.logic.tts_handler._audio_cpp_model_metadata", return_value={"family": "qwen3_tts"}):
        assert _audio_cpp_language("qwen", "jpn") == "Japanese"
        assert _audio_cpp_language("qwen", "ko-KR") == "Korean"
        assert _audio_cpp_language("qwen", "zh-Hant") == "Chinese"


@pytest.mark.parametrize("reading", ["いまおか", "イマオカ", "コーヒー", "한국어", "か\u3099", "zōng-jiào"])
def test_reviewed_native_readings_are_valid(reading):
    from pandrator.web.pronunciations import validate_respelling
    assert validate_respelling(reading)


def test_reviewed_japanese_name_can_adjoin_particles_but_latin_words_stay_bounded():
    from pandrator.web.pronunciations import apply_reviewed_pronunciations
    from pandrator.web.speech_planning import _bounded_pattern
    entry = {"source_form": "今岡", "phonetic": "いまおか"}
    assert apply_reviewed_pronunciations("今岡さんのお話です。", [entry]) == "いまおかさんのお話です。"
    assert _bounded_pattern("今岡").search("今岡さん")
    assert _bounded_pattern("Luke").search("Lukeさん")
    assert not _bounded_pattern("Luke").search("Lukeville")


def test_speech_planning_handles_native_script_and_combining_marks():
    from pandrator.web.speech_planning import tokenize, _comparison_key, _lexical_tokens, speech_pronunciation_guidance
    text = "か\u3099きくけこ👩‍💻です。"
    boundaries = {0, *[match.end() for match in regex.finditer(r"\X", text)]}
    for token in tokenize(text):
        assert token["start"] in boundaries
        assert token["end"] in boundaries
    assert _comparison_key("か\u3099") == _comparison_key("が")
    assert len(_lexical_tokens("日本語の読み方を検証します")) > 5
    assert "Japanese" in speech_pronunciation_guidance("jpn")
    assert "Korean" in speech_pronunciation_guidance("ko-KR")
    assert "Traditional" in speech_pronunciation_guidance("zh-Hant")


def test_legacy_prompt_migration_preserves_user_written_prompt():
    from pandrator.web.tts_optimization import prompt_sequence, DEFAULT_PROMPT, _clean_response
    custom = "Apply my reviewed glossary only."
    assert prompt_sequence({"combined_prompt": custom}) == [custom]
    assert prompt_sequence({}) == [DEFAULT_PROMPT]
    assert "own declared language" in DEFAULT_PROMPT
    assert _clean_response("日本語の\n字幕です。") == "日本語の字幕です。"


@pytest.mark.parametrize("runtime", [False, True])
def test_explicit_run_limits_override_inherited_automatic_profiles(runtime):
    from pandrator.web.workspace import normalize_subtitle_limit_override
    prefix = "subtitle_" if runtime else ""
    flag = prefix + "language_defaults"
    values = {prefix + "max_cps": 4}
    assert normalize_subtitle_limit_override(values, runtime=runtime)[flag] is False
    assert flag not in values
    assert normalize_subtitle_limit_override({**values, flag: True}, runtime=runtime)[flag] is True
    assert normalize_subtitle_limit_override({}, runtime=runtime) == {}


def test_guarded_speech_compilation_does_not_insert_spaces_around_kana_reading():
    from pandrator.web.speech_planning import compile_plan
    text = "今岡さんのお話です。"
    result = compile_plan(
        {"decisions": []}, mode="guarded", deterministic_text=text,
        candidates=[], known=[{"start": 0, "end": 2, "spoken": "いまおか", "text": "今岡"}],
        protected_template="", validation={},
    )
    assert result == "いまおかさんのお話です。"


def test_subtitle_language_titles_remain_readable_and_distinguish_chinese_scripts():
    from pandrator.logic.dubbing.languages import subtitle_language_title
    assert subtitle_language_title("jpn") == "Japanese"
    assert subtitle_language_title("zh-Hant") == "Chinese (Traditional)"
    assert subtitle_language_title("zh-Hans") == "Chinese (Simplified)"


@pytest.mark.parametrize("space", ["\u00a0", "\u202f"])
def test_no_break_spaces_survive_cleaning_and_subtitle_units(space):
    text = "日本語" + space + "ABC"
    assert clean_text(text) == text
    units = subtitle_units(text)
    assert "".join(units) == text
    assert not any(unit.endswith(space) for unit in units)


def test_plain_transcript_export_removes_presentation_breaks_without_japanese_spaces():
    from pandrator.logic.dubbing.srt_utils import concatenate_subtitle_text
    source = compose_srt([SubtitleSegment(1, 0, 5000, "日本語の\n字幕です。", "")])
    assert concatenate_subtitle_text(source) == "日本語の字幕です。\n"
