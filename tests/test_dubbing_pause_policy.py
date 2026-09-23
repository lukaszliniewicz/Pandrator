"""Real pause-driven fragments and the guards around semantic reconstruction."""
import pytest

from pandrator.logic.dubbing.llm_correction import (
    apply_correction_operations,
    validate_correction_operations,
)
from pandrator.logic.dubbing.llm_translation import (
    parse_translation_passage_items_details,
)
from pandrator.logic.dubbing.natural_boundaries import classify_boundary
from pandrator.logic.dubbing.pause_policy import (
    may_bridge_unfinished_pause,
)
from pandrator.logic.dubbing.speech_blocks import create_speech_blocks
from pandrator.logic.dubbing.srt_utils import create_translation_blocks
from pandrator.web.logical_passages import passage_srt

PASCAL = [
    (752700, 759500, "Indem Blum Ronges Brief in seiner Zeitung, den Sächsischen Vaterlandsblättern, veröffentlichte,"),
    (759780, 762180, "trug er dazu bei, eine lokale religiöse"),
    (764340, 769060, "Kontroverse zu einer öffentlichen Angelegenheit von nationaler Bedeutung zu machen."),
    (769260, 777500, "So wird die Kritik am Heiligen Rock Teil eines umfassenderen Kampfes für Gewissensfreiheit, Pressefreiheit"),
    (777500, 779420, "und demokratische Mitbestimmung."),
]


def rows(values, speakers=None):
    return [dict(index=i+1, start_ms=a, end_ms=b, start=a/1000, end=b/1000, text=t, speaker=(speakers or {}).get(i+1, "Pascal")) for i, (a,b,t) in enumerate(values)]


def plan(values, speakers=None, **settings):
    items = rows(values, speakers)
    return create_speech_blocks(
        passage_srt(items), target_language="de", max_chars=300,
        preserve_source_boundaries=True,
        speaker_by_subtitle={r["index"]: r["speaker"] for r in items},
        continuation_threshold_ms=settings.pop("continuation_threshold_ms", 3000),
        max_internal_gap_ms=settings.pop("max_internal_gap_ms", 1800), **settings,
    )


def test_pascal_53_54_rebalances_two_complete_sentences_without_text_loss():
    blocks = plan(PASCAL)
    assert [b["text"] for b in blocks] == [" ".join(t for _,_,t in PASCAL[:3]), " ".join(t for _,_,t in PASCAL[3:])]
    assert [len(b["text"]) for b in blocks] == [219, 139]
    assert [b["subtitles"] for b in blocks] == [[1,2,3], [4,5]]
    assert "hesitation_bridged" in blocks[0]["provenance"]["risk_flags"]
    assert blocks[0]["alignment_group"] != blocks[1]["alignment_group"]
    for block in blocks:
        for cue in block["provenance"]["source_cues"]:
            source = PASCAL[cue["reference"]-1]
            assert (cue["start_ms"], cue["end_ms"]) == source[:2]
            a,b = cue["speech_spans"][0]
            assert block["text"][a:b] == source[2]
        assert "estimated_internal_timing" not in block["provenance"]["risk_flags"]


@pytest.mark.parametrize("left,right", [
    ("Eine vollständige Aussage.", "Eine neue Aussage beginnt."),
    ("Er sagte: „Wir dürfen verschieden sein?“", "Das ist ein anderer Gedanke."),
    ("Das ist die Einleitung,", "die nächste Klausel folgt hier."),
    ("Das bleibt weiterhin unklar", "weil die Belege fehlen."),
])
def test_natural_seams_keep_the_ordinary_pause_limit(left,right):
    blocks = plan([(0,2000,left), (4160,7000,right)])
    assert [b["text"] for b in blocks] == [left,right]
    assert not any("hesitation_bridged" in b["provenance"]["risk_flags"] for b in blocks)


@pytest.mark.parametrize("gap", [1801,2160,2999,3000])
def test_bounded_midphrase_hesitation_is_bridged(gap):
    blocks=plan([(0,1000,"eine lokale religiöse"), (1000+gap,5000+gap,"Kontroverse von großer Bedeutung.")])
    assert len(blocks)==1


