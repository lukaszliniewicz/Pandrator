"""Focused SOURCE-only provisional-ASR sentence assessment.

Covers the assessment helper directly (ellipsis, years/decimals,
abbreviations, repetition, fillers, language aliases, source-only clause
ranks) plus the size-setting propagation from builder to run splitter.
Shared ``natural_boundaries``/``pause_policy`` behaviour is asserted only
to prove it is untouched, never modified here.
"""
import pytest

from pandrator.logic.dubbing.logical_passages import build_source_passages
from pandrator.logic.dubbing.source_passage_policy import (
    SOURCE_PASSAGE_POLICY_VERSION,
    select_boundaries,
)
from pandrator.logic.dubbing.source_sentence_assessment import (
    SOURCE_PASSAGE_POLICY_VERSION as HELPER_VERSION,
    FILLER_SCRAP_MAX_WORDS,
    hold_short_sentence_reason,
    is_filler_sentence,
    is_genuine_source_sentence,
    is_supported_source_language,
    left_sentence_kind,
    repetition_kind,
    resolve_source_language,
    source_clause_rank,
)
from pandrator.logic.dubbing import natural_boundaries
from tests.test_dubbing_logical_passages import _timed_fixture


def test_policy_version_is_exposed_and_consistent():
    assert SOURCE_PASSAGE_POLICY_VERSION == "source_provisional_v1"
    assert HELPER_VERSION == SOURCE_PASSAGE_POLICY_VERSION


def test_ellipsis_is_incomplete_not_sentence():
    assert left_sentence_kind("Yes…", "en") == "ellipsis"
    assert left_sentence_kind("It’s…", "en") == "ellipsis"
    assert left_sentence_kind("And...", "en") == "ellipsis"
    assert not is_genuine_source_sentence("Yes…", "more", "en")
    # The shared TTS classifier is intentionally untouched.
    assert natural_boundaries.classify_boundary("Yes…", "more") == 0


def test_year_sentences_preserved_decimals_guarded():
    assert is_genuine_source_sentence("I was born in 1987.", "Next", "en")
    assert is_genuine_source_sentence("I was born in 1987.", "", "en")
    assert not is_genuine_source_sentence("Pi is 3.", "14 exactly", "en")
    assert is_genuine_source_sentence("Pi is 3.", "14 exactly".replace("14", "much"), "en")


def test_abbreviations_supported_latin_only():
    assert not is_genuine_source_sentence("Met Dr.", "Smith", "en")
    assert not is_genuine_source_sentence("Met Dr.", "Smith", "de")
    # Unknown/mixed codes must not inherit English abbreviation grammar;
    # documented limitation: pass the real language instead.
    assert is_genuine_source_sentence("Met Dr.", "Smith", "xx")


def test_repetition_kinds():
    assert repetition_kind("Yes.", "Yes…") == "exact"
    assert repetition_kind("Never.", "Never again.") == "rhetorical_extension"
    assert repetition_kind("Hello world.", "Next phrase.") is None


def test_filler_tables_bounded_per_language():
    assert is_filler_sentence("Yeah.", "en")
    assert is_filler_sentence("Yeah.", "English")
    assert is_filler_sentence("Ja.", "de")
    assert not is_filler_sentence("Yeah.", "xx")
    assert not is_filler_sentence("", "en")
    assert not is_filler_sentence("And that's quite a progressive.", "en")
    assert not is_filler_sentence("Goal.", "en")


def test_language_aliases_and_support():
    assert resolve_source_language("English") == "en"
    assert resolve_source_language("en-US") == "en"
    assert resolve_source_language("zh") == "zh-cn"
    assert is_supported_source_language("en")
    assert is_supported_source_language("de")
    assert not is_supported_source_language("xx")
    assert not is_supported_source_language("")


def test_source_clause_rank_never_inherits_english():
    assert source_clause_rank("Alpha beta", "because gamma", "en") == 3
    assert source_clause_rank("Alpha beta", "because gamma", "xx") is None
    assert source_clause_rank("Alpha beta", "weil gamma", "de") == 3
    assert source_clause_rank("Alpha beta,", "gamma", "xx") == 2
    assert source_clause_rank("Alpha beta;", "gamma", "xx") == 1
    assert source_clause_rank("Alpha beta", "gamma", "en") is None


