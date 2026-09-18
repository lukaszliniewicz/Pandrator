"""Unicode-safe text seams and subtitle layout units.

Layout units are NOT linguistic words and must never be used to cut speech
blocks. Python character offsets remain code-point offsets; only display
capacity and reading-speed calculations use the weighted grapheme counts here.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache

import regex

_CJK = regex.compile(r"[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]")
_UNSPACED = regex.compile(r"[\p{Han}\p{Hiragana}\p{Katakana}]")
_HANGUL = regex.compile(r"\p{Hangul}")
_GRAPHEMES = regex.compile(r"\X")
_INLINE_SPACE = re.compile(r"[^\S\n\r\u3000\u00a0\u202f]+")
_LINE_BREAK = re.compile(r"[ \t]*[\r\n]+[ \t]*")

# Conservative kinsoku guards, not a replacement for a language-specific
# morphological analyser or a complete Unicode line-breaking implementation.
NO_LINE_START = frozenset(
    ",.!?:;%)]}…、。，．！？：；％）］｝」』】〉》〕〗〙〛"
    "ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶー々ゝゞヽヾ"
    "ｧｨｩｪｫｯｬｭｮｰﾞﾟ"
)
NO_LINE_END = frozenset("([{（［｛「『【〈《〔〖〘〚")
_CJK_PUNCTUATION = frozenset("、。，．！？：；（）［］｛｝「」『』【】〈〉《》〔〕")
_NO_SPACE_BEFORE = frozenset(",.!?:;%)]}…、。，．！？：；％）］｝」』】〉》〕\"\u201d\u2019")
_NO_SPACE_AFTER = NO_LINE_END | frozenset("£€$\u201c")


def contains_cjk(text: str) -> bool:
    return bool(_CJK.search(str(text or "")))


def infer_cjk_language(text: str) -> str:
    """Only infer a script family when no usable language metadata exists."""
    value = str(text or "")
    if regex.search(r"[\p{Hiragana}\p{Katakana}]", value):
        return "ja"
    if _HANGUL.search(value):
        return "ko"
    return "zh" if regex.search(r"\p{Han}", value) else ""


def fragment_separator(left: str, right: str) -> str:
    """Join transcript/cue fragments without manufacturing Japanese spaces.

    Korean remains word-spaced. Existing whitespace inside each fragment is
    preserved, including the meaningful Japanese ideographic space (U+3000).
    """
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return ""
    a, b = left[-1], right[0]
    if b in _NO_SPACE_BEFORE or a in _NO_SPACE_AFTER or right.startswith(("'", "’")):
        return ""
    if not (_HANGUL.search(a) or _HANGUL.search(b)) and (
        _UNSPACED.search(a) or _UNSPACED.search(b)
        or a in _CJK_PUNCTUATION or b in _CJK_PUNCTUATION
    ):
        return ""
    return " "


def clean_text(value: object) -> str:
    """Flatten presentation line breaks without romanizing or changing script."""
    text = _INLINE_SPACE.sub(" ", str(value or ""))
    if "\n" not in text and "\r" not in text:
        return text.strip(" \t\r\n")
    parts = _LINE_BREAK.split(text)
    result = parts[0]
    for part in parts[1:]:
        result += fragment_separator(result, part) + part
    return result.strip(" \t\r\n")


def join_fragments(values: Iterable[object]) -> str:
    result = ""
    for value in values:
        text = clean_text(value)
        if text:
            result += fragment_separator(result, text) + text
    return result.strip(" \t\r\n")


@lru_cache(maxsize=8192)
def _cluster_width(cluster: str, cjk: bool) -> float:
    if cluster in {"\n", "\r", "\r\n"}:
        return 0.0
    if not cjk:
        return 1.0
    # Combining kana marks, variation selectors, Jamo and ZWJ sequences form
    # one grapheme. Use its widest base, rather than counting each code point.
    bases = [c for c in cluster if unicodedata.category(c) not in {"Mn", "Mc", "Me", "Cf"}]
    if not bases:
        return 0.0
    return 1.0 if any(unicodedata.east_asian_width(c) in {"W", "F"} for c in bases) else 0.5


def display_length(text: str, *, cjk: bool | None = None) -> float:
    value = str(text or "")
    weighted = contains_cjk(value) if cjk is None else cjk
    if value.isascii():
        return len(value.replace("\n", "").replace("\r", "")) * (0.5 if weighted else 1.0)
    return sum(_cluster_width(cluster, weighted) for cluster in _GRAPHEMES.findall(value))


def subtitle_units(text: str, *, split_hangul: bool = False) -> list[str]:
    """Return losslessly joinable layout units, retaining existing spaces.

    Han/kana can wrap without spaces. Korean prefers existing word boundaries;
    syllable boundaries are an explicit fallback for an over-capacity word.
    Opening/closing punctuation and small kana stay with their neighbour.
    Latin words and grapheme clusters are never cut internally.
    """
    value = clean_text(text)
    clusters = list(_GRAPHEMES.finditer(value))
    if not clusters:
        return []
    cuts = [0]
    for left, right in zip(clusters, clusters[1:]):
        a, b = left.group(), right.group()
        if a[-1] in NO_LINE_END or b[0] in NO_LINE_START:
            continue
        if b.isspace() or a in {"\u00a0", "\u202f"} or b in {"\u00a0", "\u202f"}:
            continue
        if a.isspace() or _UNSPACED.search(a) or _UNSPACED.search(b) or (
            split_hangul and (_HANGUL.search(a) or _HANGUL.search(b))
        ) or a[-1] in _CJK_PUNCTUATION or b[0] in _CJK_PUNCTUATION:
            cuts.append(left.end())
    cuts.append(len(value))
    return [value[start:end] for start, end in zip(cuts, cuts[1:])]


def pronunciation_pattern(value: str, *, flexible_whitespace: bool = False) -> str:
    """Match reviewed names at script-appropriate boundaries.

    Multi-character Han/kana names may adjoin particles without spaces.
    A single Han/kana character remains conservatively word-bounded. Latin
    words still require Latin word boundaries, but may adjoin Han/kana.
    """
    if not value:
        return r"(?!)"
    multi = len(_GRAPHEMES.findall(value)) > 1
    unspaced = r"[\p{Han}\p{Hiragana}\p{Katakana}]"
    prefix = suffix = ""
    if value[0].isalnum() and not (multi and _UNSPACED.match(value[0])):
        prefix = rf"(?:(?<!\w)|(?<={unspaced}))" if not _UNSPACED.match(value[0]) else r"(?<!\w)"
    if value[-1].isalnum() and not (multi and _UNSPACED.match(value[-1])):
        suffix = rf"(?:(?!\w)|(?={unspaced}))" if not _UNSPACED.match(value[-1]) else r"(?!\w)"
    body = r"\s+".join(regex.escape(part) for part in value.split()) if flexible_whitespace else regex.escape(value)
    return prefix + body + suffix


def strip_latin_diacritics(text: str) -> str:
    """Remove Latin accents only, never dakuten, Hangul, Han or other scripts."""
    def convert(match: regex.Match) -> str:
        cluster = match.group()
        if not regex.match(r"\p{Latin}", cluster):
            return cluster
        return "".join(c for c in unicodedata.normalize("NFD", cluster) if not unicodedata.combining(c))
    return _GRAPHEMES.sub(convert, str(text or ""))
