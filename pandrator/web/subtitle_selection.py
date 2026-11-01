"""Pure subtitle selector rules shared by export admission and execution."""

from __future__ import annotations

from typing import Any


class SubtitleSelectionUnavailable(ValueError):
    """A binding subtitle choice cannot be satisfied by the captured inputs."""

    code = "subtitle_document_unavailable"


def normalize_subtitle_selection(value: Any = "auto") -> str:
    selection = str(value).strip().lower() if isinstance(value, str) else ""
    selection = {"both": "dual"}.get(selection, selection)
    if selection not in {"auto", "source", "translation", "dual"}:
        raise ValueError("subtitle_selection must be auto, source, translation, or dual.")
    return selection


def export_requires_captions(
    *, workflow_kind: str, export_mode: str, subtitle_mode: str, source_is_video: bool
) -> bool:
    if workflow_kind == "audiobook":
        return False
    return export_mode in {"subtitles", "text"} or (
        export_mode == "media"
        and source_is_video
        and subtitle_mode in {"soft", "burned", "burn"}
    )


def resolve_subtitle_selection(
    settings: dict[str, Any], *, source_available: bool,
    translation_available: bool, captions_required: bool,
) -> str:
    """Resolve auto; require every component of an explicit caption choice."""
    selection = normalize_subtitle_selection(settings.get("subtitle_selection") or "auto")
    if selection == "auto":
        selection = (
            "dual" if source_available and translation_available
            else "source" if source_available
            else "translation" if translation_available
            else "auto"
        )
    if captions_required:
        missing = []
        if selection in {"source", "dual"} and not source_available:
            missing.append("source")
        if selection in {"translation", "dual"} and not translation_available:
            missing.append("translation")
        if selection == "auto":
            missing.append("source or translation")
        if missing:
            raise SubtitleSelectionUnavailable(
                f"The requested subtitle selection '{selection}' requires an available "
                f"{' and '.join(missing)} subtitle document."
            )
    return selection
