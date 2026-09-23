"""Conservative SOURCE-only provisional-ASR sentence assessment.

This helper answers one narrow question for source segmentation: does the
text immediately left of a word-timed seam end in a *complete* sentence, or
in an unfinished/hesitation fragment?  It is used consistently by both source
stages (cue-seam run building and in-run boundary selection) so a seam is
never split as a "sentence" in one stage and held as "unfinished" in the
other.

Deliberately SOURCE-only and conservative:

- A trailing ellipsis (U+2026 ``…``, ``..``/``...``, U+2025 ``‥``) is an
  incomplete hesitation (``It's…``, ``And…``, ``Yes…``), never a complete
  sentence.  The shared target TTS classifier in ``natural_boundaries``
  still ranks ``…`` as a sentence terminal; this module does not change it.
- Repetition (``Yes. Yes…``, ``Never. Never again.``) is reported as a
  structural diagnostic only.  Wording and punctuation are never removed or
  rewritten here.
- Language handling uses normalized aliases.  First-class synthesis
  languages get abbreviation protection; unknown/mixed/unsupported codes
  fall back to universal guards only (decimal ``3.14`` and single-letter
  initials) and never silently apply English conjunction/abbreviation
  grammar.  Conjunction onsets are not sentence boundaries in any language
  here; that stays with the shared classifier (ranks 1-3, untouched).
- Speaker changes, overlaps, and long interruptions remain hard barriers in
  the callers; this module never overrides them.

Policy version: :data:`SOURCE_PASSAGE_POLICY_VERSION`.
"""

from __future__ import annotations

import re

from .languages import normalize_language_code
from .natural_boundaries import (
    ABBREVIATIONS as _SHARED_ABBREVIATIONS,
)
from .natural_boundaries import (
    CONJUNCTIONS as _SHARED_CONJUNCTIONS,
)
from .natural_boundaries import (
    SUPPORTED_SYNTHESIS_LANGUAGES,
    starts_with_safe_conjunction,
)

#: Explicit SOURCE policy version.  Stored per row in
#: ``boundary_selection.policy`` / ``policy_version`` so replays and the
#: settings/API/UI agent can tell which assessment produced a passage.
SOURCE_PASSAGE_POLICY_VERSION = "source_provisional_v1"

#: Setting names whose values are recorded per row in ``boundary_selection``.
#: Numeric defaults live with the caller (60 min / 160 preferred / +20
#: lookahead / 8000 ms span / 650 ms pause); this tuple only names them for
#: provenance so a second agent can coordinate exact keys.
SOURCE_POLICY_SETTING_KEYS = (
    "soft_min_chars",
    "preferred_chars",
    "sentence_lookahead_chars",
    "max_span_ms",
    "pause_ms",
    "language_code",
)

#: Languages whose Latin abbreviation protection is applied.  Unknown, mixed
#: or CJK codes use universal guards only (see module docstring).
_LATIN_ABBREVIATION_LANGUAGES = frozenset({
    "en", "de", "fr", "es", "it", "pt", "pl", "nl", "cs", "hu",
})

_COMPLETE_TERMINALS = frozenset(".!?。！？")
_ELLIPSIS_CHARS = frozenset("…‥")
_CLOSING_MARKS = frozenset("\"'”’“‘»›«‹)]}」』》〟〕）］｝〉")
_ALNUM_TAIL_RE = re.compile(r"[^\W_]+", re.UNICODE)
_INITIAL_RE = re.compile(r"^[A-Za-z]$")


def resolve_source_language(language_code: object) -> str:
    """Normalize a source language alias (``English``/``en-US``/``zh``...).

    Mirrors the shared base-code fallback (``en-us`` -> ``en``,
    ``pt-br`` -> ``pt``, ``zh`` -> ``zh-cn``) without routing assessment
    through the TTS classifier.  Unknown codes pass through unchanged so
    callers stay conservative instead of silently assuming English.
    """
    normalized = normalize_language_code(str(language_code or ""), default="")
    if not normalized:
        return ""
    if normalized in _SHARED_CONJUNCTIONS:
        return normalized
    base = normalized.split("-")[0]
    if base in _SHARED_CONJUNCTIONS:
        return base
    if base == "zh":
        return "zh-cn"
    return normalized


def is_supported_source_language(language_code: object) -> bool:
    """Return True for first-class synthesis languages, else conservative."""
    resolved = resolve_source_language(language_code)
    if not resolved:
        return False
    if resolved in SUPPORTED_SYNTHESIS_LANGUAGES:
        return True
    return resolved in _LATIN_ABBREVIATION_LANGUAGES


