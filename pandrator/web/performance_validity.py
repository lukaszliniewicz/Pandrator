"""Judge an adopted speaker plan per block instead of as a whole.

A plan's ``base_signature`` covers every block of its revision, so one edit
makes the whole plan stale. Each plan unit also stores its own ``text_hash``
and the block fields its planner read (speaker, language, node kind and
section): an unchanged block keeps its entry, an edited one keeps its markup
when only its text changed and the markup still fits the new text, and only
the rest need a new review.

The adopted plan itself is never written: fitted entries are derived whenever a
snapshot is frozen or a plan is read.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Set
from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select

from pandrator.logic.speech_performance import (
    PerformanceAnnotation,
    anchor_range,
    content_hash,
    validate_annotation,
)

from . import models as m
from .performance_annotations import plan_annotations
from .speech_annotation_records import normalized_record, record_annotation, record_markup

# Elements whose own text is spoken. Events and delivery controls only
# contribute their tail, exactly as ``speech_markup._extract`` reads them.
_SPOKEN_TEXT_TAGS = frozenset({"segment", "dialogue", "speaker", "narrator", "span"})
_DIALOGUE_TAGS = frozenset({"dialogue", "speaker"})
# Quotation marks that open or close a spoken line: Unicode's quotation marks,
# the backtick of older e-texts (`like this') and the heavy quotation mark
# ornaments.
_QUOTE_MARKS = frozenset(
    '"\u201c\u201d\u201e\u201f\u2018\u201a\u201b\u00ab\u00bb\u2039\u203a'
    "\u300c\u300d\u300e\u300f\u301d\u301e\u301f\uff02\uff62\uff63\ufe41\ufe42\ufe43\ufe44"
    "\u2e42\uff07`\u275b\u275c\u275d\u275e"
)
# Single quotation marks that are also the apostrophe of ordinary words: one
# counts as a quotation mark unless letters of a script that separates its
# words with spaces stand on both sides of it.
_APOSTROPHES = frozenset("'\u2019")
# Scripts written without spaces between words, where a letter on each side of
# a single quote does not make it an apostrophe inside a word ("\u4ed6\u8bf4'\u4f60\u597d'").
_UNSPACED_SCRIPTS = (
    "CJK ",
    "HIRAGANA",
    "KATAKANA",
    "HALFWIDTH KATAKANA",
    "BOPOMOFO",
    "YI ",
    "THAI",
    "LAO",
    "KHMER",
    "MYANMAR",
    "TIBETAN",
)
# Dashes that open a line of dialogue in Polish, Spanish, Russian or French
# ("\u2014 Id\u0119 do domu \u2014 powiedzia\u0142a."): one counts as a quotation mark when only
# whitespace stands before it on its line. A dash inside narration does not.
_DIALOGUE_DASHES = frozenset("\u2014\u2013\u2015")
# What ``str.splitlines`` breaks a line at.
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
# A word, or each character of a script written without spaces between words
# (CJK, Kana and Hangul), for telling whether an edit adds words beside an event
# or control: a spaceless sentence then aligns character by character.
_WORD = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
    r"|[^\s\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+"
)
# Past this many tokens an alignment can go quadratic on repeated words, so the
# edit needs review rather than a proof that no words were added beside an event.
_MAX_ALIGNED_TOKENS = 2_000
# The same bound for the character diff: past it, a quotation mark cannot be
# shown unchanged, so the edit needs review.
_MAX_DIFFED_CHARS = 10_000

REVIEW_MESSAGE = (
    "This block changed after its speaker plan was adopted and its speaker markup "
    "no longer fits the new text. Review a new speaker plan before generating it."
)


@dataclass(frozen=True, slots=True)
class PlanValidity:
    """Per-block state of one speaker plan against its revision's blocks.

    ``entries`` holds the frozen snapshot entry (``text_hash`` and ``annotation``)
    of every block that keeps one: unchanged blocks and fitted edits.
    ``changed_ids`` lists blocks whose spoken text or structure differs from
    the plan's (fitted, review, removed, blanked, or spoken now with no unit),
    and spoken blocks whose unit never recorded a structure field, edited or
    not, since they cannot show their structure is unchanged.
    ``review`` lists ``(ordinal, id)`` of spoken blocks that have no usable
    entry, in ordinal order: an edit that does not fit, a changed speaker,
    language, node kind or section (or a unit that never recorded one), or a
    block the plan never planned.
    """

    entries: dict[str, dict[str, Any]]
    changed_ids: tuple[str, ...]
    review: tuple[tuple[int, str], ...]

    @property
    def review_ids(self) -> tuple[str, ...]:
        return tuple(segment_id for _ordinal, segment_id in self.review)


def _changed_range(old: str, new: str, *, from_end: bool = False) -> tuple[int, int, str]:
    """Return the old range ``[start, end)`` and the text that replaces it.

    Where the change repeats the characters beside it, several ranges describe
    it equally well: matching from the start places it as far right as it can
    go, and ``from_end`` as far left.
    """

    limit = min(len(old), len(new))
    prefix = suffix = 0
    if from_end:
        while suffix < limit and old[-1 - suffix] == new[-1 - suffix]:
            suffix += 1
        limit -= suffix
        while prefix < limit and old[prefix] == new[prefix]:
            prefix += 1
    else:
        while prefix < limit and old[prefix] == new[prefix]:
            prefix += 1
        limit -= prefix
        while suffix < limit and old[-1 - suffix] == new[-1 - suffix]:
            suffix += 1
    return prefix, len(old) - suffix, new[prefix : len(new) - suffix]


def _change_placements(old: str, new: str) -> tuple[tuple[int, int, str], ...]:
    """The leftmost and rightmost ranges of one change; any other lies between."""

    return _changed_range(old, new, from_end=True), _changed_range(old, new)


def _spaced_letter(char: str) -> bool:
    """Whether ``char`` is a letter of a script that puts spaces between words."""

    return char.isalpha() and not unicodedata.name(char, "").startswith(_UNSPACED_SCRIPTS)


def _unspaced_letter(char: str) -> bool:
    """Whether ``char`` is a letter of a script written without spaces between words."""

    return char.isalpha() and unicodedata.name(char, "").startswith(_UNSPACED_SCRIPTS)


def _unspaced_run(text: str, offset: int) -> tuple[int, int] | None:
    """The run of spaceless-script letters touching ``offset``, if any."""

    start = offset
    while start > 0 and _unspaced_letter(text[start - 1]):
        start -= 1
    end = offset
    while end < len(text) and _unspaced_letter(text[end]):
        end += 1
    return (start, end) if end > start else None


def _line_start(text: str, index: int) -> bool:
    """Whether only whitespace stands before ``text[index]`` on its line."""

    for char in reversed(text[:index]):
        if char in _LINE_BREAKS:
            return True
        if not char.isspace():
            return False
    return True


def _quotation_marks(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` holds a quotation mark.

    ``'`` and ``’`` count unless letters of a space-separated script stand on
    both sides of them, where they are an apostrophe inside a word ("didn’t").
    A dash counts at the start of a line, where it opens a line of dialogue.
    """

    for index in range(start, end):
        char = text[index]
        if char in _QUOTE_MARKS:
            return True
        if char in _APOSTROPHES and not (
            0 < index < len(text) - 1
            and _spaced_letter(text[index - 1])
            and _spaced_letter(text[index + 1])
        ):
            return True
        if char in _DIALOGUE_DASHES and _line_start(text, index):
            return True
    return False


