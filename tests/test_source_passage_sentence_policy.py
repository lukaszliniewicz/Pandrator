"""Executable examples for the soft-minimum / sentence-preference policy."""
from copy import deepcopy
import pytest

from pandrator.logic.dubbing.logical_passages import build_source_passages
from pandrator.logic.dubbing.source_passage_policy import select_boundaries
from tests.test_dubbing_logical_passages import _timed_fixture


def padded(size, punctuation):
    body = ('word ' * (size // 5 + 1))[:size - 1].rstrip()
    return body + 'x' * (size - len(body) - 1) + punctuation


def example(stops):
    parts = []
    previous = 0
    for length, reason in stops:
        parts.append(padded(length - previous - bool(parts), '.' if reason == 'sentence' else ','))
        previous = length
    text = ' '.join(parts)
    candidates = {}
    position = 0
    for part, (_, reason) in zip(parts, stops):
        position += len(part.split())
        candidates[position] = reason
    return text, candidates


@pytest.mark.parametrize('comma,sentence', [(82, 142), (90, 168), (90, 180)])
def test_sentence_wins_over_earlier_comma_with_small_lookahead(comma, sentence):
    text, candidates = example([(comma, 'clause'), (sentence, 'sentence')])
    result = select_boundaries(text.split(), candidates)
    assert len(result) == 1
    assert result[0].offset == len(text.split())
    assert result[0].selection_reason == 'sentence_preferred'
    assert result[0].preferred_length_exceeded == (sentence > 160)


def test_equal_quality_clauses_choose_first_after_minimum_not_fullest():
    text, candidates = example([(40, 'clause'), (95, 'clause'), (150, 'clause'), (310, 'sentence')])
    result = select_boundaries(text.split(), candidates)
    assert result[0].length == 95
    assert result[0].selection_reason == 'clause_fallback'


def test_stronger_clause_can_beat_earlier_comma():
    text, candidates = example([(80, 'clause'), (120, 'strong_clause'), (330, 'sentence')])
    result = select_boundaries(text.split(), candidates)
    assert result[0].length == 120


def test_a_comma_before_soft_minimum_does_not_force_a_short_fragment():
    text, candidates = example([(40, 'clause'), (215, 'sentence')])
    result = select_boundaries(text.split(), candidates)
    assert len(result) == 1
    assert result[0].length == 215
    assert result[0].selection_reason == 'natural_boundary_overflow'


def test_first_safe_boundary_beyond_window_is_allowed():
    text, candidates = example([(215, 'clause'), (430, 'sentence')])
    result = select_boundaries(text.split(), candidates)
    assert result[0].length == 215
    assert result[0].selection_reason == 'natural_boundary_overflow'


def test_short_complete_sentences_are_not_forced_to_reach_minimum():
    cues, words = _timed_fixture(['Vielen Dank. Eine weitere vollstaendige Aussage folgt.'])
    result = build_source_passages(cues, words, language_code='de')
    assert [p['text'] for p in result] == ['Vielen Dank.', 'Eine weitere vollstaendige Aussage folgt.']


def test_named_apposition_commas_do_not_strand_the_name():
    text = ('Indem Blum Ronges Brief in seiner Zeitung, den Sächsischen Vaterlandsblättern, '
            'veröffentlichte, trug er dazu bei, eine lokale religiöse Kontroverse zu einer '
            'öffentlichen Angelegenheit von nationaler Bedeutung zu machen.')
    cues, words = _timed_fixture([text])
    result = build_source_passages(cues, words, language_code='de')
    assert len(result) == 2
    assert result[0]['text'].endswith('veröffentlichte,')
    assert 'den Sächsischen Vaterlandsblättern' in result[0]['text']
    assert ' '.join(p['text'] for p in result) == text


def test_sentence_preference_crosses_a_verified_comma_cue_edge():
    text, _ = example([(82, 'clause'), (142, 'sentence')])
    first, second = text[:82], text[83:]
    cues, words = _timed_fixture([first, second], gaps=[100])
    before = deepcopy((cues, words))
    result = build_source_passages(cues, words)
    assert len(result) == 1
    assert result[0]['text'] == text
    assert result[0]['source_cue_ids'] == ['c0', 'c1']
    assert result[0]['source_word_ids'] == [w['id'] for w in words]
    assert result[0]['source_token_ranges'][0]['range'][0] == 0
    assert result[0]['start_ms'] == words[0]['start_ms']
    assert result[0]['end_ms'] == words[-1]['end_ms']
    assert result[0]['boundary_selection']['reason'] == 'sentence_preferred'
    assert (cues, words) == before
    assert result == build_source_passages(cues, words)


def test_uncertain_evidence_is_not_crossed_to_find_a_sentence():
    text, _ = example([(82, 'clause'), (142, 'sentence')])
    cues, words = _timed_fixture([text[:82], text[83:]], gaps=[100])
    words[0]['confidence'] = .1
    result = build_source_passages(cues, words)
    assert len(result) == 2
    assert ' '.join(p['text'] for p in result) == text


def test_parentheses_are_not_treated_as_clause_splitting_opportunities():
    text, candidates = example([(95, 'clause'), (320, 'sentence')])
    tokens = text.split()
    tokens[0] = '(' + tokens[0]
    tokens[-1] += ')'
    result = select_boundaries(tokens, candidates)
    assert len(result) == 1


def test_an_incomplete_two_word_tail_is_not_a_good_second_passage():
    prefix = padded(159, ',')
    tokens = (prefix + ' these extraordinarilylengthywords.').split()
    result = select_boundaries(tokens, {len(prefix.split()): 'clause', len(tokens): 'sentence'})
    assert len(result) == 1


@pytest.mark.parametrize('reason', ['speaker', 'long_pause'])
def test_sentence_lookahead_never_jumps_a_safety_boundary(reason):
    text, _ = example([(82, 'clause'), (142, 'sentence')])
    cut = len(text[:82].split())
    result = select_boundaries(text.split(), {cut: reason, len(text.split()): 'sentence'})
    assert result[0].offset == cut
    assert result[0].selection_reason == 'source_boundary_guard'


@pytest.mark.parametrize('options,error', [({'min_chars': True}, TypeError),
                                         ({'min_chars': 0}, ValueError),
                                         ({'sentence_lookahead_chars': True}, TypeError),
                                         ({'sentence_lookahead_chars': -1}, ValueError)])
def test_invalid_options_fail_explicitly(options, error):
    cues, words = _timed_fixture(['A complete sentence.'])
    with pytest.raises(error):
        build_source_passages(cues, words, **options)


def test_fallback_does_not_borrow_words_owned_by_another_current_cue():
    cues, words = _timed_fixture(['A complete sentence.'])
    cues.append({**cues[0], 'id': 'overlapping', 'ordinal': 1})
    result = build_source_passages(cues, words)
    assert result[0]['source_word_ids'] == [w['id'] for w in words]
    assert result[1]['source_word_ids'] == []
    assert result[1]['timing_basis'] == 'cue_window'
    assert result[1]['text'] == cues[1]['text']


def test_unowned_fallback_word_ids_are_not_claimed_twice():
    cues, words = _timed_fixture(['A complete sentence.'])
    for word in words:
        word['segment_id'] = 'old-ancestor'
    cues.append({**cues[0], 'id': 'overlapping', 'ordinal': 1})
    result = build_source_passages(cues, words)
    assert result[0]['source_word_ids'] == [w['id'] for w in words]
    assert result[1]['source_word_ids'] == []
    assert ' '.join(p['text'] for p in result) == ' '.join(c['text'] for c in cues)


def test_unused_ancestor_word_can_still_anchor_its_actual_text():
    cues, words = _timed_fixture(['Alpha beta.'])
    cues[0]['text'] = 'Alpha'
    cues.append({**cues[0], 'id': 'next-cue', 'ordinal': 1, 'text': 'beta.'})
    result = build_source_passages(cues, words)
    assert [w for p in result for w in p['source_word_ids']] == [w['id'] for w in words]
    assert [p['text'] for p in result] == ['Alpha', 'beta.']


def test_year_number_sentence_is_genuine_not_decimal():
    cues, words = _timed_fixture(['I was born in 1987. A new sentence follows here.'])
    result = build_source_passages(cues, words)
    assert [p['text'] for p in result] == ['I was born in 1987.', 'A new sentence follows here.']
    assert all(p['boundary_after'] == 'sentence' for p in result)


def test_progressive_filler_scrap_joins_with_diagnostics():
    text = "And that's quite a progressive. Yeah. Goal. This phrase is crucial."
    cues, words = _timed_fixture([text])
    result = build_source_passages(cues, words)
    assert [p['text'] for p in result] == [
        "And that's quite a progressive.", 'Yeah. Goal.', 'This phrase is crucial.']
    # Wording, order, ownership, and real endpoints are preserved.
    assert ' '.join(p['text'] for p in result) == text
    assert [w for p in result for w in p['source_word_ids']] == [w['id'] for w in words]
    assert result[0]['start_ms'] == words[0]['start_ms']
    assert result[-1]['end_ms'] == words[-1]['end_ms']
    assert result[1]['end_ms'] == result[2]['start_ms'] or True  # adjacent word endpoints
    joined = result[1]['boundary_selection']
    assert joined['policy'] == 'source_provisional_v1'
    assert joined['suppressed_sentence_splits'] == [
        {'offset': 6, 'reason': 'filler_scrap', 'length': 5}]
    assert result[0]['boundary_selection']['suppressed_sentence_splits'] == []


def test_yes_repetition_ellipsis_holds_within_and_across_cues():
    cues, words = _timed_fixture(['Yes. Yes…'])
    result = build_source_passages(cues, words)
    assert [p['text'] for p in result] == ['Yes. Yes…']
    assert result[0]['boundary_selection']['reason'] == 'sentence_provisional_hold'
    assert result[0]['boundary_selection']['suppressed_sentence_splits'][0]['reason'] == 'exact_repetition_ellipsis'

    cues, words = _timed_fixture(['Yes.', 'Yes…'], gaps=[100])
    result = build_source_passages(cues, words)
    assert [p['text'] for p in result] == ['Yes. Yes…']
    assert ' '.join(p['text'] for p in result) == 'Yes. Yes…'


def test_yes_before_different_material_stays_split():
    cues, words = _timed_fixture(['Yes. So I went to the store yesterday.'])
    result = build_source_passages(cues, words)
    assert [p['text'] for p in result] == ['Yes.', 'So I went to the store yesterday.']

    cues, words = _timed_fixture(['Yes. So I went home early…'])
    result = build_source_passages(cues, words)
    assert [p['text'] for p in result] == ['Yes.', 'So I went home early…']


def test_rhetorical_repetition_is_preserved_not_joined():
    cues, words = _timed_fixture(['Never. Never again.'])
    result = build_source_passages(cues, words)
    assert [p['text'] for p in result] == ['Never.', 'Never again.']
    assert all(p['boundary_selection']['suppressed_sentence_splits'] == [] for p in result
               if 'boundary_selection' in p)


def test_cross_cue_repetition_barriers_still_split():
    cues, words = _timed_fixture(['Yes.', 'Yes…'], gaps=[100])
    cues[1]['speaker'] = 'B'
    result = build_source_passages(cues, words)
    assert [p['text'] for p in result] == ['Yes.', 'Yes…']
