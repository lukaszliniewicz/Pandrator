"""Frame-accurate book-word rebasing across sound-mix insertions."""

from __future__ import annotations

import copy
import unittest

import pytest

from pandrator.logic.book_timing import AlignedBookText, BookWord
from pandrator.web.artifacts import sha256_file
from pandrator.web.models import Artifact
from pandrator.web.sound_book_timing import rebase_book_words, take_base_bounds
from pandrator.web.sound_mix_plan import map_base_frame, remap_audio_metadata
from pandrator.web.workflow_book_export import _load
from tests.test_workflow_book_export import FrozenBookExportTests


def _plan(
    *,
    base_frames: int = 96,
    rate: int = 8000,
    insertions: list[dict[str, int]] | None = None,
) -> dict:
    insertions = insertions or []
    return {
        "base_frames": base_frames,
        "rate": rate,
        "total_frames": base_frames + sum(item["added_frames"] for item in insertions),
        "insertions": insertions,
    }


def _metadata(plan: dict, *, chapters: list[dict] | None = None) -> dict:
    return {
        "audio_timeline": {
            "version": 1,
            "sample_rate_hz": plan["rate"],
            "total_frames": plan["total_frames"],
        },
        "chapters": chapters or [],
        "sound_mix": {
            "frame_plan": plan,
            "base_audio_timeline": {
                "version": 1,
                "sample_rate_hz": plan["rate"],
                "total_frames": plan["base_frames"],
            },
        },
    }


def _row(plan: dict, start: int, end: int) -> dict[str, int]:
    return {
        "base_start_frame": start,
        "base_end_frame": end,
        "start_frame": map_base_frame(plan, start, bias="after"),
        "end_frame": map_base_frame(plan, end, bias="before"),
    }


def _aligned() -> AlignedBookText:
    return AlignedBookText(
        "one two three",
        (
            BookWord("one", 0, 1, 0, 3),
            BookWord("two", 1, 3, 4, 7),
            BookWord("three", 3, 6, 8, 13),
        ),
        "fixture",
        {"duration_ms": 6},
    )


def test_passage_words_keep_local_durations_across_before_and_inside_insertions():
    plan = _plan(insertions=[{"base_frame": 16, "added_frames": 8}, {"base_frame": 56, "added_frames": 16}])
    chapters = [{"start_ms": 2, "title": "Chapter"}]
    metadata = _metadata(plan, chapters=chapters)
    row = _row(plan, 32, 80)
    original_chapters = copy.deepcopy(metadata["chapters"])

    words = rebase_book_words(_aligned(), row, metadata, cue_mode="passages")

    assert [(word.start_ms, word.end_ms) for word in words] == [(5, 6), (6, 8), (10, 13)]
    assert [word.text for word in words] == ["one", "two", "three"]
    assert [(word.start_char, word.end_char) for word in words] == [(0, 3), (4, 7), (8, 13)]
    assert metadata["chapters"] == original_chapters
    assert words[2].start_ms - words[1].end_ms == 2


def test_segments_use_the_mapped_take_bounds_as_one_word():
    plan = _plan(insertions=[{"base_frame": 16, "added_frames": 8}, {"base_frame": 56, "added_frames": 16}])
    row = _row(plan, 32, 80)

    words = rebase_book_words(_aligned(), row, _metadata(plan), cue_mode="segments")

    assert words == (BookWord("one two three", 5, 13, 0, 13),)


def test_final_aligner_rounding_is_clamped_at_original_take_boundary():
    plan = _plan(base_frames=96)
    aligned = AlignedBookText(
        "last",
        (BookWord("last", 9, 11, 0, 4),),
        "fixture",
        {"duration_ms": 11},
    )
    row = _row(plan, 16, 96)

    words = rebase_book_words(aligned, row, _metadata(plan), cue_mode="passages")

    assert words == (BookWord("last", 11, 12, 0, 4),)


