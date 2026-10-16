"""Conservative punctuation-only dialogue paragraph recognition.

This module identifies source paragraphs that are likely spoken dialogue.  It
does not assign speakers, alter text, or infer delivery.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import regex

from .dubbing.languages import normalize_language_code

_COMMON_PAIRS: tuple[tuple[str, str], ...] = (
    ('"', '"'),
    ("“", "”"),
    ("‘", "’"),
    ("„", "“"),
    ("„", "”"),
    ("‚", "‘"),
    ("«", "»"),
    ("»", "«"),
    ("‹", "›"),
    ("›", "‹"),
    ("「", "」"),
    ("『", "』"),
)

_QUOTE_PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "en": (('"', '"'), ("“", "”"), ("‘", "’"), ("'", "'")),
    "de": (('"', '"'), ("„", "“"), ("»", "«"), ("‚", "‘")),
    "pl": (('"', '"'), ("„", "”")),
    "fr": (('"', '"'), ("«", "»"), ("“", "”")),
    "nl": (('"', '"'), ("“", "”"), ("‘", "’"), ("„", "”")),
    "ja": (('"', '"'), ("「", "」"), ("『", "』")),
    "generic": _COMMON_PAIRS,
}

_REPORTING_VERBS: dict[str, tuple[str, ...]] = {
    "en": (
        "said", "asked", "replied", "answered", "called", "cried",
        "whispered", "murmured", "exclaimed", "continued", "added",
        "remarked", "observed", "returned", "retorted", "rejoined",
        "insisted", "declared", "resumed",
    ),
    "de": (
        "sagte", "fragte", "antwortete", "erwiderte", "rief",
        "flüsterte", "murmelte", "meinte", "entgegnete", "fügte hinzu",
    ),
    "pl": (
        "powiedział", "powiedziała", "powiedzieli", "powiedziałem",
        "powiedziałam", "spytał", "spytała", "zapytał", "zapytała",
        "odpowiedział", "odpowiedziała", "odparł", "odparła", "zawołał",
        "zawołała", "krzyknął", "krzyknęła", "wyszeptał", "wyszeptała",
        "szepnął", "szepnęła", "mruknął", "mruknęła",
    ),
    "fr": (
        "dit", "demanda", "répondit", "répliqua", "cria", "murmura",
        "chuchota", "souffla", "s'exclama",
    ),
    "nl": (
        "zei", "vroeg", "antwoordde", "reageerde", "riep", "fluisterde",
        "mompelde", "merkte op",
    ),
}

_REPORTING_PRONOUNS: dict[str, tuple[str, ...]] = {
    "en": ("he", "she", "they", "i", "we", "you"),
    "de": ("er", "sie", "es", "ich", "wir", "du", "ihr"),
    "pl": ("on", "ona", "ono", "oni", "ja", "my", "ty", "wy"),
    "fr": ("il", "elle", "ils", "elles", "je", "nous", "tu", "vous"),
    "nl": ("hij", "zij", "ze", "ik", "wij", "je", "jij", "u"),
}

_REPORTING_ARTICLES: dict[str, tuple[str, ...]] = {
    "en": ("the", "a", "an"),
    "de": ("der", "die", "das", "ein", "eine"),
    "pl": ("ten", "ta", "to", "pan", "pani"),
    "fr": ("le", "la", "les", "un", "une"),
    "nl": ("de", "het", "een"),
}

_DASH_LANGUAGES = frozenset({"pl", "fr"})
_MAX_DIALOGUE_PARAGRAPH_CHARS = 600
_MAX_REPORTING_TAG_CHARS = 120
_MAX_FRENCH_OPENING_DASH_REPLIES = 64
_SPEAKER_WORD = r"[\p{L}\p{M}][\p{L}\p{M}'’\-]*"
_CAPITALIZED_NAME = r"\p{Lu}[\p{L}\p{M}\-]*"
_ENGLISH_PERSON = re.compile(r"\b(?:i|we|us|our|ours|you|your|yours|me|my|mine)\b", re.I)
_ENGLISH_REPLY = re.compile(
    r'''^[“‘"']\s*(?:yes|no|hello|hi|thanks|thank you|sorry|okay|ok|agreed|certainly|indeed|well)\s*[,!?.…“”‘’"']''',
    re.I,
)


def _rule_language(language: object) -> str:
    code = normalize_language_code(str(language or ""), default="")
    base = code.split("-", 1)[0]
    return base if base in _QUOTE_PAIRS and base != "generic" else "generic"


def _is_internal_apostrophe(text: str, index: int) -> bool:
    return (
        0 < index < len(text) - 1
        and text[index - 1].isalnum()
        and text[index + 1].isalnum()
    )


def _find_balanced_quote_end(
    text: str, pairs: tuple[tuple[str, str], ...]
) -> int | None:
    if not text:
        return None
    opening_to_closing: dict[str, list[str]] = {}
    for opening, closing in pairs:
        opening_to_closing.setdefault(opening, []).append(closing)
    closing_marks = {closing for _opening, closing in pairs}
    first = text[0]
    if first not in opening_to_closing:
        return None

    stack = [set(opening_to_closing[first])]
    index = 1
    while index < len(text):
        character = text[index]
        if character in {"'", "‘", "’"} and _is_internal_apostrophe(text, index):
            index += 1
            continue

        if character in stack[-1]:
            stack.pop()
            index += 1
            if not stack:
                return index
            continue

        nested_closers = opening_to_closing.get(character)
        if nested_closers is not None:
            stack.append(set(nested_closers))
            index += 1
            continue

        if character in closing_marks:
            return None
        index += 1

    return None


def _speaker_tag_pattern(language: str) -> regex.Pattern | None:
    verbs = _REPORTING_VERBS.get(language)
    if not verbs:
        return None
    pronouns = _REPORTING_PRONOUNS.get(language, ())
    articles = _REPORTING_ARTICLES.get(language, ())
    verb_pattern = "(?i:" + "|".join(
        regex.escape(verb) for verb in sorted(verbs, key=len, reverse=True)
    ) + ")"
    pronoun_pattern = "(?i:" + "|".join(
        regex.escape(word) for word in pronouns
    ) + ")"
    article_pattern = "(?i:" + "|".join(
        regex.escape(word) for word in articles
    ) + ")"
    named_speaker = (
        rf"(?:{_CAPITALIZED_NAME}(?:\s+{_CAPITALIZED_NAME}){{0,2}}|"
        rf"{_CAPITALIZED_NAME}['’]s\s+{_SPEAKER_WORD})"
    )
    article_speaker = rf"{article_pattern}\s+{_SPEAKER_WORD}"
    speaker_pattern = rf"(?:{pronoun_pattern}|{named_speaker}|{article_speaker})"
    adverb_pattern = r"(?:\s+(?i:\p{L}+ly))?" if language == "en" else ""
    return regex.compile(
        rf"\A\s*[,;:—–]?\s*(?:"
        rf"{verb_pattern}\s+{speaker_pattern}{adverb_pattern}|"
        rf"{speaker_pattern}\s+{verb_pattern}{adverb_pattern}"
        rf")\s*[.!?…]*\s*\Z"
    )


def _find_next_quote_start(text: str, start: int, pairs) -> int | None:
    opening_marks = {opening for opening, _closing in pairs}
    for index in range(start, len(text)):
        character = text[index]
        if character not in opening_marks:
            continue
        if character in {"'", "‘", "’"} and _is_internal_apostrophe(text, index):
            continue
        return index
    return None


def _is_allowed_attribution(text: str, language: str) -> bool:
    if not text.strip():
        return True
    if len(text) > _MAX_REPORTING_TAG_CHARS or language == "generic":
        return False
    tag_pattern = _speaker_tag_pattern(language)
    return bool(tag_pattern and tag_pattern.fullmatch(text))


def _quoted_dialogue_coverage(paragraph: str, language: str) -> float | None:
    pairs = _QUOTE_PAIRS[language]
    spans: list[tuple[int, int]] = []
    outside_chunks: list[str] = []
    cursor = 0
    while cursor < len(paragraph):
        quote_end = _find_balanced_quote_end(paragraph[cursor:], pairs)
        if quote_end is None:
            return None
        absolute_end = cursor + quote_end
        spans.append((cursor, absolute_end))

        next_start = _find_next_quote_start(paragraph, absolute_end, pairs)
        if next_start is None:
            outside_chunks.append(paragraph[absolute_end:])
            break
        outside_chunks.append(paragraph[absolute_end:next_start])
        cursor = next_start

    if not spans or not all(
        _is_allowed_attribution(chunk, language) for chunk in outside_chunks
    ):
        return None
    quoted_characters = 0
    for start, end in spans:
        content = paragraph[start + 1:end - 1]
        if not content.strip():
            return None
        quoted_characters += len(content)

    # Quote marks delimit speech; they are not part of the coverage content.
    content_length = len(paragraph) - (2 * len(spans))
    if content_length <= 0:
        return None
    return quoted_characters / content_length


def _is_quoted_dialogue(paragraph: str, language: str) -> bool:
    stripped = paragraph.strip()
    if (
        not stripped
        or len(stripped) > _MAX_DIALOGUE_PARAGRAPH_CHARS
        or stripped[0] not in {opening for opening, _closing in _QUOTE_PAIRS[language]}
    ):
        return False
    coverage = _quoted_dialogue_coverage(stripped, language)
    return coverage is not None and coverage >= 0.55


def _is_dash_dialogue_candidate(paragraph: str, language: str) -> bool:
    stripped = paragraph.strip()
    if language not in _DASH_LANGUAGES or len(stripped) > _MAX_DIALOGUE_PARAGRAPH_CHARS:
        return False
    # A hyphen-minus line is treated as a list item.  En/em dashes are the
    # conventional dialogue markers in the supported French and Polish texts.
    if (
        language == "fr"
        and len(stripped) > 1
        and stripped[0] == "—"
        and (stripped[1].isalpha() or stripped[1] in {"«", "“", '"'})
    ):
        return True
    return bool(re.match(r"^\s*[—–]\s+\S", paragraph))


def _english_exchange_evidence(paragraphs: Sequence[str]) -> bool:
    """Keep unrelated quoted maxims/instructions out of the dialogue path."""
    if any("?" in paragraph or "？" in paragraph for paragraph in paragraphs):
        return True
    if any(_ENGLISH_REPLY.search(paragraph.strip()) for paragraph in paragraphs):
        return True
    if sum(bool(_ENGLISH_PERSON.search(paragraph)) for paragraph in paragraphs) >= 2:
        return True
    # Candidate validation has already restricted outside text to speaker
    # attributions. Look for one without matching reporting verbs inside quotes.
    pairs = _QUOTE_PAIRS["en"]
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        cursor = 0
        while cursor < len(paragraph):
            quote_end = _find_balanced_quote_end(paragraph[cursor:], pairs)
            if quote_end is None:
                break
            absolute_end = cursor + quote_end
            next_start = _find_next_quote_start(paragraph, absolute_end, pairs)
            if paragraph[absolute_end:next_start].strip():
                return True
            if next_start is None:
                break
            cursor = next_start
    return False


def _find_french_opening_quote_end(
    paragraphs: Sequence[str], candidates: Sequence[str | None], opener_index: int
) -> int | None:
    opener = paragraphs[opener_index].strip()
    if (
        len(opener) > _MAX_DIALOGUE_PARAGRAPH_CHARS
        or not opener.startswith("«")
        or opener.count("«") != 1
        or "»" in opener
        or not opener[1:].strip()
    ):
        return None

    last_reply_index = min(
        len(paragraphs), opener_index + _MAX_FRENCH_OPENING_DASH_REPLIES + 1
    )
    for reply_index in range(opener_index + 1, last_reply_index):
        if candidates[reply_index] != "dash":
            return None

        reply = paragraphs[reply_index].strip()
        if (
            len(reply) > _MAX_DIALOGUE_PARAGRAPH_CHARS
            or "«" in reply
        ):
            return None

        closing_count = reply.count("»")
        if closing_count == 0:
            continue
        if closing_count == 1 and reply.rstrip(".!?…").endswith("»"):
            return reply_index
        return None

    return None


def classify_dialogue_paragraphs(
    paragraphs: Sequence[str], language: object
) -> list[dict | None]:
    """Return optional boundary metadata for each source paragraph.

    Supported language profiles may recognize short speaker tags and French
    or Polish dash dialogue.  Unknown and automatic language settings use a
    generic profile that accepts only entirely quoted paragraphs.
    """
    rule_language = _rule_language(language)
    candidates: list[str | None] = []
    for paragraph in paragraphs:
        if _is_quoted_dialogue(paragraph, rule_language):
            candidates.append("quoted")
        elif _is_dash_dialogue_candidate(paragraph, rule_language):
            candidates.append("dash")
        else:
            candidates.append(None)

    hard_run_ends: set[int] = set()
    if rule_language == "fr":
        for paragraph_index, candidate in enumerate(candidates):
            if candidate is not None:
                continue
            closing_index = _find_french_opening_quote_end(
                paragraphs, candidates, paragraph_index
            )
            if closing_index is not None:
                candidates[paragraph_index] = "quoted"
                hard_run_ends.add(closing_index + 1)

    metadata: list[dict | None] = [None] * len(paragraphs)
    index = 0
    while index < len(paragraphs):
        if candidates[index] is None:
            index += 1
            continue
        end = index + 1
        while end < len(paragraphs) and candidates[end] is not None:
            end += 1
            if end in hard_run_ends:
                break

        run_length = end - index
        if (
            rule_language == "en" and run_length >= 2
            and not _english_exchange_evidence(paragraphs[index:end])
        ):
            index = end
            continue
        dash_is_eligible = run_length >= 2
        for paragraph_index in range(index, end):
            if candidates[paragraph_index] == "dash" and not dash_is_eligible:
                continue
            metadata[paragraph_index] = {
                "source": "punctuation",
                "paragraph_count": 1,
                "rule_language": rule_language,
                "speech_boundary_after": (
                    "dialogue_turn" if paragraph_index < end - 1 else "paragraph"
                ),
            }
        index = end

    return metadata
