"""Shared natural speech-boundary helpers for dubbing and voiceover planning.

This module is the single home for language-aware split decisions so that
speech-block sizing (``speech_blocks``) and early voiceover repair
(``early_repair``) agree on what counts as a speakable boundary.

A natural boundary is, in preference order:

1. a sentence terminal (``. ! ? …`` plus CJK equivalents),
2. a strong clause break (``; : — –`` plus CJK equivalents),
3. a weak clause break (comma, including CJK ``，``/``、``),
4. a conservative language-aware conjunction onset (``and``/``weil``/…).

Plain whitespace and hard character offsets are deliberately *not* natural
boundaries.  Callers that must respect a synthesis engine's hard character
cap keep whitespace as a clearly-flagged last resort and must surface an
actionable error instead of cutting mid-word.
"""

from __future__ import annotations

import re

from pandrator.constants import XTTS_LANGUAGES

from .languages import normalize_language_code

#: Languages with first-class synthesis support.  Conjunction tables cover
#: every entry; other codes fall back to base-code matching (``pt-br`` →
#: ``pt``, ``zh`` → ``zh-cn``) and finally to an empty conjunction list.
SUPPORTED_SYNTHESIS_LANGUAGES: tuple[str, ...] = tuple(XTTS_LANGUAGES)

#: Minimum characters required on each side of a repair/split fragment so
#: the planner never manufactures tiny one- or two-word tails.
MIN_NATURAL_FRAGMENT_CHARS = 20

CONJUNCTIONS: dict[str, list[str]] = {
    "en": [
        "and",
        "but",
        "or",
        "because",
        "although",
        "so",
        "while",
        "if",
        "then",
        "that",
        "as",
        "for",
        "since",
        "until",
        "whether",
    ],
    "es": [
        "y",
        "pero",
        "o",
        "porque",
        "aunque",
        "así",
        "mientras",
        "si",
        "entonces",
        "que",
        "como",
        "pues",
        "desde",
        "hasta",
    ],
    "fr": [
        "et",
        "mais",
        "ou",
        "parce que",
        "bien que",
        "donc",
        "pendant que",
        "si",
        "alors",
        "que",
        "comme",
        "car",
        "depuis",
        "jusqu'à",
    ],
    "de": [
        "und",
        "aber",
        "oder",
        "weil",
        "obwohl",
        "also",
        "während",
        "wenn",
        "dann",
        "dass",
        "als",
        "denn",
        "seit",
        "bis",
        "ob",
    ],
    "it": [
        "e",
        "ma",
        "o",
        "perché",
        "sebbene",
        "quindi",
        "mentre",
        "se",
        "allora",
        "che",
        "come",
        "poiché",
        "da quando",
        "fino a",
    ],
    "pt": [
        "e",
        "mas",
        "ou",
        "porque",
        "embora",
        "então",
        "enquanto",
        "se",
        "logo",
        "que",
        "como",
        "pois",
        "desde",
        "até",
    ],
    "pl": [
        "i",
        "ale",
        "lub",
        "ponieważ",
        "chociaż",
        "więc",
        "podczas gdy",
        "jeśli",
        "wtedy",
        "że",
        "jak",
        "gdyż",
        "od",
        "aż",
        "czy",
    ],
    "tr": [
        "ve",
        "ama",
        "veya",
        "çünkü",
        "rağmen",
        "bu yüzden",
        "iken",
        "eğer",
        "o zaman",
        "ki",
        "gibi",
        "zira",
    ],
    "ru": [
        "и",
        "но",
        "или",
        "потому что",
        "хотя",
        "так что",
        "пока",
        "если",
        "тогда",
        "что",
        "как",
        "ибо",
        "с",
        "до",
        "ли",
    ],
    "nl": [
        "en",
        "maar",
        "of",
        "omdat",
        "hoewel",
        "dus",
        "terwijl",
        "als",
        "dan",
        "dat",
        "zoals",
        "want",
        "sinds",
        "tot",
    ],
    "cs": [
        "a",
        "ale",
        "nebo",
        "protože",
        "ačkoli",
        "takže",
        "zatímco",
        "jestli",
        "pak",
        "že",
        "jako",
        "neboť",
        "od",
        "až",
        "zda",
    ],
    "hu": [
        "és",
        "de",
        "vagy",
        "mert",
        "bár",
        "tehát",
        "míg",
        "ha",
        "akkor",
        "hogy",
        "mint",
        "hiszen",
        "óta",
        "ameddig",
        "vajon",
    ],
    "ar": [
        "و",
        "لكن",
        "أو",
        "لأن",
        "رغم أن",
        "لذلك",
        "بينما",
        "إذا",
        "ثم",
        "أن",
        "كما",
        "ف",
        "منذ",
        "حتى",
        "هل",
    ],
    "zh-cn": [
        "和",
        "但是",
        "或者",
        "因为",
        "虽然",
        "所以",
        "当",
        "如果",
        "那么",
        "的",
        "作为",
        "由于",
        "从",
        "直到",
        "是否",
    ],
    "ja": [
        "そして",
        "しかし",
        "または",
        "なぜなら",
        "にもかかわらず",
        "だから",
        "もし",
        "その時",
        "と",
        "ように",
        "から",
        "以来",
        "まで",
        "かどうか",
    ],
    "ko": [
        "그리고",
        "하지만",
        "또는",
        "왜냐하면",
        "비록",
        "그래서",
        "동안",
        "만약",
        "그때",
        "것",
        "처럼",
        "때문에",
        "이후",
        "까지",
        "인지",
    ],
    "hi": [
        "और",
        "लेकिन",
        "या",
        "क्योंकि",
        "अगर",
        "तो",
        "कि",
        "इसलिए",
        "जबकि",
        "हालाँकि",
        "तथा",
        "परंतु",
    ],
}

