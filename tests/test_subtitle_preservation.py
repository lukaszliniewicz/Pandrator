"""Literal preservation fixtures independent of the production seam normalizer."""

from pathlib import Path
from unittest.mock import patch

import pytest

from pandrator.logic.dubbing.models import SubtitleSegment
from pandrator.logic.dubbing.srt_utils import (
    compose_srt,
    concatenate_subtitle_text,
    parse_srt,
    renumber_subtitles,
    srt_to_vtt,
)
from pandrator.logic.dubbing.subtitle_finalization import (
    diagnose_srt_content,
    finalize_srt_content,
    finalize_srt_file,
)
from pandrator.logic.dubbing.text_units import clean_text, join_fragments
from pandrator.web.export_subtitles import subtitle_export_diagnostics


def _srt(text: str, start: int = 0, end: int = 2000) -> str:
    return compose_srt([SubtitleSegment(1, start, end, text)])


def _integrity(source: str, final: str) -> dict:
    return subtitle_export_diagnostics(
        source, final, {}, None, language="en", language_origin="test"
    )["text_integrity"]


@pytest.mark.parametrize("prefix", ["Erstens:", "Zweitens:", "Drittens:", "Megismétlem:"])
def test_semantic_prefix_survives_all_display_boundaries(prefix):
    text = prefix + " A complete sentence."
    source = _srt(text)
    final = finalize_srt_content(source)
    assert parse_srt(final, infer_speakers=False)[0].text == text
    assert text in srt_to_vtt(source)
    assert text in concatenate_subtitle_text(source)
    assert text in renumber_subtitles(source)
    assert diagnose_srt_content(source)["observed_max_units_per_line"] == len(text)
    broken = _srt("A complete sentence.")
    assert _integrity(source, broken)["matches"] is False


def test_intentional_speaker_metadata_omission_and_default_inference_remain():
    source = "1\n00:00:00,000 --> 00:00:02,000\n[SPEAKER_01] Hello, everyone.\n"
    expected = _srt("Hello, everyone.")
    assert finalize_srt_content(source) == expected
    assert _integrity(source, expected)["matches"] is True
    assert "SPEAKER" not in compose_srt([SubtitleSegment(1, 0, 2000, "Hello.", "Alice")])
    inferred = parse_srt(_srt("Alice: Hello."))[0]
    assert (inferred.speaker, inferred.text) == ("Alice", "Hello.")


@pytest.mark.parametrize("fragments,expected", [
    (["for", '"What matters"', "today."], 'for "What matters" today.'),
    (['"What matters', '"', "today."], '"What matters" today.'),
    (['He said "', 'hello",', "then left."], 'He said "hello", then left.'),
    (["für", "„Was zählt", "“", "heute."], "für „Was zählt“ heute."),
    (["日本語", '"What matters"', "today."], '日本語"What matters" today.'),
])
def test_quote_seams_use_context(fragments, expected):
    assert clean_text("\n".join(fragments)) == expected
    assert join_fragments(fragments) == expected


def test_integrity_detects_missing_latin_quote_space_independently():
    source = _srt('for\n"What matters"\ntoday.')
    correct = _srt('for "What matters" today.')
    broken = _srt('for"What matters" today.')
    assert _integrity(source, correct)["matches"] is True
    with patch("pandrator.logic.dubbing.text_units.fragment_separator", return_value=""):
        result = _integrity(source, broken)
    assert result["matches"] is False
    assert result["source_sha256"] != result["final_sha256"]


@pytest.mark.parametrize("source,final,matches", [
    ("A B.", "AB.", False),
    ("Hello, world!", "Hello world!", False),
    ("„Bewusstsein“ im Alltag.", "„Bewusstsein“im Alltag.", False),
    ("日本語 ABC word 日本語 DEF word 日本語", "日本語\nABC word\n日本語 DEF word 日本語", True),
    ("日本語日本語", "日本語\n日本語", True),
    ("日本語 ABC word", "日本語 ABCword", False),
])
def test_independent_integrity_literal_expectations(source, final, matches):
    assert _integrity(_srt(source), _srt(final))["matches"] is matches


