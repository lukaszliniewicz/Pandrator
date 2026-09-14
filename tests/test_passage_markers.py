"""Markers describe real source windows, never guessed translation word times."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pandrator.web.passage_markers import describe_passages, validate_passage_split


def block(texts=None, speech=None, gap=160, **changes):
    texts = texts or ["The first clause,", "followed by its continuation."]
    speech = speech or texts
    cues = []
    d = s = 0
    for i, (display, spoken) in enumerate(zip(texts, speech, strict=True)):
        cues.append({"reference": i + 1, "start_ms": i * (4000 + gap),
                     "end_ms": i * (4000 + gap) + 4000,
                     "display_text": display, "speech_text": spoken,
                     "display_spans": [[d, d + len(display)]],
                     "speech_spans": [[s, s + len(spoken)]]})
        d += len(display) + 1
        s += len(spoken) + 1
    return SimpleNamespace(**{"id": "test", "revision": 1, "removed": False,
        "text": " ".join(texts), "optimized_text": " ".join(speech) if speech != texts else None,
        "language": "en", "speech_block_provenance_json": {"source_cues": cues}, **changes})


def layer(b, which="display"):
    return describe_passages(b)["layers"][which]


def test_exact_unicode_offsets_and_source_windows():
    b = block(["😀 The first clause,", "followed by detail."])
    before = deepcopy(vars(b))
    marker = layer(b)["boundaries"][0]
    assert marker["offset"] == len("😀 The first clause,")
    assert marker["left_window"] == [0, 4000]
    assert marker["right_window"] == [4160, 8160]
    assert marker["gap_ms"] == 160
    assert marker["natural"] and marker["split_allowed"]
    assert vars(b) == before


def test_unfinished_grammar_warns_without_fabricating_word_timing():
    b = block(["trug er dazu bei, eine lokale religiöse", "Kontroverse öffentlich zu machen."], gap=2160, language="de")
    m = layer(b)["boundaries"][0]
    assert not m["natural"] and m["split_allowed"]
    assert "unfinished phrase" in m["warning"]
    assert m["gap_ms"] == 2160


def test_current_wording_must_match_all_stored_spans():
    b = block()
    b.text = b.text.replace("first", "much longer")
    assert layer(b)["status"] == "stale"
    assert layer(b)["boundaries"] == []


def test_no_reconstructed_premerge_boundaries():
    b = block(["A merged passage with several sentences. Still a single window."])
    b.source_segment_ids_json = [12, 13, 14]
    assert layer(b)["status"] == "mapped"
    assert layer(b)["boundaries"] == []


def test_verified_companion_offsets_not_proportional_guesses():
    b = block(speech=["The abbreviated clause,", "followed by a longer spoken continuation."])
    m = layer(b)["boundaries"][0]
    assert validate_passage_split(b, m["id"], "display", m["offset"]) == (
        len("The first clause,"), len("The abbreviated clause,"))
    b.optimized_text = "A completely different speech override."
    m = layer(b)["boundaries"][0]
    assert not m["split_allowed"]
    with pytest.raises(ValueError, match="other text layer"):
        validate_passage_split(b, m["id"], "display", m["offset"])


def test_stale_preview_fails_closed_even_when_plan_revision_same():
    b = block()
    m = layer(b)["boundaries"][0]
    b.revision += 1
    with pytest.raises(ValueError, match="no longer matches"):
        validate_passage_split(b, m["id"], "display", m["offset"])


@pytest.mark.parametrize("gap", [-100, -3900])
def test_overlap_is_inspectable_but_not_independently_splittable(gap):
    b = block(gap=gap)
    m = layer(b)["boundaries"][0]
    assert m["gap_ms"] == gap and not m["split_allowed"]
    with pytest.raises(ValueError, match="overlap"):
        validate_passage_split(b, m["id"], "display", m["offset"])


def test_shared_envelope_has_no_precise_internal_split():
    b = block()
    b.speech_block_provenance_json["risk_flags"] = ["shared_passage_timing"]
    assert not layer(b)["boundaries"][0]["split_allowed"]


@pytest.mark.parametrize("value", [None, [], [{"reference": 1}], ["bad"]])
def test_missing_or_malformed_evidence_is_unavailable(value):
    b = block(speech_block_provenance_json={"source_cues": value})
    assert layer(b)["status"] == "unavailable"
    assert not layer(b)["boundaries"]


def test_clipped_source_range_is_not_claimed_as_a_whole_passage():
    b = block()
    b.speech_block_provenance_json["source_cues"][0]["display_text"] += " Missing words"
    assert layer(b)["status"] == "stale"
    assert not layer(b)["boundaries"]