SENTENCE_TERMINALS = frozenset(".!?…。！？")
STRONG_CLAUSE_MARKS = frozenset(";:\u2014\u2013；：")
WEAK_CLAUSE_MARKS = frozenset(",\u060c\uff0c\u3001")

_CLOSING_MARKS = frozenset("\"'”’»›)]}」』》〟〕〕）］｝〉〉")

_CLOSING_PATTERN = "[" + re.escape("".join(sorted(_CLOSING_MARKS))) + "]*"
_SENTENCE_END_RE = re.compile(
    "[" + re.escape("".join(sorted(SENTENCE_TERMINALS))) + "]" + _CLOSING_PATTERN
)
_STRONG_END_RE = re.compile(
    "[" + re.escape("".join(sorted(STRONG_CLAUSE_MARKS))) + "]" + _CLOSING_PATTERN
)
_WEAK_END_RE = re.compile(
    "[" + re.escape("".join(sorted(WEAK_CLAUSE_MARKS))) + "]" + _CLOSING_PATTERN
)


def resolve_speech_language(language: object, default: str = "en") -> str:
    """Resolve a user-facing language to a speech-planning code.

    Exact table hits win; otherwise the base code is tried (``pt-br`` →
    ``pt``, ``en-us`` → ``en``) with ``zh`` mapping to ``zh-cn``.  Unknown
    codes are returned unchanged so downstream guards (sentence-splitter
    language sets, empty conjunction lists) keep working.
    """

    code = normalize_language_code(str(language or ""), default=default)
    if not code:
        return code
    if code in CONJUNCTIONS:
        return code
    base = code.split("-")[0]
    if base in CONJUNCTIONS:
        return base
    if base == "zh":
        return "zh-cn"
    return code


def conjunctions_for(language: object) -> list[str]:
    """Return the conservative conjunction list for a language."""

    return list(CONJUNCTIONS.get(resolve_speech_language(language, default=""), []))


def _resolve_language_argument(
    language: object = None, language_code: object = None, default: str = "en"
) -> str:
    """Prefer an explicit ``language_code`` keyword, else ``language``."""

    if language_code is not None:
        return resolve_speech_language(language_code, default=default)
    return resolve_speech_language(language, default=default)


def conjunction_split_positions(
    text: str, language: object = None, *, language_code: object = None,
    safe_only: bool = True,
) -> list[int]:
    """Return whole-word conjunction onset offsets usable as split points.

    By default only the conservative :data:`SAFE_CLAUSE_CONJUNCTIONS` set is
    used, so bare coordinators inside noun phrases (``fish and chips``,
    ``Tom und Jerry``) never license a cut on their own.  Pass
    ``safe_only=False`` for the broader table.  Returned offsets never
    rewrite text; they only mark where a cut may fall, preserving the
    authored canonical wording.
    """

    value = str(text or "")
    if not value.strip():
        return []
    resolved = _resolve_language_argument(language, language_code, default="")
    table = SAFE_CLAUSE_CONJUNCTIONS if safe_only else CONJUNCTIONS
    spans: list[tuple[int, int]] = []
    for conjunction in table.get(resolved, []):
        pattern = r"(?<!\w)" + re.escape(conjunction) + r"(?!\w)"
        for match in re.finditer(pattern, value, re.IGNORECASE):
            start, end = match.start(), match.end()
            if 0 < start < len(value):
                if safe_only and not starts_with_safe_conjunction(value[start:], resolved):
                    continue
                spans.append((start, end))
    # Keep the longest match when conjunctions overlap ("parce que" wins
    # over its standalone "que" tail).
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    positions: list[int] = []
    last_end = -1
    for start, end in spans:
        if start < last_end:
            continue
        positions.append(start)
        last_end = end
    return sorted(set(positions))