def _quote_role_changes(
    old: str, new: str, start: int, end: int, new_start: int, length: int
) -> bool:
    """Whether a single quote or dash beside ``old[start:end]`` changes role.

    ``new_start`` is where the changed part starts in ``new``, and ``length``
    characters replace it there. Only the characters on either side of it gain
    or lose a neighbouring letter, so only an apostrophe there can turn into a
    quotation mark ("didn't" to "didn 't") or back ("'tis" to "x'tis"). A dash
    opens a line of dialogue by what stands before it on its line, so only the
    first one after the change, with nothing but whitespace between them, can
    turn into a quotation mark ("Then: — Go." to "— Go.") or back.
    """

    for old_index, new_index in ((start - 1, new_start - 1), (end, new_start + length)):
        if (
            0 <= old_index < len(old)
            and 0 <= new_index < len(new)
            and old[old_index] in _APOSTROPHES
            and _quotation_marks(old, old_index, old_index + 1)
            != _quotation_marks(new, new_index, new_index + 1)
        ):
            return True
    dash = end
    while dash < len(old) and old[dash] not in _LINE_BREAKS and old[dash].isspace():
        dash += 1
    if dash < len(old) and old[dash] in _DIALOGUE_DASHES:
        # The text after the change is the same in both, shifted.
        moved = dash - end + new_start + length
        if _quotation_marks(old, dash, dash + 1) != _quotation_marks(new, moved, moved + 1):
            return True
    return False