def test_preserve_timing_keeps_terminal_short_cue_and_diagnostic():
    source = _srt("End.", 900, 1000)
    final = finalize_srt_content(source, preserve_timing=True)
    cue = parse_srt(final, infer_speakers=False)[0]
    assert (cue.start_ms, cue.end_ms, cue.text) == (900, 1000, "End.")
    diagnostic = diagnose_srt_content(final)
    assert diagnostic["reading_speed_violating_cues"] == 1
    assert diagnostic["min_duration_violating_cues"] == 1
    assert "duration_below_minimum" in diagnostic["issues"][0]["reasons"]


@pytest.mark.parametrize("text,end", [
    ("one two three four five six seven eight nine ten eleven twelve", 100),
    ("Supercalifragilisticexpialidocious", 20000),
    ("日本語 ABC word 日本語 DEF word 日本語", 100),
])
def test_preserving_split_retains_outer_bounds_and_all_text(text, end):
    source = _srt(text, 0, end)
    final = finalize_srt_content(
        source, {"subtitle_max_chars_per_line": 8, "subtitle_max_lines": 1},
        preserve_timing=True,
    )
    cues = parse_srt(final, infer_speakers=False)
    assert (min(c.start_ms for c in cues), max(c.end_ms for c in cues)) == (0, end)
    assert all(0 <= c.start_ms < c.end_ms <= end for c in cues)
    assert _integrity(source, final)["matches"] is True
    if end == 20000:
        diagnostic = diagnose_srt_content(final)
        assert diagnostic["max_duration_violating_cues"] == 1
        assert "duration_above_maximum" in diagnostic["issues"][0]["reasons"]


def test_duration_warning_boundaries_are_exact_and_invalid_spans_stay_separate():
    source = compose_srt([
        SubtitleSegment(1, 0, 833, "Minimum."),
        SubtitleSegment(2, 1000, 8000, "Maximum."),
        SubtitleSegment(3, 9000, 9000, "Invalid."),
    ])
    diagnostic = diagnose_srt_content(source)
    assert diagnostic["min_duration_violating_cues"] == 0
    assert diagnostic["max_duration_violating_cues"] == 0
    assert diagnostic["invalid_duration_cues"] == 1


def test_file_export_preserves_timing_by_default(tmp_path: Path):
    source, destination = tmp_path / "source.srt", tmp_path / "final.srt"
    source.write_text(_srt("End.", 900, 1000), encoding="utf-8")
    finalize_srt_file(source, destination)
    assert destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_nested_source_cues_keep_each_envelope_in_preservation_mode():
    source = compose_srt([
        SubtitleSegment(1, 0, 10000, "Outer."),
        SubtitleSegment(2, 2000, 3000, "Inner."),
        SubtitleSegment(3, 9000, 12000, "Tail."),
    ])
    final = finalize_srt_content(source, preserve_timing=True)
    cues = parse_srt(final, infer_speakers=False)
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in cues] == [
        (0, 10000, "Outer."), (2000, 3000, "Inner."), (9000, 12000, "Tail."),
    ]
    assert _integrity(source, final)["matches"] is True
    assert diagnose_srt_content(final)["overlapping_cues"] == 2


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("original,broken", [
    ("Erstens: A complete sentence.", "A complete sentence."),
    ('for\n"What matters"\ntoday.', 'for"What matters" today.'),
])
def test_file_export_rejects_loss_before_writing(tmp_path: Path, existing, original, broken):
    source, destination = tmp_path / "source.srt", tmp_path / "final.srt"
    source.write_text(_srt(original), encoding="utf-8")
    if existing:
        destination.write_text("existing artifact", encoding="utf-8")
    with patch(
        "pandrator.logic.dubbing.subtitle_finalization.finalize_srt_content",
        return_value=_srt(broken),
    ), pytest.raises(ValueError, match="preserv"):
        finalize_srt_file(source, destination)
    if existing:
        assert destination.read_text(encoding="utf-8") == "existing artifact"
    else:
        assert not destination.exists()


