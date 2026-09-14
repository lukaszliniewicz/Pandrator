"""Capacity limits must not manufacture boundaries inside timed passages."""

import pytest

from pandrator.logic.dubbing.speech_blocks import create_speech_blocks
from pandrator.web.logical_passages import passage_srt


def plan(texts, *, cap, **kwargs):
    rows = [
        {"text": text, "start_ms": i * 4000, "end_ms": (i + 1) * 4000}
        for i, text in enumerate(texts)
    ]
    return create_speech_blocks(
        passage_srt(rows), max_chars=cap, preserve_source_boundaries=True, **kwargs
    )


def test_pascal_capacity_uses_whole_passages():
    texts = [
        "Am Ende der heutigen Veranstaltung",
        "haben Sie hoffentlich ein klareres Bild davon, wie diese Idee der Freiheit in der Religion entstand",
        "und warum manche Menschen dafür ihre berufliche Laufbahn,",
        "ihr gesellschaftliches Ansehen",
        "und manchmal ihre Sicherheit oder ihr Leben aufs Spiel zu setzen bereit waren.",
    ]
    blocks = plan(texts, cap=300, target_language="de")
    assert len(blocks) == 2
    assert [ref for block in blocks for ref in block["subtitles"]] == [1, 2, 3, 4, 5]
    assert " ".join(block["text"] for block in blocks) == " ".join(texts)
    for block in blocks:
        assert len(block["text"]) <= 300
        assert block["text"] == " ".join(texts[ref - 1] for ref in block["subtitles"])
        assert not block["provenance"]["risk_flags"]
        for cue in block["provenance"]["source_cues"]:
            start, end = cue["speech_spans"][0]
            assert block["text"][start:end] == texts[cue["reference"] - 1]
            assert (cue["start_ms"], cue["end_ms"]) == (
                (cue["reference"] - 1) * 4000, cue["reference"] * 4000
            )


def test_atomic_partition_can_require_more_than_character_lower_bound():
    texts = ["a" * 59 + ".", "b" * 59 + ".", "c" * 59 + "."]
    blocks = plan(texts, cap=100)
    assert [block["text"] for block in blocks] == texts


def test_merged_passage_retains_internal_sentences():
    text = "A complete opening thought. Another sentence in the same merged passage."
    assert [block["text"] for block in plan([text], cap=80)] == [text]


def test_repeated_introductory_phrases_are_not_inferred_as_speakers():
    texts = ["Mit anderen Worten: Ein Gedanke.", "Mit anderen Worten: Noch ein Gedanke."]
    blocks = plan(texts, cap=100, speaker_by_subtitle={1: "Pascal", 2: "Pascal"})
    assert " ".join(block["text"] for block in blocks) == " ".join(texts)


def test_oversized_passage_uses_linguistic_cuts_and_shared_window():
    first = "The opening clause has enough words to stand alone,"
    second = "the next clause completes the same logical passage."
    blocks = plan([f"{first} {second}", "A separate next passage."], cap=70)
    assert [block["text"] for block in blocks] == [first, second, "A separate next passage."]
    assert blocks[0]["alignment_group"] == blocks[1]["alignment_group"]
    assert blocks[1]["alignment_group"] != blocks[2]["alignment_group"]
    for block in blocks[:2]:
        assert "shared_passage_timing" in block["provenance"]["risk_flags"]
        assert "estimated_internal_timing" not in block["provenance"]["risk_flags"]
        event = block["provenance"]["formation_events"][-1]
        assert event["reason_code"] == "shared_passage_capacity_split"
        measurements = event["measurements"]
        assert measurements["timing_basis"] == "shared_source_window"
        assert measurements["source_start_ms"] == 0
        assert measurements["source_end_ms"] == 4000
        assert "estimated_start_ms" not in measurements
        assert "estimated_end_ms" not in measurements
    assert "shared_passage_timing" not in blocks[2]["provenance"]["risk_flags"]


def test_reviewed_variant_uses_the_same_whole_reference_groups():
    texts = ["Long display wording " * 4, "More display wording " * 4, "Closing display wording."]
    speech = ["a" * 59 + ".", "b" * 59 + ".", "c" * 59 + "."]
    speech_srt = passage_srt([
        {"text": text, "start_ms": i * 4000, "end_ms": (i + 1) * 4000}
        for i, text in enumerate(speech)
    ])
    blocks = plan(texts, cap=100, speech_srt_content=speech_srt)
    assert [block["text"] for block in blocks] == [text.strip() for text in texts]
    assert [block["_optimized_text"] for block in blocks] == speech
    assert [block["subtitles"] for block in blocks] == [[1], [2], [3]]


def test_unnatural_source_seam_uses_natural_chunks_in_shared_window():
    from pandrator.logic.dubbing.natural_boundaries import classify_boundary

    texts = [
        "We introduced several people: names such as",
        "Alice, Beatrice, Charlie and Daniel were important to the discussion.",
    ]
    blocks = plan(texts, cap=80)
    assert len(blocks) == 2
    assert " ".join(block["text"] for block in blocks) == " ".join(texts)
    assert all(len(block["text"]) <= 80 for block in blocks)
    assert classify_boundary(blocks[0]["text"], blocks[1]["text"]) is not None
    assert not blocks[0]["text"].endswith("such as")
    assert blocks[0]["alignment_group"] == blocks[1]["alignment_group"]
    assert all("shared_passage_timing" in block["provenance"]["risk_flags"] for block in blocks)
    assert {ref for block in blocks for ref in block["subtitles"]} == {1, 2}


def test_unnatural_source_seam_is_not_an_escape_from_unsplittable_error():
    from pandrator.logic.dubbing.speech_blocks import UnsplittableSpeechBlockError

    with pytest.raises(UnsplittableSpeechBlockError):
        plan(["a" * 60, "b" * 60], cap=100)


@pytest.mark.parametrize("gap, count", [(1499, 1), (1500, 1), (1501, 2)])
def test_whole_passages_still_obey_merge_threshold(gap, count):
    content = passage_srt([
        {"text": "First thought.", "start_ms": 0, "end_ms": 4000},
        {"text": "Next thought.", "start_ms": 4000 + gap, "end_ms": 8000 + gap},
    ])
    blocks = create_speech_blocks(
        content, max_chars=300, merge_threshold=1500,
        max_internal_gap_ms=1800, preserve_source_boundaries=True,
    )
    assert len(blocks) == count
