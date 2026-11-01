"""Finite downloadable speech outputs, independent of their old input history."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

COPIED_OUTPUT_KEY = "copied_output"
SAVED_SPEECH_ROLE = "export_saved_speech"
_RAW_ROLE = "assembled_audio"
SOUND_MIX_ROLE = "sound_mix.audio"
_OPTIONAL = {"duration_ms", "segment_count", "format", "bitrate", "metadata", "chapters"}
_TAGS = {"title", "artist", "album", "genre", "language"}
_SAFE_INTEGER = 2**53 - 1
_TEXT_LIMIT = 16 * 1024
_DISPLAY_LIMIT = 1024 * 1024


def _invalid() -> ValueError:
    return ValueError("Invalid saved speech output copy graph.")


def _integer(value: Any, *, minimum: int) -> int:
    if type(value) is not int or not minimum <= value <= _SAFE_INTEGER:
        raise _invalid()
    return value


def _text(value: Any) -> str:
    if type(value) is not str:
        raise _invalid()
    try:
        if len(value.encode("utf-8")) > _TEXT_LIMIT:
            raise _invalid()
    except UnicodeError as error:
        raise _invalid() from error
    return value


def _metadata(
    source: Any, *, canonical: bool, allow_sound_mix: bool = False,
    output_kind: str = "speech_assembly",
) -> dict[str, Any]:
    if type(source) is not dict:
        raise _invalid()
    if canonical:
        capsule = source.get(COPIED_OUTPUT_KEY)
        if (
            set(source) - _OPTIONAL - {COPIED_OUTPUT_KEY}
            or type(capsule) is not dict
            or set(capsule) != {"version", "kind"}
            or type(capsule["version"]) is not int
            or capsule["version"] != 1
            or type(capsule["kind"]) is not str
            or capsule["kind"] not in (
                {"speech_assembly", "sound_mix"} if allow_sound_mix else {"speech_assembly"}
            )
        ):
            raise _invalid()
        output_kind = capsule["kind"]
    elif COPIED_OUTPUT_KEY in source:
        raise _invalid()
    result: dict[str, Any] = {COPIED_OUTPUT_KEY: {"version": 1, "kind": output_kind}}
    for key, minimum in (("duration_ms", 1), ("segment_count", 0)):
        if key in source:
            result[key] = _integer(source[key], minimum=minimum)
    if "format" in source:
        if type(source["format"]) is not str or source["format"] not in {
            "wav",
            "mp3",
            "flac",
            "opus",
        }:
            raise _invalid()
        result["format"] = source["format"]
    if "bitrate" in source:
        value = source["bitrate"]
        if value is not None and (type(value) is not str or len(value) > 64):
            raise _invalid()
        result["bitrate"] = value
    if "metadata" in source:
        tags = source["metadata"]
        if type(tags) is not dict or canonical and set(tags) - _TAGS:
            raise _invalid()
        result["metadata"] = {key: _text(tags[key]) for key in _TAGS if key in tags}
    if "chapters" in source:
        chapters = source["chapters"]
        if type(chapters) is not list or len(chapters) > 10000:
            raise _invalid()
        projected = []
        previous = 0
        for chapter in chapters:
            if type(chapter) is not dict or set(chapter) != {"start_ms", "title"}:
                raise _invalid()
            start = _integer(chapter["start_ms"], minimum=0)
            if start < previous or "duration_ms" in result and start > result["duration_ms"]:
                raise _invalid()
            projected.append({"start_ms": start, "title": _text(chapter["title"])})
            previous = start
        result["chapters"] = projected
    try:
        if (
            len(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
            > _DISPLAY_LIMIT
        ):
            raise _invalid()
    except (ValueError, TypeError, UnicodeError) as error:
        raise _invalid() from error
    return result


def _sound_mix_display(source: Any, mime: str) -> dict[str, Any]:
    """Keep literal tags and actual output duration, without old timing/history."""
    if type(source) is not dict or COPIED_OUTPUT_KEY in source:
        raise _invalid()
    display = {"metadata": source["metadata"]} if "metadata" in source else {}
    timeline = source.get("audio_timeline")
    if timeline is not None:
        if type(timeline) is not dict:
            raise _invalid()
        frames = _integer(timeline.get("total_frames"), minimum=1)
        rate = _integer(timeline.get("sample_rate_hz"), minimum=1)
        display["duration_ms"] = _integer(max(1, (frames * 1000 + rate // 2) // rate), minimum=1)
    if mime in {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}:
        display["format"] = "wav"
    return display


def freeze_saved_speech_output(
    artifact: Mapping[str, Any], *, allow_sound_mix: bool = False,
) -> dict[str, Any]:
    """Project only finite display facts; discarded manifests are never traversed."""
    if not isinstance(artifact, Mapping):
        raise _invalid()
    role = artifact.get("role")
    mime = artifact.get("mime_type")
    state = artifact.get("state")
    if (
        type(role) is not str
        or role not in (
            {_RAW_ROLE, SAVED_SPEECH_ROLE, SOUND_MIX_ROLE}
            if allow_sound_mix else {_RAW_ROLE, SAVED_SPEECH_ROLE}
        )
        or artifact.get("kind") != "audio"
        or type(mime) is not str
        or not mime.startswith("audio/")
        or len(mime) <= 6
        or type(state) is not str
        or state not in {"current", "stale"}
    ):
        raise _invalid()
    source = artifact.get("metadata_json")
    if role == SOUND_MIX_ROLE:
        source = _sound_mix_display(source, mime)
    metadata = _metadata(
        source, canonical=role == SAVED_SPEECH_ROLE, allow_sound_mix=allow_sound_mix,
        output_kind="sound_mix" if role == SOUND_MIX_ROLE else "speech_assembly",
    )
    return {**artifact, "role": SAVED_SPEECH_ROLE, "metadata_json": metadata}


def validate_output_copy_graph(
    rows: Mapping[str, list[dict[str, Any]]], *, allow_sound_mix: bool = False,
) -> None:
    """Reject live assemblies, malformed reserved capsules and incoming history."""
    saved = set()
    for artifact in rows.get("artifacts", []):
        role = artifact.get("role")
        metadata = artifact.get("metadata_json")
        marked = isinstance(metadata, Mapping) and COPIED_OUTPUT_KEY in metadata
        if role in {_RAW_ROLE, SOUND_MIX_ROLE}:
            raise _invalid()
        if role == SAVED_SPEECH_ROLE or marked:
            if role != SAVED_SPEECH_ROLE:
                raise _invalid()
            freeze_saved_speech_output(artifact, allow_sound_mix=allow_sound_mix)
            identifier = artifact.get("id")
            if type(identifier) is not str or not identifier:
                raise _invalid()
            saved.add(identifier)
    if saved and any(
        edge.get("child_artifact_id") in saved for edge in rows.get("artifact_edges", [])
    ):
        raise _invalid()