def test_cjk_and_quotes_assessed_not_rewritten():
    assert is_genuine_source_sentence("螳螂捕蝉。", "黄雀在后。", "zh-cn")
    assert is_genuine_source_sentence('He said “Hello world.”', "Next", "en")
    assert is_genuine_source_sentence("It's about reason.", "Next", "en")


def test_hold_reasons_bounded():
    assert hold_short_sentence_reason("Yes.", "Yes…", "en", 60) == "exact_repetition_ellipsis"
    assert hold_short_sentence_reason("Never.", "Never again.", "en", 60) is None
    assert hold_short_sentence_reason("Yeah.", "Goal.", "en", 60) == "filler_scrap"
    assert hold_short_sentence_reason("And that's quite a progressive.", "Yeah.", "en", 60) is None
    assert hold_short_sentence_reason("Yes.", "So I went to the store.", "en", 60) is None
    assert hold_short_sentence_reason("Yeah.", "Goal.", "xx", 60) is None
    assert hold_short_sentence_reason("A" * 60 + ".", "Goal.", "en", 60) is None


def test_min_chars_propagates_to_run_splitter():
    # Default 60: cross-cue "Yes."/"Yes…" holds into one passage.
    cues, words = _timed_fixture(['Yes.', 'Yes…'], gaps=[100])
    assert len(build_source_passages(cues, words)) == 1
    # Custom min 4: the 4-char leader is substantial, the seam splits.
    cues, words = _timed_fixture(['Yes.', 'Yes…'], gaps=[100])
    result = build_source_passages(cues, words, min_chars=4)
    assert [p['text'] for p in result] == ['Yes.', 'Yes…']
    # Custom min also reaches in-run selection: "Yeah." holds by default…
    cues, words = _timed_fixture(["And that's quite a progressive. Yeah. Goal."])
    assert any('filler_scrap' in str(p.get('boundary_selection', {}).get('suppressed_sentence_splits'))
               for p in build_source_passages(cues, words))
    # …but not when the leader already reaches the minimum.
    cues, words = _timed_fixture(["And that's quite a progressive. Yeah. Goal."])
    result = build_source_passages(cues, words, min_chars=5)
    assert [p['text'] for p in result] == [
        "And that's quite a progressive.", 'Yeah.', 'Goal.']


def test_provenance_keys_present_on_word_timed_rows():
    cues, words = _timed_fixture(['Hello world. Next phrase.'])
    result = build_source_passages(cues, words, language_code='en')
    selection = result[0]['boundary_selection']
    assert selection['policy'] == selection['policy_version'] == 'source_provisional_v1'
    assert (selection['soft_min_chars'], selection['preferred_chars'],
            selection['sentence_lookahead_chars']) == (60, 160, 20)
    assert selection['max_span_ms'] == 8000
    assert selection['pause_ms'] == 650
    assert selection['language_code'] == 'en'
    assert selection['suppressed_sentence_splits'] == []


def test_selector_ellipsis_label_demoted_not_split():
    tokens = "Yes Yes…".split()
    result = select_boundaries(tokens, {1: 'sentence', 2: 'sentence'}, language_code='en')
    assert len(result) == 1
    assert result[0].reason == 'cue'
    assert result[0].suppressed[0]['reason'] == 'ellipsis_or_non_genuine_sentence'


def test_selector_keeps_genuine_short_pair_split():
    tokens = "Hello world. Next phrase.".split()
    result = select_boundaries(tokens, {2: 'sentence', 4: 'sentence'}, language_code='en')
    assert [b.offset for b in result] == [2, 4]
    assert all(b.suppressed == () for b in result)


def test_filler_scrap_max_words_bound():
    assert FILLER_SCRAP_MAX_WORDS == 3


def test_zero_pause_keeps_zero_ordinary_joining_allowance():
    from pandrator.logic.dubbing.source_passage_policy import (
        DEFAULT_CUE_JOIN_GAP_MS,
        DEFAULT_DIAGNOSTIC_SPAN_MS,
    )
    assert DEFAULT_CUE_JOIN_GAP_MS == 650
    assert DEFAULT_DIAGNOSTIC_SPAN_MS == 8000
    cues, words = _timed_fixture(['Blum helped transform a local religious',
                                  'controversy into a public national issue.'], gaps=[200])
    assert len(build_source_passages(cues, words, pause_ms=650)) == 1
    cues, words = _timed_fixture(['Blum helped transform a local religious',
                                  'controversy into a public national issue.'], gaps=[200])
    assert len(build_source_passages(cues, words, pause_ms=0)) == 2