def _strip_closings(value: str) -> str:
    result = value.rstrip()
    while result and result[-1] in _CLOSING_MARKS:
        result = result[:-1].rstrip()
    return result


def token_ends_ellipsis(tokens, position: int) -> bool:
    """Return True when the token closing ``position`` ends in ellipsis.

    Walks back over standalone closing-mark tokens (``”``).  Used to demote
    caller-supplied ``sentence`` labels that are unambiguously hesitations;
    abbreviation/initial/decimal filtering stays at candidate-generation
    time so synthetic single-letter tails (``w.'') cannot trip it.
    """
    index = position - 1
    while index >= 0 and tokens[index] and all(
        char in _CLOSING_MARKS for char in tokens[index]
    ):
        index -= 1
    if index < 0:
        return False
    stripped = _strip_closings(tokens[index])
    return (bool(stripped) and (stripped[-1] in _ELLIPSIS_CHARS
            or stripped.endswith("...") or stripped.endswith("..")))


def left_sentence_kind(left_text: str, language_code: object = "en") -> str:
    """Classify text left of a seam: ``complete`` | ``ellipsis`` | ``none``.

    ``ellipsis`` covers ``…``/``‥``/``...`` hesitations (``It's…``) which
    the shared TTS classifier ranks as sentence terminals but which source
    segmentation must treat as unfinished.  ``complete`` requires a genuine
    ``. ! ?`` (or CJK ``。！？``) terminal that survives the decimal,
    initial, and (supported Latin languages only) abbreviation guards.
    """
    stripped = _strip_closings(str(left_text or ""))
    if not stripped:
        return "none"
    if stripped[-1] in _ELLIPSIS_CHARS:
        return "ellipsis"
    if stripped.endswith("...") or stripped.endswith(".."):
        return "ellipsis"
    if stripped[-1] not in _COMPLETE_TERMINALS:
        return "none"
    terminal = stripped[-1]
    if terminal == "." and _period_is_protected(stripped, language_code):
        return "none"
    return "complete"


def _period_is_protected(stripped: str, language_code: object) -> bool:
    """Return True when a trailing period is not a sentence end (SOURCE).

    Year/number sentences (``I was born in 1987.``) are genuine: a trailing
    digit never protects a period on its own.  Only a digit on *both* sides
    (``3.14``) guards the decimal, checked follower-aware in
    :func:`is_genuine_source_sentence`.  Bare initials (``J.``) and, for
    supported Latin languages only, abbreviations (``Dr.``) still protect.
    """
    before = stripped[:-1].rstrip()
    if not before:
        return True
    # Universal: a bare initial (J.) is never a sentence boundary, whatever
    # the language.  Digits are deliberately NOT guarded here.
    match = re.search(r"(?<!\w)([A-Za-z](?:[A-Za-z.]*[A-Za-z])?)$", before)
    if match is not None:
        token = match.group(1).casefold()
        if len(token) == 1:
            return True
        if _INITIAL_RE.fullmatch(match.group(1)):
            return True
        # English-style abbreviation protection applies only to supported
        # Latin languages; unknown/mixed codes must not silently inherit
        # English grammar (documented limitation: e.g. "Dr." in an
        # unidentified language may split; pass the real language instead).
        resolved = resolve_source_language(language_code)
        if resolved in _LATIN_ABBREVIATION_LANGUAGES and token in _SHARED_ABBREVIATIONS:
            return True
    return False


def is_genuine_source_sentence(
    left_text: str, right_text: str = "", language_code: object = "en",
) -> bool:
    """Return True only for a complete SOURCE sentence terminal.

    Adds the follower-aware decimal guard (``3.`` + ``14``) on top of
    :func:`left_sentence_kind`.  Ellipses, bare conjunction onsets, and
    unsupported-language English-grammar matches never count.
    """
    kind = left_sentence_kind(left_text, language_code)
    if kind != "complete":
        return False
    stripped = _strip_closings(str(left_text or ""))
    if not stripped or stripped[-1] != ".":
        return True
    follower = str(right_text or "").lstrip()[:1]
    before = stripped[:-1].rstrip()
    if before and before[-1].isdigit() and follower.isdigit():
        return False
    return True


