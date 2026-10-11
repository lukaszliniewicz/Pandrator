"""Worker entry points for durable sound-library jobs."""

from __future__ import annotations

import threading
from typing import Any, Callable

from .sound_service import SoundService


class SoundJobHandlers:
    def __init__(self, sounds: SoundService) -> None:
        self.sounds = sounds

    def generate_sound(
        self,
        payload: dict[str, Any],
        progress: Callable[[float, str | None], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        return self.sounds.run_generate(payload, progress, cancel_event)


__all__ = ["SoundJobHandlers"]