_COMMA_LED_PRECEDERS = frozenset(",;:\u2014\u2013；：")


ABBREVIATIONS = frozenset(
    {
        "a.m",
        "approx",
        "asst",
        "dept",
        "dr",
        "e.g",
        "etc",
        "fig",
        "i.e",
        "jr",
        "mr",
        "mrs",
        "ms",
        "no",
        "prof",
        "rev",
        "sr",
        "st",
        "u.s",
        "vs",
    }
)


def period_is_non_boundary(value: str, following: str) -> bool:
    """Return True when a trailing period is not a sentence end.

    Decimal points (``3.14``) and abbreviations (``Dr.``, ``e.g.``) must
    never be treated as speakable boundaries.
    """

    before_period = value[:-1].rstrip()
    next_character = following.lstrip()[:1]
    if before_period and before_period[-1].isdigit() and next_character.isdigit():
        return True

    match = re.search(r"(?<!\w)([A-Za-z](?:[A-Za-z.]*[A-Za-z])?)$", before_period)
    if match is None:
        return False
    token = match.group(1).casefold()
    return token in ABBREVIATIONS or len(token) == 1


#: Subordinating conjunctions that reliably open a new clause.  This set is
#: intentionally narrower than CONJUNCTIONS: bare coordinators (and/und/or,
#: that/as/for and their equivalents) often join noun phrases ("fish and
#: chips") and must never license a cut on their own.
SAFE_CLAUSE_CONJUNCTIONS: dict[str, list[str]] = {
    "en": [
        "because",
        "although",
        "though",
        "whereas",
        "unless",
        "whether",
        "if",
    ],
    "es": ["porque", "aunque", "mientras", "si", "cuando"],
    "fr": ["parce que", "bien que", "puisque", "pendant que", "lorsque"],
    "de": ["weil", "obwohl", "obschon", "wenn", "falls"],
    "it": ["perché", "sebbene", "mentre", "quando"],
    "pt": ["porque", "embora", "enquanto", "quando"],
    "pl": ["ponieważ", "chociaż", "jeśli", "skoro"],
    "tr": ["çünkü", "eğer"],
    "ru": ["потому что", "хотя", "если", "пока"],
    "nl": ["omdat", "hoewel", "terwijl"],
    "cs": ["protože", "ačkoli", "jestli"],
    "hu": ["mert", "bár", "ha", "míg"],
    "ar": ["لأن", "رغم أن", "بينما", "إذا"],
    "zh-cn": ["因为", "虽然", "如果", "当"],
    "ja": ["なぜなら", "もし"],
    "ko": ["왜냐하면", "만약"],
    "hi": ["क्योंकि", "अगर", "जबकि", "हालाँकि"],
}


def safe_conjunctions_for(language: object) -> list[str]:
    """Return the conservative clause-opening conjunction list."""

    return list(
        SAFE_CLAUSE_CONJUNCTIONS.get(
            _resolve_language_argument(language, default=""), []
        )
    )


def conjunction_tiers(
    text: str, language: object = None, *, language_code: object = None,
    safe_only: bool = True,
) -> dict[int, str]:
    """Tier each conjunction onset as ``comma_led`` or ``bare``.

    A comma-led onset (``…, because …``) is an explicit breath mark left by
    the author and safe to split before.  A bare onset may merely coordinate
    a noun phrase, so planners must treat it conservatively: a small bonus
    at most, and only with substantial text on both sides.
    """

    value = str(text or "")
    tiers: dict[int, str] = {}
    for position in conjunction_split_positions(
        value, language, language_code=language_code, safe_only=safe_only
    ):
        preceding = value[:position].rstrip()
        tiers[position] = (
            "comma_led"
            if preceding and preceding[-1] in _COMMA_LED_PRECEDERS
            else "bare"
        )
    return tiers