@pytest.mark.parametrize("space", ["\u3000", "\u00a0", "\u202f"])
def test_non_ascii_body_spaces_are_preserved_by_integrity(space):
    source = _srt(f"日本語{space}本文")
    assert _integrity(source, _srt("日本語本文"))["matches"] is False
    assert _integrity(source, source)["matches"] is True
    assert _integrity(source, _srt(f"日本語{space}\n本文"))["matches"] is True


def test_file_export_preserves_interior_ideographic_space(tmp_path: Path):
    source, destination = tmp_path / "source.srt", tmp_path / "final.srt"
    source.write_text(_srt("日本語\u3000本文"), encoding="utf-8")
    finalize_srt_file(source, destination)
    assert "日本語\u3000本文" in destination.read_text(encoding="utf-8")


def test_file_export_rejects_removed_ideographic_space(tmp_path: Path):
    source, destination = tmp_path / "source.srt", tmp_path / "final.srt"
    source.write_text(_srt("日本語\u3000本文"), encoding="utf-8")
    with patch(
        "pandrator.logic.dubbing.subtitle_finalization.finalize_srt_content",
        return_value=_srt("日本語本文"),
    ), pytest.raises(ValueError, match="preserv"):
        finalize_srt_file(source, destination)
    assert not destination.exists()


@pytest.mark.parametrize("timestamp", [
    "bad --> 00:00:02,000", "00:00:00,000 --> bad", "bad timestamp",
])
def test_integrity_refuses_malformed_timestamp_body_loss(timestamp):
    good = _srt("Other good cue.")
    source = good + f"\n2\n{timestamp}\nReviewed words in bad block.\n"
    result = _integrity(source, good)
    assert result["matches"] is False
    assert result["source_structure_valid"] is False
    assert result["final_structure_valid"] is True
    assert _integrity(good, source)["matches"] is False
    assert _integrity(source, source)["matches"] is False


def test_blank_blocks_and_explicit_speaker_metadata_remain_accepted():
    source = "\n\n \t\n1\n00:00:00,000 --> 00:00:02,000\n[SPEAKER_01] Hello.\n\n"
    result = _integrity(source, _srt("Hello."))
    assert result["matches"] is True
    assert result["source_structure_valid"] is True


@pytest.mark.parametrize("existing", [False, True])
def test_file_export_refuses_malformed_source_before_destination_write(tmp_path: Path, existing):
    source, destination = tmp_path / "source.srt", tmp_path / "final.srt"
    source.write_text(
        _srt("Other good cue.") + "\n2\nbad --> bad\nReviewed words in bad block.\n",
        encoding="utf-8",
    )
    if existing:
        destination.write_text("existing artifact", encoding="utf-8")
    with pytest.raises(ValueError, match="preserv"):
        finalize_srt_file(source, destination)
    if existing:
        assert destination.read_text(encoding="utf-8") == "existing artifact"
    else:
        assert not destination.exists()


@pytest.mark.parametrize("text,expected_spaces", [
    ("日本語\u3000\n本文", 1),
    ("日本語\u3000本文日本語\u3000本文日本語\u3000本文", 3),
])
def test_file_export_never_publishes_layout_space_loss(tmp_path: Path, text, expected_spaces):
    source, destination = tmp_path / "source.srt", tmp_path / "final.srt"
    source.write_text(_srt(text), encoding="utf-8")
    finalize_srt_file(source, destination, {"subtitle_max_chars_per_line": 8})
    result = destination.read_text(encoding="utf-8")
    assert result.count("\u3000") == expected_spaces
    assert _integrity(source.read_text(encoding="utf-8"), result)["matches"] is True
