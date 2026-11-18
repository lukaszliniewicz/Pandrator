"""Which subtitle source a speech plan was prepared from, and whether it still fits.

A plan depends on two narrower things than the artifact's exact identity: the
spoken text of every passage, and the passage windows the plan is fitted to. A
timing-only edit (trimmed cue ends) keeps the first and changes the second, so
its takes stay usable and only the fit needs rebuilding.

Display cue windows are deliberately not part of either signature: a display
reflow (a different line width, say) keeps the passages, so the plan built from
them is unchanged. A saved cue retime rewrites the passage window it owns.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from .logical_passages import source_passages
from .models import Artifact
from .settings_policy import stable_hash

SIGNATURE_VERSION = 1
TIMING_ONLY_WARNING = (
    "Subtitle timing changed after this plan was prepared, but the text did not. "
    "The current audio still follows the plan's timing. Prepare the speech plan "
    "again to fit the new timing: preparing starts no generation, and takes that "
    "can be reused carry forward."
)
# Artifact roles whose passages are the display subtitles a plan is built from.
_SUBTITLE_ROLES = frozenset(
    {"transcription", "correction", "translation", "tts_optimized", "media_edit_subtitles"}
)
_WHITESPACE = re.compile(r"\s+")


def normalized_spoken_text(value: Any) -> str:
    """Stripped text with internal whitespace runs collapsed to one space."""
    return _WHITESPACE.sub(" ", str(value or "")).strip()


def _signature_artifact(session: Session, artifact: Artifact | None) -> Artifact | None:
    """The stored subtitle artifact whose passages a signature describes.

    A reviewed speech optimization has none: its spoken text lives in its own
    file, which the display passages do not show, so two optimizations of the
    same subtitles would look identical. It matches by exact identity only.
    """
    if artifact is None:
        return None
    # Read the stored row: a caller may hold an object from an earlier session.
    stored = session.get(Artifact, artifact.id)
    if stored is None or stored.state == "deleted":
        return None
    if stored.role not in _SUBTITLE_ROLES or stored.role == "tts_optimized":
        return None
    return stored


def _identity(row: Mapping[str, Any], position: int) -> Any:
    passage = row.get("_passage")
    identifier = passage.get("id") if isinstance(passage, Mapping) else None
    if isinstance(identifier, str) and identifier.strip():
        return identifier
    return position


def source_signatures(session: Session, artifact: Artifact | None) -> dict[str, Any] | None:
    """Text and timing signatures of a subtitle source, or ``None``.

    ``None`` means "no evidence": a narration source, or passages that cannot be
    read. Callers must not treat it as a mismatch on its own.
    """
    source = _signature_artifact(session, artifact)
    if source is None:
        return None
    try:
        rows = source_passages(session, source)
    except (KeyError, TypeError, ValueError):
        # Malformed or absent passage evidence is "no evidence", never a
        # mismatch: the caller falls back to the exact artifact identity.
        return None
    if not rows:
        return None
    text: list[list[Any]] = []
    timing: list[list[Any]] = []
    for position, row in enumerate(rows):
        identity = _identity(row, position)
        text.append(
            [identity, normalized_spoken_text(row.get("text")), str(row.get("speaker") or "")]
        )
        timing.append([identity, row.get("start_ms"), row.get("end_ms")])
    return {
        "text": stable_hash(text),
        "timing": stable_hash(timing),
        "version": SIGNATURE_VERSION,
    }


def source_plan_settings(session: Session, artifact: Artifact | None) -> dict[str, Any]:
    """The plan-revision fields that record a subtitle source and its signatures.

    Every subtitle source records its content hash; the text and timing
    signatures are added when its passages can be read.
    """
    if artifact is None:
        return {}
    stored = session.get(Artifact, artifact.id)
    if stored is None or stored.state == "deleted" or stored.role not in _SUBTITLE_ROLES:
        return {}
    settings: dict[str, Any] = {"_source_content_hash": stored.content_hash}
    signatures = source_signatures(session, stored)
    if signatures is not None:
        settings["_source_text_signature"] = signatures["text"]
        settings["_source_timing_signature"] = signatures["timing"]
    return settings


def signature_reader(
    session: Session, artifact: Artifact | None
) -> Callable[[], dict[str, Any] | None]:
    """A callable that reads the current signatures once, when a comparison needs them."""
    cache: list[dict[str, Any] | None] = []

    def read() -> dict[str, Any] | None:
        if not cache:
            cache.append(source_signatures(session, artifact))
        return cache[0]

    return read


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def stored_signatures(
    revision_settings: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The text and timing signatures a revision stored, or ``None`` without them."""
    settings = revision_settings or {}
    text = settings.get("_source_text_signature")
    timing = settings.get("_source_timing_signature")
    return {"text": text, "timing": timing} if text and timing else None


def plan_source_compatibility(
    revision_settings: Mapping[str, Any],
    current: Any,
    current_signatures: Mapping[str, Any] | Callable[[], Mapping[str, Any] | None] | None,
) -> str:
    """How a plan revision's source relates to the current one.

    ``exact``: the same artifact and content hash, or the same text and timing
    in another artifact. ``timing_only``: the same text with different cue
    windows. ``incompatible``: anything else. A revision without a recorded
    source artifact keeps the old "compatible" reading; a revision without
    signatures can only reach ``exact`` or ``incompatible``.

    ``current_signatures`` may be a callable, which is read only when the exact
    identity does not already settle the answer.
    """
    settings = revision_settings or {}
    stored_id = settings.get("_source_artifact_id")
    if not stored_id:
        return "exact"
    current_id = _field(current, "id")
    current_hash = _field(current, "content_hash")
    stored_hash = settings.get("_source_content_hash")
    if (
        current_id
        and stored_id == current_id
        and (stored_hash is None or stored_hash == current_hash)
    ):
        return "exact"
    signatures = current_signatures() if callable(current_signatures) else current_signatures
    stored_text = settings.get("_source_text_signature")
    stored_timing = settings.get("_source_timing_signature")
    current_text = _field(signatures, "text")
    current_timing = _field(signatures, "timing")
    if stored_text and stored_timing and current_text and current_timing:
        if stored_text == current_text:
            return "exact" if stored_timing == current_timing else "timing_only"
    return "incompatible"


def revision_source_compatibility(
    session: Session,
    revision_settings: Mapping[str, Any],
    artifact: Artifact | None,
) -> str:
    """``plan_source_compatibility`` for a live artifact, reading signatures only when needed."""
    return plan_source_compatibility(
        revision_settings, artifact, signature_reader(session, artifact)
    )
