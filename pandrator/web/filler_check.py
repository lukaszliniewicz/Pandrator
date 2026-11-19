"""Post-correction filler diagnostics for the declared removal policy.

A correction run knows whether incidental disfluencies were meant to disappear.
This check reads the finalised passages and reports fillers the model left
behind. It is diagnostic only: it never blocks finalisation.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from pandrator.logic.dubbing.languages import normalize_language_code
from pandrator.logic.dubbing.settings import (
    CORRECTION_STYLE_FAITHFUL,
    CORRECTION_STYLE_PUBLISHABLE,
    normalize_correction_style,
)

# The styles whose definition removes incidental disfluencies. Transcript-faithful
# correction deliberately preserves meaningful hesitation, so it is never scanned.
FILLER_REMOVING_CORRECTION_STYLES = frozenset({CORRECTION_STYLE_PUBLISHABLE})
PRESERVING_CORRECTION_STYLE = CORRECTION_STYLE_FAITHFUL

# One place for the token list. English only for now; whole words, case-insensitive,
# so a following comma or full stop is still a match but "uhh" or "term" is not.
ENGLISH_FILLER_TOKENS = ("uh", "um", "erm", "er", "ah", "uhm", "hmm")
_FILLER_PATTERN = re.compile(
    r"(?<!\w)(?:%s)(?!\w)"
    % "|".join(
        sorted((re.escape(token) for token in ENGLISH_FILLER_TOKENS), key=len, reverse=True)
    ),
    re.IGNORECASE,
)
MAX_REPORTED_PASSAGES = 50
MAX_REPORTED_TEXT = 80


def _not_checked(policy: str, reason: str) -> dict[str, Any]:
    return {
        "status": "not_checked",
        "policy": policy,
        "reason": reason,
        "count": 0,
        "passages": [],
    }


def _passage_identifier(
    item: Mapping[str, Any], ordinal: int, *, logical: bool
) -> dict[str, Any]:
    """Name a passage the way the patch tools take it.

    A logical passage is named by its ``id`` ("p000028"), which the passage
    preview and ``pandrator_patch_subtitle_passages`` use; a plain cue by its
    1-based ordinal, for ``pandrator_patch_subtitle_cues``.
    """

    passage_id = item.get("id")
    if logical and passage_id:
        return {"passage_id": str(passage_id)}
    return {"cue_id": int(item.get("ordinal") or ordinal)}


def filler_check(
    values: Iterable[Mapping[str, Any]],
    *,
    kind: str,
    document_language: str,
    settings: Mapping[str, Any] | None,
    logical: bool = False,
) -> dict[str, Any] | None:
    """Scan finalised passages for fillers a filler-removing style should remove.

    ``logical`` says ``values`` are the output's logical passages rather than
    its display cues.
    """

    if kind != "correction":
        return None
    policy = normalize_correction_style((settings or {}).get("correction_style"))
    if policy not in FILLER_REMOVING_CORRECTION_STYLES:
        return _not_checked(policy, "The correction style keeps fillers.")
    language = normalize_language_code(str(document_language or ""), default="")
    if not language.startswith("en"):
        return _not_checked(policy, "Filler tokens are only checked for English documents.")
    count = 0
    passages: list[dict[str, Any]] = []
    for ordinal, item in enumerate(values, start=1):
        text = str(item.get("text") or "")
        matches = _FILLER_PATTERN.findall(text)
        if not matches:
            continue
        count += len(matches)
        if len(passages) < MAX_REPORTED_PASSAGES:
            passages.append(
                {
                    **_passage_identifier(item, ordinal, logical=logical),
                    "text": text[:MAX_REPORTED_TEXT],
                }
            )
    return {
        "status": "fillers_remaining" if count else "clean",
        "policy": policy,
        "count": count,
        "passages": passages,
    }
