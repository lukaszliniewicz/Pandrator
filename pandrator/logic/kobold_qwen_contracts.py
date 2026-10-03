"""Kobold Qwen batch capability, audio event and reader-message contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, NotRequired, TypedDict

from pydub import AudioSegment


class KoboldQwenBatchCapabilities(TypedDict):
    supported: bool
    streaming: bool
    default_batch_size: int
    max_batch_size: int
    endpoint: NotRequired[str]
    protocol: NotRequired[str]
    parallelism: NotRequired[int]


class KoboldQwenBatchAudioEvent(TypedDict):
    id: str
    audio: AudioSegment | None
    error: Mapping[str, object] | None


KoboldQwenBatchMessage = (
    tuple[Literal["event"], KoboldQwenBatchAudioEvent]
    | tuple[Literal["error"], Exception]
    | tuple[Literal["done"], None]
)