def test_unmixed_word_times_match_legacy_rebasing_and_keep_unicode_indices():
    text = "Café 東京"
    aligned = AlignedBookText(
        text,
        (BookWord("Café", 0, 1, 0, 4), BookWord("東京", 1, 2, 5, 7)),
        "fixture",
        {"duration_ms": 2},
    )
    row = {"start_frame": 12345, "end_frame": 56446}
    metadata = {"audio_timeline": {"sample_rate_hz": 44100, "total_frames": 100000}}
    old_offset = row["start_frame"] * 1000 / 44100
    expected = tuple(
        BookWord(
            word.text,
            round(old_offset + word.start_ms),
            round(old_offset + word.end_ms),
            word.start_char,
            word.end_char,
        )
        for word in aligned.words
    )

    assert take_base_bounds(metadata, row) == (12345, 56446)
    assert rebase_book_words(aligned, row, metadata, cue_mode="passages") == expected


def test_half_open_insertion_boundary_uses_after_for_start_and_before_for_end():
    plan = _plan(base_frames=32, insertions=[{"base_frame": 16, "added_frames": 4}])
    metadata = _metadata(plan)
    assert map_base_frame(plan, 16, bias="before") == 16
    assert map_base_frame(plan, 16, bias="after") == 20
    row = _row(plan, 16, 24)
    assert (row["start_frame"], row["end_frame"]) == (20, 28)
    assert take_base_bounds(metadata, row) == (16, 24)

    bad_start = {**row, "start_frame": 16}
    bad_end = {**row, "end_frame": 32}
    with pytest.raises(ValueError, match="mapped bounds"):
        take_base_bounds(metadata, bad_start)
    with pytest.raises(ValueError, match="mapped bounds"):
        take_base_bounds(metadata, bad_end)


@pytest.mark.parametrize(
    "row_changes",
    [
        {"base_start_frame": -1},
        {"base_end_frame": 97},
        {"start_frame": 41},
        {"end_frame": 105},
    ],
)
def test_invalid_base_or_mapped_take_bounds_are_rejected(row_changes: dict[str, int]):
    plan = _plan(insertions=[{"base_frame": 16, "added_frames": 8}, {"base_frame": 56, "added_frames": 16}])
    metadata = _metadata(plan)
    row = {**_row(plan, 32, 80), **row_changes}

    with pytest.raises(ValueError):
        take_base_bounds(metadata, row)


def test_malformed_mixed_metadata_is_rejected_instead_of_falling_back():
    row = {"start_frame": 10, "end_frame": 20}
    with pytest.raises(ValueError, match="frame_plan"):
        take_base_bounds({"sound_mix": {}, "audio_timeline": {}}, row)

    plan = _plan(insertions=[{"base_frame": 16, "added_frames": 8}])
    metadata = _metadata(plan)
    metadata["audio_timeline"]["total_frames"] += 1
    with pytest.raises(ValueError, match="does not match"):
        take_base_bounds(metadata, _row(plan, 32, 80))


class MixedTakeLoadTests(unittest.TestCase):
    def test_load_checks_mixed_pcm_against_original_take_bounds(self):
        fixture = FrozenBookExportTests("test_exact_frames_silence_chapters_and_frozen_mapping")
        fixture.setUp()
        try:
            original = fixture.audio.metadata_json
            base_frames = original["audio_timeline"]["total_frames"]
            plan = _plan(
                base_frames=base_frames,
                rate=fixture.rate,
                insertions=[{"base_frame": 20000, "added_frames": 1000}],
            )
            metadata = remap_audio_metadata(original, plan)
            metadata["sound_mix"] = {
                "frame_plan": plan,
                "base_audio_timeline": {
                    "version": 1,
                    "sample_rate_hz": fixture.rate,
                    "total_frames": base_frames,
                },
            }
            fixture.write_wav(fixture.audio_path, plan["total_frames"])
            content_hash = sha256_file(fixture.audio_path)
            with fixture.database.session() as session:
                selected = session.get(Artifact, fixture.audio.id)
                assert selected is not None
                selected.metadata_json = metadata
                selected.content_hash = content_hash
            fixture.audio.metadata_json = metadata
            fixture.audio.content_hash = content_hash

            takes, _caches, _audio_path, _frames, _rate = _load(
                fixture.context,
                fixture.inputs(),
                fixture.audio,
            )

            assert takes[0].frames == fixture.take_frames[0]
            assert takes[0].manifest["end_frame"] > takes[0].manifest["base_end_frame"]
        finally:
            fixture.tearDown()
