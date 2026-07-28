"""Guidance tool arguments."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .common import ToolInput

GUIDE_TOPICS = (
    "artifacts-and-revisions",
    "audiobooks",
    "durable-work",
    "manager-and-recovery",
    "overview",
    "providers-and-voices",
    "remote-targets",
    "security-boundaries",
    "subtitles",
    "voiceover-and-dubbing",
    "workflows",
)

GuideTopic = Annotated[
    str,
    Field(
        min_length=1,
        max_length=120,
        json_schema_extra={
            "enum": list(GUIDE_TOPICS),
            "description": (
                "A packaged Pandrator guide topic. Use one of the advertised "
                "canonical topic names."
            ),
        },
    ),
]


class ExplainSystemInput(ToolInput):
    topic: GuideTopic = "overview"
    audience: Literal["new_user", "operator", "developer", "administrator"] = "new_user"
    include_live_context: bool = True
