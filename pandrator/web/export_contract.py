"""Immutable export intent shared by queue submission and worker execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .source_resolution import PrimarySourceResolution


EXPORT_CONTRACT_VERSION = 1
EXPORT_MODES = frozenset({"media", "subtitles", "text"})
AUDIO_MODE_ALIASES = {
    "preserve": "preserve",
    "source": "preserve",
    "mixed": "mixed",
    "dubbing_only": "dubbing_only",
    "dubbed": "dubbing_only",
}


def normalize_export_mode(value: Any, *, workflow_kind: str) -> str:
    fallback = "subtitles" if workflow_kind == "subtitles" else "media"
    normalized = str(value or fallback).strip().lower()
    if workflow_kind == "subtitles" and normalized not in {"subtitles", "text"}:
        raise ValueError("A subtitle workspace export must be subtitles or text.")
    if normalized not in EXPORT_MODES:
        raise ValueError(f"Unsupported export mode: {normalized or '(empty)'}")
    return normalized


def normalize_audio_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    result = AUDIO_MODE_ALIASES.get(normalized)
    if result is None:
        raise ValueError(
            "Voiceover media exports require an explicit audio mode: "
            "preserve, mixed, or dubbing_only."
        )
    return result


def build_export_contract(
    *,
    workflow_kind: str,
    settings: dict[str, Any],
    source: PrimarySourceResolution,
) -> dict[str, Any]:
    """Build and validate the exact export intent before a job is queued."""

    export_mode = normalize_export_mode(
        settings.get("export_mode"),
        workflow_kind=workflow_kind,
    )
    audio_mode = None
    if workflow_kind == "voiceover" and export_mode == "media":
        audio_mode = normalize_audio_mode(settings.get("audio_mode"))
        if audio_mode in {"preserve", "mixed"} and (
            source.artifact is None or not source.has_audio
        ):
            label = "Preserving" if audio_mode == "preserve" else "Mixing"
            raise ValueError(
                f"{label} source audio requires an attached audio or video source. "
                "Attach the intended source or choose Voiceover only."
            )
    return {
        "version": EXPORT_CONTRACT_VERSION,
        "workflow_kind": workflow_kind,
        "export_mode": export_mode,
        "audio_mode": audio_mode,
        "source_artifact_id": source.artifact.id if source.artifact else None,
        "source_content_hash": source.artifact.content_hash if source.artifact else None,
        "source_profile": source.profile,
        "source_resolution": source.resolution,
    }


@dataclass(frozen=True, slots=True)
class ExportContract:
    workflow_kind: str
    export_mode: str
    audio_mode: str | None
    source_artifact_id: str | None
    source_content_hash: str | None
    source_profile: str

    @classmethod
    def verify(
        cls,
        payload: Any,
        *,
        workflow_kind: str,
        settings: dict[str, Any],
    ) -> "ExportContract":
        if not isinstance(payload, dict):
            raise ValueError(
                "This voiceover export predates the explicit export contract. "
                "Submit it again from the Output page."
            )
        if payload.get("version") != EXPORT_CONTRACT_VERSION:
            raise ValueError("The export contract version is unsupported; submit the export again.")
        contract_workflow = str(payload.get("workflow_kind") or "")
        if contract_workflow != workflow_kind:
            raise ValueError("The export contract does not match this session workflow.")
        export_mode = normalize_export_mode(
            payload.get("export_mode"),
            workflow_kind=workflow_kind,
        )
        requested_export_mode = normalize_export_mode(
            settings.get("export_mode"),
            workflow_kind=workflow_kind,
        )
        if export_mode != requested_export_mode:
            raise ValueError("The queued export mode does not match its immutable export contract.")
        audio_mode = None
        if workflow_kind == "voiceover" and export_mode == "media":
            audio_mode = normalize_audio_mode(payload.get("audio_mode"))
            if audio_mode != normalize_audio_mode(settings.get("audio_mode")):
                raise ValueError(
                    "The queued audio mode does not match its immutable export contract."
                )
        return cls(
            workflow_kind=workflow_kind,
            export_mode=export_mode,
            audio_mode=audio_mode,
            source_artifact_id=(
                str(payload.get("source_artifact_id"))
                if payload.get("source_artifact_id")
                else None
            ),
            source_content_hash=(
                str(payload.get("source_content_hash"))
                if payload.get("source_content_hash")
                else None
            ),
            source_profile=str(payload.get("source_profile") or "none"),
        )
