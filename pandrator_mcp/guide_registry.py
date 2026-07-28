"""Versioned, deterministic product guidance packaged with the sidecar."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .schemas.guidance import GUIDE_TOPICS

_TOPIC_ALIASES = {
    "artifact": "artifacts-and-revisions",
    "artifacts": "artifacts-and-revisions",
    "artifact revisions": "artifacts-and-revisions",
    "revisions": "artifacts-and-revisions",
    "audiobook": "audiobooks",
    "durable work": "durable-work",
    "durable workflow": "durable-work",
    "durable workflows": "durable-work",
    "jobs": "durable-work",
    "work": "durable-work",
    "manager": "manager-and-recovery",
    "manager recovery": "manager-and-recovery",
    "recovery": "manager-and-recovery",
    "provider": "providers-and-voices",
    "providers": "providers-and-voices",
    "providers and voices": "providers-and-voices",
    "remote": "remote-targets",
    "remote target": "remote-targets",
    "server": "remote-targets",
    "lan": "remote-targets",
    "pod": "remote-targets",
    "security": "security-boundaries",
    "security boundary": "security-boundaries",
    "threat model": "security-boundaries",
    "subtitle": "subtitles",
    "voiceover": "voiceover-and-dubbing",
    "voice over": "voiceover-and-dubbing",
    "dubbing": "voiceover-and-dubbing",
    "workflow": "workflows",
}


def normalize_guide_topic(value: str) -> str:
    """Resolve a human topic phrase to one canonical packaged guide name."""

    supplied = " ".join(
        str(value).strip().lower().replace("_", " ").replace("/", " ").split()
    )
    hyphenated = supplied.replace(" ", "-")
    if hyphenated in GUIDE_TOPICS:
        return hyphenated
    return _TOPIC_ALIASES.get(supplied, hyphenated)


class GuideEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    title: str
    summary: str
    audiences: tuple[str, ...]
    related_tools: tuple[str, ...] = ()
    minimum_application_version: str = "0.6.0"
    minimum_manager_version: str | None = None
    revision: int = Field(ge=1)
    file: str


class GuideRegistry:
    def __init__(self) -> None:
        root = resources.files("pandrator_mcp").joinpath("guides")
        payload = json.loads(root.joinpath("index.json").read_text(encoding="utf-8"))
        values = payload.get("guides") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise RuntimeError("The packaged Pandrator guide index is invalid.")
        entries = tuple(GuideEntry.model_validate(value) for value in values)
        self._root = root
        self._entries = {entry.topic: entry for entry in entries}
        if len(self._entries) != len(entries):
            raise RuntimeError("Packaged Pandrator guide topics must be unique.")
        if set(self._entries) != set(GUIDE_TOPICS):
            raise RuntimeError(
                "Packaged Pandrator guide topics and the MCP tool schema differ."
            )

    def list(self) -> tuple[GuideEntry, ...]:
        return tuple(self._entries[name] for name in sorted(self._entries))

    def index(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "items": [entry.model_dump(mode="json") for entry in self.list()],
        }

    def get(self, topic: str) -> dict[str, Any]:
        selected = normalize_guide_topic(topic)
        try:
            entry = self._entries[selected]
        except KeyError as error:
            available = ", ".join(sorted(self._entries))
            raise ValueError(
                f"Unknown Pandrator guide topic {topic!r}. Available: {available}."
            ) from error
        text = self._root.joinpath(entry.file).read_text(encoding="utf-8")
        return {
            "schema_version": "1",
            "topic": entry.topic,
            "title": entry.title,
            "summary": entry.summary,
            "audiences": list(entry.audiences),
            "revision": entry.revision,
            "content": text,
            "related_tools": list(entry.related_tools),
        }