def repetition_kind(left_text: str, right_text: str) -> str | None:
    """Report structural repetition without touching wording/punctuation.

    Returns ``"exact"`` (``Yes.`` -> ``Yes…``), ``"rhetorical_extension"``
    (``Never.`` -> ``Never again.``), or None.  Diagnostics only: callers
    use it to *preserve* rhetorical splits, never to remove repetition.
    """
    left_words = [w.casefold() for w in _ALNUM_TAIL_RE.findall(left_text or "")]
    right_words = [w.casefold() for w in _ALNUM_TAIL_RE.findall(right_text or "")]
    if not left_words or not right_words:
        return None
    if left_words == right_words:
        return "exact"
    if len(right_words) > len(left_words) and right_words[: len(left_words)] == left_words:
        return "rhetorical_extension"
    if len(left_words) <= 2 and len(right_words) <= 4 and left_words[0] == right_words[0]:
        return "rhetorical_extension"
    return None


def follower_is_fragmentary(tail_text: str, language_code: object = "en") -> bool:
    """Return True when a tail holds no genuine complete SOURCE sentence.

    Used to decide whether a short leading fragment (``Yes.``) is followed
    by a real new sentence (preserve the split, e.g. ``Hello world. Next
    phrase.``) or by an unfinished scrap (hold the split, e.g. ``Yes.``
    before ``Yes…``).  Scans sentence-terminal positions in the tail with
    the same ellipsis/abbreviation guards as the seam check.
    """
    tail = str(tail_text or "")
    if not tail.strip():
        return True
    # Walk candidate terminal positions: any .!?。！？ possibly followed by
    # closings and then a boundary/end.  Ellipses never count.
    index = 0
    while index < len(tail):
        char = tail[index]
        if char in _ELLIPSIS_CHARS:
            index += 1
            continue
        if char in _COMPLETE_TERMINALS:
            # Skip ... runs together (ellipsis, not a terminal).
            run_start = index
            while index + 1 < len(tail) and tail[index + 1] == char == ".":
                index += 1
            if char == "." and index > run_start:
                index += 1
                continue
            left = tail[: index + 1]
            right = tail[index + 1 :]
            if is_genuine_source_sentence(left, right, language_code):
                return False
        index += 1
    return True


#: Bounded per-language filler/backchannel words for the scrap-join rule.
#: A filler-only short sentence (``Yeah.'') may join a following short
#: scrap (``Goal.''); content sentences never join on this rule.  Languages
#: without an entry (unknown/mixed/CJK) get no filler rule at all rather
#: than inheriting English backchannels.
FILLER_WORDS: dict[str, frozenset] = {
    "en": frozenset({"yeah", "yes", "yep", "yup", "uh", "um", "erm", "er",
                      "oh", "ah", "okay", "ok", "well", "so", "no", "hi", "hey"}),
    "de": frozenset({"ja", "nein", "nee", "äh", "ähm", "oh", "also", "na", "okay"}),
    "fr": frozenset({"oui", "non", "euh", "ben", "oh", "okay"}),
    "es": frozenset({"sí", "no", "eh", "pues", "bueno", "vale"}),
    "it": frozenset({"sì", "no", "eh", "beh", "oh"}),
    "pt": frozenset({"sim", "não", "nao", "eh", "pois", "bem"}),
    "pl": frozenset({"tak", "nie", "yhm", "ehm", "no", "okej"}),
    "nl": frozenset({"ja", "nee", "eh", "nou", "oh"}),
    "cs": frozenset({"ano", "ne", "ehm", "no"}),
    "hu": frozenset({"igen", "nem", "öhm", "na"}),
    "ru": frozenset({"да", "нет", "э", "ну"}),
    "tr": frozenset({"evet", "hayır", "hmm", "şey"}),
    "ar": frozenset({"نعم", "لا", "هم"}),
    "hi": frozenset({"हाँ", "नहीं", "अच्छा"}),
}

#: Maximum words for a follower scrap in the filler-scrap join rule.
FILLER_SCRAP_MAX_WORDS = 3

_STRONG_MARKS = frozenset(";:—–；：")
_WEAK_MARKS = frozenset(",，、،")
_NUMERIC_SEPARATORS = frozenset(".,:，：")


def is_filler_sentence(text: str, language_code: object = "en") -> bool:
    """Return True when every word is a bounded filler for the language."""
    resolved = resolve_source_language(language_code)
    fillers = FILLER_WORDS.get(resolved)
    if not fillers:
        return False
    words = [word.casefold() for word in _ALNUM_TAIL_RE.findall(text or "")]
    return bool(words) and all(word in fillers for word in words)