@pytest.mark.parametrize("settings", [
    {"max_internal_gap_ms":0}, {"continuation_threshold_ms":0},
    {"continuation_threshold_ms":2100},
])
def test_explicit_strict_settings_are_not_replaced_by_fallbacks(settings):
    assert len(plan([(0,1000,"eine lokale religiöse"),(3160,5000,"Kontroverse von Bedeutung.")], **settings))==2


def test_long_pause_is_preserved_and_forced_fragment_is_visible():
    blocks=plan([(0,1000,"eine lokale religiöse"),(5000,7000,"Kontroverse von Bedeutung.")])
    assert len(blocks)==2
    assert "timing_forced_fragment" in blocks[1]["provenance"]["risk_flags"]


def test_speaker_change_never_uses_hesitation_exception():
    blocks=plan([(0,1000,"eine lokale religiöse"),(3160,5000,"Kontroverse von Bedeutung.")], {1:"Pascal",2:"Luke"})
    assert len(blocks)==2


def test_repeated_hesitations_cannot_form_an_unbounded_block():
    blocks=plan([(0,1000,"Es handelt sich um eine"),(3000,4000,"besondere religiöse"),(6000,7000,"Kontroverse von Bedeutung.")])
    assert len(blocks)==2
    assert "timing_forced_fragment" in blocks[1]["provenance"]["risk_flags"]


def test_reviewed_speech_can_veto_a_display_only_bridge():
    values=[(0,1000,"eine lokale religiöse"),(3160,5000,"Kontroverse von Bedeutung.")]
    reviewed=passage_srt(rows([(0,1000,"Eine vollständige Aussage."),(3160,5000,"Eine neue Aussage.")]))
    assert len(plan(values,speech_srt_content=reviewed))==2


def test_continuation_window_is_bounded():
    assert not may_bridge_unfinished_pause("a local religious","controversy",2160,ordinary_gap_ms=1800,continuation_gap_ms=3000,combined_span_ms=31000)


def merge_source(gap=2160,left="a local religious",right="controversy becomes a national issue."):
    return rows([(0,2400,left),(2400+gap,7000+gap,right)])


def test_correction_and_translation_accept_the_same_lossless_phrase_merge():
    source=merge_source()
    operations=[{"action":"merge","cue_ids":[1,2],"texts":["A local religious controversy becomes a national issue."]}]
    validate_correction_operations(source,operations,logical_passages=True)
    corrected=apply_correction_operations(source,operations,logical_passages=True)
    assert len(corrected)==1
    assert corrected[0]["start"]==0
    assert corrected[0]["end"]==9.16
    translated=parse_translation_passage_items_details([{"cue_ids":[1,2],"text":"Eine lokale religiöse Kontroverse wird zu einer nationalen Angelegenheit."}],block=source)
    assert translated[3]==[[1,2]]


@pytest.mark.parametrize("source", [
    merge_source(gap=3001), merge_source(gap=-100),
    merge_source(left="A complete sentence."),
    rows([(0,1000,"a local religious"),(3000,4000,"controversy involving"),(6000,8000,"the whole country.")]),
])
def test_logical_merge_guards_are_shared_by_both_response_validators(source):
    ids=[r["index"] for r in source]
    operations=[{"action":"merge","cue_ids":ids,"texts":["Merged wording."]}]
    with pytest.raises(ValueError):
        validate_correction_operations(source,operations,logical_passages=True)
    with pytest.raises(ValueError):
        parse_translation_passage_items_details([{"cue_ids":ids,"text":"Zusammengeführter Text."}],block=source)


def test_batch_builder_prefers_not_to_cut_the_hesitation_pair():
    values=[(0,1000,"A complete opening sentence."),(1100,2400,"a local religious"),(4560,7000,"controversy becomes a national issue."),(7100,8000,"A closing sentence.")]
    blocks=create_translation_blocks(passage_srt(rows(values)),95,"en",substantial_gap_ms=2000,max_subtitles_per_block=10)
    groups=[[r["index"] for r in block] for block in blocks]
    assert any(2 in group and 3 in group for group in groups), groups
    assert [i for g in groups for i in g]==[1,2,3,4]


def test_quote_aware_sentence_detection_does_not_bridge_a_finished_question():
    assert classify_boundary('„Wie können wir zusammenleben?“', 'Das ist ein Strategiepapier.', language_code='de')==0