def _quote_changed(old: str, new: str) -> bool:
    """Whether the edit adds, removes or replaces a quotation mark.

    Each part of the character diff is inspected on its own: an edit either
    side of an unchanged mark leaves it alone, while a mark inserted, deleted or
    replaced, or an apostrophe or dash beside a changed part turned into one or
    back, moves where speech starts or ends. In any block, narration may now
    hold dialogue, or a spoken line an attribution ("“Go,” he said, “now.”"). A
    dash that opens a line counts as a quotation mark ("— Go — she said."). An
    apostrophe that stays inside a word ("didn't" to "couldn't") and a dash
    inside narration are not one.
    """

    start, end, replacement = _changed_range(old, new)
    # A long region of repeated text would make the diff quadratic; without it
    # the marks cannot be shown unchanged.
    if max(end - start, len(replacement)) > _MAX_DIFFED_CHARS:
        return True
    matcher = SequenceMatcher(None, old[start:end], replacement, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if (
            _quotation_marks(old, start + i1, start + i2)
            or _quotation_marks(new, start + j1, start + j2)
            or _quote_role_changes(
                old, new, start + i1, start + i2, start + j1, j2 - j1
            )
        ):
            return True
    return False


def _words_added_beside(old: str, new: str, offsets: Set[int]) -> bool:
    """Whether the edit adds words at an event or control's offset.

    The character change alone can miss this: new words that share letters
    with the old ones start it past the event ("Hello" to "Hi, hello"), a swap
    can keep its length ("Hello" to "Oh hi"), and a second edit elsewhere can
    shrink the whole change. Compared word by word, a change that brings in more
    words than it replaces, over words that reach an offset or in the gap that
    holds one, could put the new words on either side of it. Both alignments of
    repeated words are tried, as for the character change.
    """

    if not offsets:
        return False
    old_words = [(match.start(), match.end()) for match in _WORD.finditer(old)]
    old_tokens = [old[start:end] for start, end in old_words]
    new_tokens = _WORD.findall(new)
    old_count, new_count = len(old_tokens), len(new_tokens)
    # Only an alignment can show where added words sit, and a long one can go
    # quadratic on repeated words; past the limit the edit needs review.
    if max(old_count, new_count) > _MAX_ALIGNED_TOKENS:
        return True
    alignments = [SequenceMatcher(None, old_tokens, new_tokens, autojunk=False).get_opcodes()]
    reverse = SequenceMatcher(None, old_tokens[::-1], new_tokens[::-1], autojunk=False)
    alignments.append(
        [
            (tag, old_count - i2, old_count - i1, new_count - j2, new_count - j1)
            for tag, i1, i2, j1, j2 in reverse.get_opcodes()
        ]
    )
    # Text written without spaces has no word boundary to place new words by:
    # any change inside the run of such letters that holds an event or control
    # may have moved words across it, so it needs review.
    runs = [run for run in (_unspaced_run(old, offset) for offset in offsets) if run]
    for opcodes in alignments:
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                continue
            if i2 > i1:
                left, right = old_words[i1][0], old_words[i2 - 1][1]
            else:
                left = old_words[i1 - 1][1] if i1 else 0
                right = old_words[i1][0] if i1 < old_count else len(old)
            if any(start <= right and left <= end for start, end in runs):
                return True
            if j2 - j1 <= i2 - i1:
                continue
            if any(left <= offset <= right for offset in offsets):
                return True
    return False


def _text_slots(
    root: ET.Element,
) -> tuple[str, list[tuple[ET.Element, str, int, int, bool]], set[int]]:
    """List every non-empty spoken string as (owner, attribute, start, end, voiced).

    ``voiced`` is true when ``speech_markup._scope_for`` gives the string a
    voice other than the narration's: inside a ``dialogue`` or ``speaker``
    element, or a ``span`` or ``narrator`` with its own ``voice``, unless a
    nearer ``narrator`` without one resets it.

    Also returns the offsets where an event or delivery control sits. One with
    no text on a side (at the start or end of a block) leaves no boundary
    between two strings, so it is listed on its own.
    """

    slots: list[tuple[ET.Element, str, int, int, bool]] = []
    markers: set[int] = set()
    offset = 0

    def add(owner: ET.Element, attribute: str, value: str | None, voiced: bool) -> None:
        nonlocal offset
        if not value:
            return
        slots.append((owner, attribute, offset, offset + len(value), voiced))
        offset += len(value)

    def walk(element: ET.Element, inherited: bool) -> None:
        if element.tag not in _SPOKEN_TEXT_TAGS:
            return
        if element.tag == "narrator":
            voiced = "voice" in element.attrib
        elif element.tag in _DIALOGUE_TAGS or (
            element.tag == "span" and "voice" in element.attrib
        ):
            voiced = True
        else:
            voiced = inherited
        add(element, "text", element.text, voiced)
        for child in element:
            if child.tag not in _SPOKEN_TEXT_TAGS:
                markers.add(offset)
            walk(child, voiced)
            add(child, "tail", child.tail, voiced)

    walk(root, False)
    transcript = "".join(
        str(getattr(owner, attribute)) for owner, attribute, *_rest in slots
    )
    return transcript, slots, markers


def _fit_markup(xml: str, old: str, new: str, segment_id: str) -> str | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    transcript, slots, markers = _text_slots(root)
    if transcript != old:
        return None
    if _words_added_beside(old, new, markers):
        return None
    chosen: int | None = None
    quotes = _quote_changed(old, new)
    # Both extreme placements of the change must fit the same string, so text
    # that repeats the characters beside an edge cannot slip across it.
    for start, end, replacement in _change_placements(old, new):
        # Text added beside an event or control, by an insertion or by a
        # replacement that lengthens the words there, is ambiguous: it could
        # fall on either side of it. A pure insertion where two strings meet
        # matches both of them below, and a change across two strings crosses
        # a span or event boundary.
        if (start in markers or end in markers) and len(replacement) > end - start:
            return None
        matches = [
            index for index, slot in enumerate(slots) if slot[2] <= start and end <= slot[3]
        ]
        if len(matches) != 1 or chosen not in (None, matches[0]):
            return None
        chosen = matches[0]
        _owner, _attribute, slot_start, slot_end, voiced = slots[chosen]
        # Text added or removed at the edge of a spoken line, or of a span with
        # its own voice, could be the narration around it (an attribution after
        # a closing quote, say), so a change that reaches such a string's edge
        # needs review.
        if voiced and (start == slot_start or end == slot_end):
            return None
        # A quotation mark added, removed or replaced moves where speech
        # starts or ends, with or without a spoken line in the block.
        if quotes:
            return None
    if chosen is None:
        return None
    start, end, replacement = _changed_range(old, new)
    owner, attribute, slot_start, _slot_end, _voiced = slots[chosen]
    value = str(getattr(owner, attribute))
    setattr(
        owner,
        attribute,
        value[: start - slot_start] + replacement + value[end - slot_start :],
    )
    root.attrib["id"] = segment_id
    # ElementTree writes a carriage return in text raw, and a reparse would
    # read it as a line feed; keep it as the character reference the canonical
    # markup uses.
    return ET.tostring(root, encoding="unicode").replace("\r", "&#13;")


def _legacy_anchors_hold(annotation: Mapping[str, Any], old: str, new: str) -> bool:
    """Whether every anchor of a legacy entry stays on the same words.

    An anchor resolves by its quote and occurrence, so an edit before it that
    adds or removes an earlier copy of the quote would move it elsewhere. Each
    anchor must lie wholly before or after the change, wherever repeated
    characters let the change sit, and resolve in the new text exactly where it
    was, shifted by the change's length when after it.

    An event sits before or after its anchored words, or at the start or end of
    the block without an anchor. As for an event in markup, text added right
    beside it, by an insertion or a lengthening replacement, or words added to
    the words beside it, could fall on either side of it, so the entry does not
    fit.
    """

    parsed = PerformanceAnnotation.model_validate(dict(annotation))
    anchors = [span.anchor for span in parsed.spans]
    anchors.extend(event.anchor for event in parsed.events if event.anchor)
    event_offsets = set()
    for event in parsed.events:
        if event.anchor is None:
            event_offsets.add(0 if event.position == "before" else len(old))
        else:
            anchor_start, anchor_end = anchor_range(old, event.anchor)
            event_offsets.add(anchor_start if event.position == "before" else anchor_end)
    if _words_added_beside(old, new, event_offsets):
        return False
    for start, end, replacement in _change_placements(old, new):
        delta = len(replacement) - (end - start)
        if delta > 0 and (start in event_offsets or end in event_offsets):
            return False
        for anchor in anchors:
            old_start, old_end = anchor_range(old, anchor)
            new_start, _new_end = anchor_range(new, anchor)
            if old_end <= start:
                expected = old_start
            elif old_start >= end:
                expected = old_start + delta
            else:
                # The change overlaps the anchored words, or inserts inside them.
                return False
            if new_start != expected:
                return False
    return True


def fit_entry(
    unit: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    spoken_text: str,
    segment_id: str,
    characters: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Rebase one plan entry onto a segment's new spoken text, or return None.

    The entry fits when the change lies inside one spoken string of its markup,
    wherever repeated characters let it sit; adds no text right beside an
    event or control, nor words to the words beside one; does not reach the
    edge of a voiced string (an event or control inside one is an edge too);
    adds, removes or replaces no quotation mark (a dash that opens a line is
    one), nor turns an apostrophe or dash into one or back, whether or not the
    block has a spoken line; and the rebased
    markup parses against the new text. A legacy (non-XML) annotation fits
    when it still validates against the new text, every anchor still resolves
    to the same words, no text or words are added right beside an event, and
    no quotation mark changes, by the same rule as markup.
    """

    if record is None:
        return None
    old = str(unit.get("spoken_text") or "")
    xml = record_markup(record)
    if xml is None:
        annotation = record_annotation(record) or {}
        try:
            fitted = validate_annotation(spoken_text, annotation)
            if not _legacy_anchors_hold(annotation, old, spoken_text):
                return None
            if _quote_changed(old, spoken_text):
                return None
        except ValueError:
            return None
        return fitted
    if old == spoken_text:
        candidate: str | None = xml
    else:
        candidate = _fit_markup(xml, old, spoken_text, segment_id)
    if candidate is None:
        return None
    locked = record.get("locked", False)
    reason = record.get("reason") or ""
    try:
        return normalized_record(
            spoken_text,
            segment_id,
            {
                "speech_xml": candidate,
                "locked": locked if isinstance(locked, bool) else False,
                "reason": reason if isinstance(reason, str) else "",
            },
            characters,
        )
    except ValueError:
        return None


def plan_characters(plan: m.PerformancePlan) -> list[dict[str, Any]]:
    value = (plan.settings_json or {}).get("character_dictionary") or []
    return deepcopy(value) if isinstance(value, list) else []


# The fields a plan unit records about its own block besides its text, as
# ``speech_plan_context.semantic_context_units`` builds them: what the planner
# read about who speaks the block and where it sits. Voice, voice id, pauses
# and references are left out: audio identity reads them live, and the frozen
# entry does not depend on them.
STRUCTURE_FIELDS = ("speaker", "language", "node_kind", "section_id")

UNCHANGED = "unchanged"
FITTED = "fitted"
REVIEW = "review"
SILENT = "silent"


def block_structure(
    speaker: str | None,
    language: str | None,
    node_kind: str | None,
    provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """A block's structure fields, normalised exactly as plan units store them."""

    return {
        "speaker": speaker or "",
        "language": language or "",
        "node_kind": node_kind,
        "section_id": str((provenance or {}).get("section_id") or ""),
    }


def judge_block(
    unit: Mapping[str, Any] | None,
    record: Mapping[str, Any] | None,
    *,
    spoken_text: str,
    removed: bool,
    structure: Mapping[str, Any],
    segment_id: str,
    characters: list[dict[str, Any]] | None,
) -> tuple[str, Mapping[str, Any] | None]:
    """Judge one block against its plan unit and entry.

    Returns the state and the entry to freeze: ``unchanged`` with the stored
    record (not copied), ``fitted`` with a rebased record, ``review`` or
    ``silent`` (removed or blank, nothing to speak) with ``None``.

    A unit made before units recorded a structure field cannot show that its
    block's speaker, language, node kind or section is unchanged, so it needs
    review, on every path a plan is judged, a project copy included.
    """

    if removed or not spoken_text.strip():
        return SILENT, None
    if unit is None:
        # Blank when the plan was made, spoken now.
        return REVIEW, None
    if any(
        field not in unit or unit[field] != structure[field] for field in STRUCTURE_FIELDS
    ):
        return REVIEW, None
    if unit.get("text_hash") == content_hash(spoken_text):
        return UNCHANGED, record
    fitted = fit_entry(unit, record, spoken_text, segment_id, characters)
    return (REVIEW, None) if fitted is None else (FITTED, fitted)


def segment_entry(
    plan: m.PerformancePlan,
    segment: m.GenerationSegment,
    annotations: Mapping[str, Mapping[str, Any]],
) -> tuple[str, Mapping[str, Any] | None]:
    """``judge_block`` for one block of the plan's revision."""

    unit = next(
        (item for item in plan.units_json if str(item["id"]) == segment.id), None
    )
    return judge_block(
        unit,
        annotations.get(segment.id),
        spoken_text=segment.optimized_text or segment.text or "",
        removed=bool(segment.removed),
        structure=block_structure(
            segment.speaker,
            segment.language,
            segment.node_kind,
            segment.speech_block_provenance_json,
        ),
        segment_id=segment.id,
        characters=plan_characters(plan),
    )


def plan_validity(
    session,
    plan: m.PerformancePlan,
    revision_id: str,
    *,
    annotations: Mapping[str, Mapping[str, Any]] | None = None,
    counts_only: bool = False,
) -> PlanValidity:
    """Judge every plan entry against the revision's current blocks.

    A block is unchanged while its spoken text (``text_hash``) and structure
    (``STRUCTURE_FIELDS``) match its unit; an edit to its text alone may still
    fit (``fit_entry``); anything else needs review. ``counts_only`` leaves
    ``entries`` empty, for a plan read that needs only the changed and review
    blocks.
    """

    if annotations is None:
        annotations = plan_annotations(session, plan)
    characters = plan_characters(plan)
    units = {str(unit["id"]): unit for unit in plan.units_json}
    rows = session.execute(
        select(
            m.GenerationSegment.id,
            m.GenerationSegment.ordinal,
            m.GenerationSegment.text,
            m.GenerationSegment.optimized_text,
            m.GenerationSegment.removed,
            m.GenerationSegment.speaker,
            m.GenerationSegment.language,
            m.GenerationSegment.node_kind,
            m.GenerationSegment.speech_block_provenance_json,
        )
        .where(m.GenerationSegment.plan_revision_id == revision_id)
        .order_by(m.GenerationSegment.ordinal)
    ).all()
    entries: dict[str, dict[str, Any]] = {}
    changed: list[str] = []
    review: list[tuple[int, str]] = []
    seen: set[str] = set()
    for (
        segment_id, ordinal, text, optimized_text, removed,
        speaker, language, node_kind, provenance,
    ) in rows:
        unit = units.get(segment_id)
        if unit is not None:
            seen.add(segment_id)
        spoken = optimized_text or text or ""
        state, record = judge_block(
            unit,
            annotations.get(segment_id),
            spoken_text=spoken,
            removed=bool(removed),
            structure=block_structure(speaker, language, node_kind, provenance),
            segment_id=segment_id,
            characters=characters,
        )
        if state == SILENT:
            if unit is not None:
                changed.append(segment_id)
            continue
        if state != UNCHANGED:
            changed.append(segment_id)
        if state == REVIEW:
            review.append((int(ordinal), segment_id))
        elif record is not None and not counts_only:
            entries[segment_id] = {
                "text_hash": content_hash(spoken),
                "annotation": deepcopy(dict(record)),
            }
    changed.extend(segment_id for segment_id in units if segment_id not in seen)
    return PlanValidity(entries, tuple(changed), tuple(review))


__all__ = [
    "REVIEW",
    "REVIEW_MESSAGE",
    "STRUCTURE_FIELDS",
    "PlanValidity",
    "block_structure",
    "fit_entry",
    "judge_block",
    "plan_characters",
    "plan_validity",
    "segment_entry",
]
