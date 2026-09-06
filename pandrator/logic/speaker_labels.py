"""Shared caption speaker-label normalization heuristics."""

from __future__ import annotations

import re
from collections.abc import Collection

_SPEAKER_LABEL_RE = re.compile(r"^(?P<label>[^:\n]{1,80}):\s+(?P<text>\S.*)$")

# These words commonly introduce explanatory prose rather than a person or
# diarization label. They remain eligible when repeated across cues, which is
# the same evidence used for plausible lowercase labels.
_PROSE_LABELS = frozenset(
    {
        "answer",
        "context",
        "conclusion",
        "example",
        "explanation",
        "note",
        "question",
        "reason",
        "result",
        "summary",
        "warning",
    }
)


def speaker_label_candidate(text: str) -> tuple[str, str] | None:
    """Return a literal ``label: payload`` candidate, if one is present."""

    match = _SPEAKER_LABEL_RE.fullmatch(str(text or "").strip())
    if match is None:
        return None
    return match.group("label").strip(), match.group("text").strip()


def normalize_speaker_label(
    text: str,
    repeated_speakers: Collection[str] = (),
) -> tuple[str | None, str]:
    """Split a plausible human/Zoom speaker prefix from caption text.

    Title-like labels (for example, ``Pascal Schilling``) are accepted on one
    cue. Lowercase or otherwise non-title-like labels need repeated evidence
    across the input. A small set of ordinary prose lead-ins is excluded from
    the title-like shortcut so text such as ``Reason: explanation`` remains
    literal unless it is repeated as a label.
    """

    candidate = speaker_label_candidate(text)
    if candidate is None:
        return None, str(text or "").strip()
    label, payload = candidate
    words = label.replace("-", " ").replace("_", " ").split()
    title_like = (
        1 <= len(words) <= 6
        and any(character.isalpha() for character in label)
        and all(
            word[0].isupper() or not any(character.isalpha() for character in word)
            for word in words
        )
    )
    normalized_repeated = {str(value).casefold() for value in repeated_speakers}
    repeated = label.casefold() in normalized_repeated
    if (not title_like and not repeated) or (
        title_like and label.casefold() in _PROSE_LABELS and not repeated
    ):
        return None, str(text or "").strip()
    return label, payload
