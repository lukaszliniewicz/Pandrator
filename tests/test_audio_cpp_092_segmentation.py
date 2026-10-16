"""Acceptance tests for audio.cpp 0.9.2 narration segmentation budgets."""

from __future__ import annotations

import re
from copy import deepcopy

from pandrator.logic.audio_cpp_segmentation import (
    irodori_tokenizer_byte_bound,
    validate_segment_text_window,
)
from pandrator.logic.text_preprocessor import (
    _limit_segment_tokenizer_bytes,
    _limit_segment_words,
)
from tests import test_audiobook_chunking_integration as _integration_fixture


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE)


def _normalized_irodori_bytes(text: str) -> int:
    return sum(3 if character == " " else len(character.encode("utf-8")) for character in text)


def _segment_texts(segments: list[dict]) -> list[str]:
    return [str(segment["original_sentence"]) for segment in segments]


def _numbered_short_word(index: int) -> str:
    """Return a one-letter word so 630 words still fit the manual test budget."""
    return chr(ord("a") + index % 26)


def _voxtral_text() -> str:
    sentences = []
    index = 0
    for _ in range(9):
        words = []
        for position in range(70):
            word = _numbered_short_word(index)
            index += 1
            words.append(word + ("," if position in {13, 27, 41, 55} else ""))
        sentences.append(" ".join(words) + ".")
    return " ".join(sentences)


def _workflow_prepare(text: str, settings: dict):
    fixture = _integration_fixture.AudiobookChunkingIntegrationTests()
    fixture.setUp()
    try:
        source = fixture._register_clean_text(text, filename="audio-cpp-092.txt")
        return fixture._prepare(source, settings)
    finally:
        fixture.tearDown()


def _preparation_settings(model: str, *, mode: str = "model") -> dict:
    return {
        "audiobook_chunking": mode,
        "max_sentence_length": 1500,
        "enable_sentence_splitting": True,
        "enable_sentence_appending": True,
        "enable_nemo_normalization": False,
        "remove_diacritics": False,
        "remove_quotation_marks": False,
        "normalize_all_caps": False,
        "_audiobook_tts_settings": {"service": "audio_cpp", "model": model},
    }


def test_irodori_byte_bound_counts_metaspace_prefix_and_bos() -> None:
    text = "ab c 𐐷!"
    normalized_length = len(text.replace(" ", "▁").encode("utf-8"))

    assert irodori_tokenizer_byte_bound("irodori_tts_600m_v3_f16", text) == normalized_length + 4
    assert irodori_tokenizer_byte_bound("irodori_tts_v4_small_q8_0", text) == normalized_length + 1


def test_irodori_segment_window_accepts_256_rejects_257_and_ignores_other_models() -> None:
    validate_segment_text_window("irodori_tts_600m_v3_f16", "a" * 252)
    validate_segment_text_window("irodori_tts_v4_small_q8_0", "a" * 255)

    for model, text in (
        ("irodori_tts_600m_v3_f16", "a" * 253),
        ("irodori_tts_v4_small_q8_0", "a" * 256),
    ):
        try:
            validate_segment_text_window(model, text)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{model} accepted a 257-token segment")

    validate_segment_text_window("voxtral_4b_tts_2603_q8_0", "a" * 1000)


def test_irodori_byte_limiter_is_lossless_and_preserves_final_paragraph_marker() -> None:
    phrases = [
        f"café{index}𐐷, (naïve{index})—rocket{index}!"
        for index in range(32)
    ]
    text = " ".join(phrases)
    source = {
        "original_sentence": text,
        "paragraph": "yes",
        "chapter": "no",
        "split_part": None,
        "sentence_continues_after": False,
    }
    before = deepcopy(source)

    parts = _limit_segment_tokenizer_bytes(source, 225, "en")

    assert source == before
    assert len(parts) > 1
    assert all(_normalized_irodori_bytes(part["original_sentence"]) <= 225 for part in parts)
    assert _tokens(" ".join(_segment_texts(parts))) == _tokens(text)
    assert [part["paragraph"] for part in parts[:-1]] == ["no"] * (len(parts) - 1)
    assert parts[-1]["paragraph"] == "yes"


def test_word_limiter_splits_naturally_without_loss_and_exempts_chapters() -> None:
    words = [_numbered_short_word(index) for index in range(620)]
    punctuated_words = [
        word + ("," if index % 24 == 23 else "")
        for index, word in enumerate(words)
    ]
    text = " ".join(punctuated_words) + "."
    source = {
        "original_sentence": text,
        "paragraph": "yes",
        "chapter": "no",
        "split_part": None,
        "sentence_continues_after": False,
    }
    before = deepcopy(source)

    parts = _limit_segment_words(source, 240, "en")

    assert source == before
    assert len(parts) >= 3
    assert all(len(part["original_sentence"].split()) <= 240 for part in parts)
    assert _tokens(" ".join(_segment_texts(parts))) == _tokens(text)
    assert all(part["original_sentence"].endswith(",") for part in parts[:-1])
    assert parts[-1]["paragraph"] == "yes"
    assert all(part["paragraph"] == "no" for part in parts[:-1])

    chapter = {**source, "chapter": "yes", "paragraph": "yes"}
    chapter_before = deepcopy(chapter)
    chapter_parts = _limit_segment_words(chapter, 240, "en")
    assert chapter_parts == [chapter]
    assert chapter_parts[0] is chapter
    assert chapter == chapter_before


def test_prepare_text_applies_voxtral_word_budget_but_manual_mode_keeps_configured_limit() -> None:
    text = _voxtral_text()
    model_settings = _preparation_settings("voxtral_4b_tts_2603_q8_0")

    model_result, _model_artifact, model_rows = _workflow_prepare(text, model_settings)

    model_budget = model_result["segmentation_budget"]
    model_texts = _segment_texts(model_rows)
    assert model_budget["target_chars"] == 1152
    assert model_budget["target_words"] == 240
    assert len(model_rows) < 9
    assert all(len(part) <= 1152 for part in model_texts)
    assert all(len(part.split()) <= 240 for part in model_texts)
    assert _tokens(" ".join(model_texts)) == _tokens(text)

    manual_settings = _preparation_settings(
        "voxtral_4b_tts_2603_q8_0",
        mode="manual",
    )
    manual_result, _manual_artifact, manual_rows = _workflow_prepare(text, manual_settings)
    manual_budget = manual_result["segmentation_budget"]
    manual_texts = _segment_texts(manual_rows)
    assert manual_budget["target_chars"] == 1500
    assert "target_words" not in manual_budget
    assert len(manual_rows) == 1
    assert len(manual_texts[0]) <= 1500
    assert len(manual_texts[0].split()) > 240
    assert _tokens(manual_texts[0]) == _tokens(text)


def test_prepare_text_applies_irodori_v4_character_and_normalized_byte_budgets() -> None:
    words = [chr(0x10400 + (index % 80)) for index in range(96)]
    punctuated_words = [
        word + ("," if index % 8 == 7 else "")
        for index, word in enumerate(words)
    ]
    text = " ".join(punctuated_words) + "."
    settings = _preparation_settings("irodori_tts_v4_small_q8_0")

    result, _artifact, rows = _workflow_prepare(text, settings)

    budget = result["segmentation_budget"]
    prepared_texts = _segment_texts(rows)
    assert budget["target_chars"] == 72
    assert budget["target_tokenizer_bytes"] == 225
    assert all(len(part) <= 72 for part in prepared_texts)
    assert all(_normalized_irodori_bytes(part) <= 225 for part in prepared_texts)
    assert _tokens(" ".join(prepared_texts)) == _tokens(text)
