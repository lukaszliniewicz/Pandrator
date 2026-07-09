"""Shared data models for Pandrator-native dubbing services."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SubtitleSegment:
    """A parsed subtitle segment with millisecond timing."""

    index: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class SpeechBlock:
    """Speech block JSON entry imported into Pandrator sentence generation."""

    number: str
    text: str
    subtitles: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "text": self.text,
            "subtitles": list(self.subtitles),
        }


@dataclass(frozen=True)
class AudioAlignmentBlock:
    """Generated audio mapped to a subtitle timing window."""

    number: str
    text: str
    start_ms: int
    end_ms: int
    audio_files: list[Path] = field(default_factory=list)
    subtitles: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "audio_files": [str(path) for path in self.audio_files],
            "subtitles": list(self.subtitles),
        }


@dataclass(frozen=True)
class DubbingArtifact:
    """Named file produced or consumed by a dubbing stage."""

    role: str
    path: Path
    is_current: bool = True
