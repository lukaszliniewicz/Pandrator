"""Sentence-first passage selection. Lengths are preferences, never forced cuts.

The caller supplies only boundaries supported by real word endpoints. This
module chooses among them without changing a word or manufacturing a timestamp.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence

DEFAULT_MIN_CHARS = 60
DEFAULT_PREFERRED_CHARS = 160
DEFAULT_SENTENCE_LOOKAHEAD_CHARS = 20
_HARD_REASONS = frozenset({"speaker", "long_pause"})
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
# Conservative protection, not a grammatical parser. Commas surrounding a
# named apposition must not cut a name away from its governing sentence.
_ARTICLES = frozenset("the a an der die das den dem des ein eine einer einem einen eines de het een le la les un une el los las il lo gli i o os as um uma ten ta to een".split())
_NAME_PARTICLES = frozenset("von van de del der den da di und and".split())
_DANGLING = frozenset("a an the of to with from and or der die das ein eine einer einem einen eines den dem des mit von zu und oder de het een van met en of le la les du des et ou el los las un una y o w z na do i oraz".split())


@dataclass(frozen=True)
class PassageBoundary:
    offset: int
    reason: str
    selection_reason: str
    length: int
    preferred_length_exceeded: bool


def unsafe_clause_offsets(tokens: Sequence[str]) -> set[int]:
    """Protect bracketed material, dangling function words and named appositions."""
    blocked: set[int] = set()
    depth = 0
    for index, token in enumerate(tokens):
        for char in token:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth = max(0, depth - 1)
        words = _WORD.findall(token)
        if depth or (words and words[-1].casefold() in _DANGLING):
            blocked.add(index + 1)
    commas = [index + 1 for index, token in enumerate(tokens)
              if token.rstrip('\"\'»”’)]}').endswith((",", "，", "،"))]
    for left, right in zip(commas, commas[1:]):
        middle = list(tokens[left:right])
        if not 2 <= len(middle) <= 8 or any(re.search(r"[.!?;:]", t) for t in middle):
            continue
        lexical = [_WORD.findall(t) for t in middle]
        words = [word for group in lexical for word in group]
        if not words or words[0].casefold() not in _ARTICLES:
            continue
        names = [word for word in words[1:] if word.casefold() not in _NAME_PARTICLES]
        if names and all(word[:1].isupper() for word in names):
            blocked.update((left, right))
    return blocked


def select_boundaries(
    tokens: Sequence[str], candidates: Mapping[int, str], *,
    min_chars: int = DEFAULT_MIN_CHARS,
    preferred_chars: int = DEFAULT_PREFERRED_CHARS,
    lookahead_chars: int = DEFAULT_SENTENCE_LOOKAHEAD_CHARS,
) -> list[PassageBoundary]:
    """Prefer the first sentence, then the best substantial clause, then overflow.

    Short complete sentences remain independent. Equal-quality clause candidates
    choose the earlier boundary, not the fullest block. Mandatory speaker/long
    interruption boundaries end the search and can never be jumped over.
    """
    if not tokens:
        return []
    prefix = [0]
    for token in tokens:
        prefix.append(prefix[-1] + len(token) + 1)

    def length(start: int, end: int) -> int:
        return max(0, prefix[end] - prefix[start] - 1)

    count = len(tokens)
    available = {**candidates}
    available.setdefault(count, "cue")
    ordered = sorted(available)
    blocked = unsafe_clause_offsets(tokens)
    result: list[PassageBoundary] = []
    start = 0
    while start < count:
        barrier = next((p for p in ordered if p > start and
                        (p == count or available[p] in _HARD_REASONS)), count)
        nearby = [p for p in ordered if start < p <= barrier]
        sentences = [p for p in nearby if available[p] == "sentence"]
        first_sentence = sentences[0] if sentences else None
        sentence_horizon = first_sentence or barrier

        def good_clause(p: int) -> bool:
            if p in blocked or p >= sentence_horizon or length(start, p) < min_chars:
                return False
            # An incomplete two-word tail is not a useful second passage.
            tail_tokens = tokens[p:sentence_horizon]
            tail_length = length(p, sentence_horizon)
            unspaced = not any(re.search(r"[A-Za-z]", t) for t in tail_tokens)
            return (tail_length >= min(20, min_chars) and
                    (len(tail_tokens) >= 3 or (unspaced and tail_length >= 10)))

        sentence_fits = first_sentence is not None and (
            length(start, first_sentence) <= preferred_chars + lookahead_chars
        )
        if sentence_fits:
            end, decision = first_sentence, "sentence_preferred"
        else:
            clauses = [p for p in nearby if available[p] in {"clause", "strong_clause", "conjunction"}
                       and good_clause(p)]
            in_window = [p for p in clauses if length(start, p) <= preferred_chars]
            if in_window:
                priority = {"strong_clause": 0, "clause": 1, "conjunction": 2}
                end = min(in_window, key=lambda p: (priority[available[p]], p))
                decision = "clause_fallback"
            else:
                # With no good candidate in the preference window, the first
                # genuinely usable candidate beyond it is allowed to be longer.
                future = clauses + ([first_sentence] if first_sentence else [])
                end = min(future) if future else barrier
                decision = ("natural_boundary_overflow" if length(start, end) > preferred_chars
                            else "source_boundary_guard")
        reason = available.get(end, "cue")
        if reason in _HARD_REASONS:
            decision = "source_boundary_guard"
        result.append(PassageBoundary(end, "clause" if reason == "strong_clause" else reason,
                                      decision, length(start, end), length(start, end) > preferred_chars))
        start = end
    return result