def source_clause_rank(
    left_text: str, right_text: str = "", language_code: object = "en",
) -> int | None:
    """Rank a non-sentence seam SOURCE-side: strong 1, weak 2, conjunction 3.

    Punctuation ranks mirror the shared sets (including CJK) with a
    digit-guard for numeric separators.  Conjunction onsets (rank 3) apply
    only for supported languages via the shared safe list; unknown/mixed
    codes never inherit English conjunction grammar.  Sentence terminals
    are NOT ranked here; use :func:`is_genuine_source_sentence`.
    """
    stripped = _strip_closings(str(left_text or ""))
    follower = str(right_text or "").lstrip()[:1]
    if stripped:
        terminal = stripped[-1]
        if terminal in _STRONG_MARKS or terminal in _WEAK_MARKS:
            if (terminal in _NUMERIC_SEPARATORS and len(stripped) > 1
                    and stripped[-2].isdigit() and follower.isdigit()):
                return None
            return 1 if terminal in _STRONG_MARKS else 2
    resolved = resolve_source_language(language_code)
    if resolved and resolved in SUPPORTED_SYNTHESIS_LANGUAGES:
        if starts_with_safe_conjunction(str(right_text or ""), language_code=resolved):
            return 3
    return None


def head_until_genuine(text: str, language_code: object = "en") -> str:
    """Return the follower head up to (not incl.) its first genuine sentence."""
    tail = str(text or "")
    index = 0
    while index < len(tail):
        char = tail[index]
        if char in _ELLIPSIS_CHARS:
            index += 1
            continue
        if char in _COMPLETE_TERMINALS:
            run_start = index
            while index + 1 < len(tail) and tail[index + 1] == char == ".":
                index += 1
            if char == "." and index > run_start:
                index += 1
                continue
            left, right = tail[: index + 1], tail[index + 1 :]
            if is_genuine_source_sentence(left, right, language_code):
                return tail[: index + 1]
        index += 1
    return tail


def _head_has_ellipsis(head: str) -> bool:
    stripped = head.strip()
    return ("…" in stripped or "‥" in stripped or "..." in stripped
            or stripped.endswith(".."))


def hold_short_sentence_reason(
    leader_text: str,
    follower_head: str,
    language_code: object = "en",
    min_chars: int = 60,
) -> str | None:
    """Decide whether a short genuine leader joins its follower head.

    Bounded structures only; returns a reason or None (preserve the split):

    - ``"exact_repetition_ellipsis"``: the follower head repeats the
      leader's words (exactly, or as an extension like ``Yes`` ->
      ``Yes… kindly``) and carries an ellipsis hesitation (``Yes. Yes…``).
      Period-ended rhetorical repetition (``Never. Never again.'') has no
      ellipsis and is preserved.
    - ``"filler_scrap"``: the leader is filler-only for its language
      (``Yeah.'') and the follower head is short (under ``min_chars``)
      and either holds no genuine sentence and is itself filler-only
      (``Yeah. um…'') or its first genuine sentence is a tiny scrap of
      at most :data:`FILLER_SCRAP_MAX_WORDS` words (``Yeah. Goal.'').
      Content leaders (``And that's quite a progressive.'') and
      substantial or different followers (``so I went to the store.'')
      never join on this rule.

    Unknown/mixed languages get no filler rule; repetition still applies
    since it is structural, not grammatical.
    """
    leader = str(leader_text or "")
    head = str(follower_head or "")
    if len(leader) >= min_chars or not head.strip():
        return None
    repetition = repetition_kind(leader, head)
    if (repetition in ("exact", "rhetorical_extension")
            and _head_has_ellipsis(head)):
        return "exact_repetition_ellipsis"
    if not is_filler_sentence(leader, language_code):
        return None
    if follower_is_fragmentary(head, language_code):
        if len(head) < min_chars and is_filler_sentence(head, language_code):
            return "filler_scrap"
        return None
    tiny = head_until_genuine(head, language_code)
    if (len(tiny) < min_chars
            and len(_ALNUM_TAIL_RE.findall(tiny)) <= FILLER_SCRAP_MAX_WORDS):
        return "filler_scrap"
    return None


__all__ = [
    "FILLER_SCRAP_MAX_WORDS",
    "FILLER_WORDS",
    "SOURCE_PASSAGE_POLICY_VERSION",
    "SOURCE_POLICY_SETTING_KEYS",
    "head_until_genuine",
    "hold_short_sentence_reason",
    "is_filler_sentence",
    "is_genuine_source_sentence",
    "is_supported_source_language",
    "follower_is_fragmentary",
    "left_sentence_kind",
    "repetition_kind",
    "resolve_source_language",
    "source_clause_rank",
    "token_ends_ellipsis",
]