def starts_with_safe_conjunction(
    text: str, language: object = None, *, language_code: object = None
) -> bool:
    """Return True when text opens with a safe clause-opening conjunction."""

    value = str(text or "").lstrip()
    if not value:
        return False
    resolved = _resolve_language_argument(language, language_code, default="")
    for conjunction in SAFE_CLAUSE_CONJUNCTIONS.get(resolved, []):
        match = re.match(
            r"(?<!\w)" + re.escape(conjunction) + r"(?!\w)",
            value,
            re.IGNORECASE,
        )
        if match is not None:
            # This use of because introduces a noun phrase, not a clause.
            if resolved == "en" and conjunction == "because" and re.match(
                r"\s+of\b", value[match.end():], re.IGNORECASE
            ):
                continue
            return True
    return False


def _sentence_end_is_genuine(text: str, end: int) -> bool:
    """Return False when a ``.`` offset is a decimal or abbreviation."""

    index = end - 1
    while index >= 0 and text[index] in _CLOSING_MARKS:
        index -= 1
    if index < 0 or text[index] != ".":
        return True
    if text[end:end + 1] == ".":
        return False  # Keep an ellipsis together.
    if (index > 0 and text[index - 1].isalnum()
            and text[end:end + 1].isalnum()):
        return False  # Decimal, URL, or another token-internal period.
    return not period_is_non_boundary(text[: index + 1], text[end:])


def strip_closing_marks(value: str) -> str:
    """Strip trailing quotation/bracket marks to reveal terminal punctuation."""

    result = str(value or "").rstrip()
    while result and result[-1] in _CLOSING_MARKS:
        result = result[:-1].rstrip()
    return result


def boundary_strength_rank(terminal: str) -> int | None:
    """Rank a terminal punctuation mark: sentence 0, strong 1, weak 2."""

    if not terminal:
        return None
    if terminal in SENTENCE_TERMINALS:
        return 0
    if terminal in STRONG_CLAUSE_MARKS:
        return 1
    if terminal in WEAK_CLAUSE_MARKS:
        return 2
    return None


def classify_boundary(
    left: str, right: str, *, language_code: object = None
) -> int | None:
    """Rank the boundary between two text fragments, if any.

    Sentence terminals rank 0, strong clause marks 1, commas 2, and a safe
    clause-conjunction seam (``… because …`` with no punctuation) ranks 3.
    ``language_code`` selects the safe-conjunction policy.  Decimal points
    and abbreviation periods never count as boundaries.  Text is never
    rewritten.
    """

    lang = _resolve_language_argument(language_code=language_code, default="en")
    right_text = str(right or "")
    stripped = strip_closing_marks(left)
    if stripped:
        terminal = stripped[-1]
        rank = boundary_strength_rank(terminal)
        if rank is not None:
            numeric_separator = (
                terminal in ".,:，：" and len(stripped) > 1
                and stripped[-2].isdigit() and right_text.lstrip()[:1].isdigit()
            )
            if not numeric_separator and (
                terminal != "." or not period_is_non_boundary(stripped, right_text)
            ):
                return rank
    if starts_with_safe_conjunction(right_text, language_code=lang):
        return 3
    return None


def natural_split_candidates(
    text: str, language: object = None, *, language_code: object = None
) -> list[tuple[int, str]]:
    """List ``(offset, kind)`` natural split points in preference order.

    Offsets sit just past the boundary punctuation (including any closing
    marks) or at a safe clause-conjunction onset.  Kinds are ``sentence``,
    ``strong``, ``comma``, or ``conjunction``.  Decimal points and
    abbreviation periods are excluded.  Conjunction onsets are un-tiered
    here; use :func:`conjunction_tiers` to keep bare noun-phrase
    coordinations conservative.
    """

    value = str(text or "")
    candidates: dict[int, str] = {}
    for match in _SENTENCE_END_RE.finditer(value):
        if not _sentence_end_is_genuine(value, match.end()):
            continue
        candidates.setdefault(match.end(), "sentence")
    for match in _STRONG_END_RE.finditer(value):
        if classify_boundary(value[:match.end()], value[match.end():], language_code=language_code) is not None:
            candidates.setdefault(match.end(), "strong")
    for match in _WEAK_END_RE.finditer(value):
        if classify_boundary(value[:match.end()], value[match.end():], language_code=language_code) is not None:
            candidates.setdefault(match.end(), "comma")
    for position in conjunction_split_positions(
        value, language, language_code=language_code
    ):
        candidates.setdefault(position, "conjunction")
    rank = {"sentence": 0, "strong": 1, "comma": 2, "conjunction": 3}
    return sorted(candidates.items(), key=lambda item: (rank[item[1]], item[0]))
