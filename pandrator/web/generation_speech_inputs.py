"""Run-local retained speech material, fenced by the caller's current proof."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .database import Database
from .project_speech_references import normalize_speech_reference_binding
from .speech_input_contracts import (
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    SpeechInputError,
    SpeechInputOwner,
    canonical_json,
    validate_speech_input_pin,
)
from .speech_inputs import check_speech_input, load_speech_input, speech_input_adapter

_MATERIAL_KEYS = (
    "audio_cpp_voice_ref",
    "audio_cpp_voice_ref_hash",
    "audio_cpp_reference_text",
)


@dataclass(frozen=True, slots=True)
class RetainedGenerationInput:
    """Strict locator metadata; a retained pin is never an authorization grant."""

    pin_json: str
    binding_json: str

    @property
    def pin(self) -> dict[str, Any] | None:
        return json.loads(self.pin_json)

    @property
    def binding(self) -> dict[str, str]:
        return json.loads(self.binding_json)


def retained_generation_input(settings: dict[str, Any]) -> RetainedGenerationInput | None:
    """Validate marked settings without consulting roots, libraries or files."""
    has_pin = PROJECT_SPEECH_INPUT_KEY in settings
    has_binding = PROJECT_SPEECH_BINDING_KEY in settings
    if not has_pin and not has_binding:
        return None
    if not has_pin or not has_binding:
        raise SpeechInputError("Incomplete retained generation speech selection.")
    pin = validate_speech_input_pin(settings[PROJECT_SPEECH_INPUT_KEY])
    try:
        binding = normalize_speech_reference_binding(settings[PROJECT_SPEECH_BINDING_KEY])
        adapter, _registrations = speech_input_adapter(settings)
    except (TypeError, ValueError) as error:
        raise SpeechInputError("Invalid retained generation speech binding.") from error
    voice = str(settings.get("voice") or settings.get("speaker") or "").strip()
    if (adapter, settings.get("model"), voice) != (
        binding["adapter"],
        binding["model"],
        binding["voice"],
    ):
        raise SpeechInputError("Retained speech binding disagrees with selected settings.")
    return RetainedGenerationInput(canonical_json(pin), canonical_json(binding))


class GenerationSpeechInputs:
    """Share only verified ephemeral material within one generation runtime."""

    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        owner: SpeechInputOwner,
        before_io: Callable[[], None],
    ) -> None:
        if not callable(before_io):
            raise TypeError("before_io must be callable.")
        if type(owner) is not SpeechInputOwner or any(
            type(value) is not str or not value or value != value.strip()
            for value in (owner.workspace_id, owner.project_id, owner.session_id)
        ):
            raise SpeechInputError(
                "Generation speech input requires an exact project-session owner."
            )
        self.database = database
        self.paths = paths
        self.owner = owner
        self.before_io = before_io
        self._lock = threading.RLock()
        self._selected: dict[str, str] = {}
        self._material: dict[str, str] = {}

    def check(self, session: Session) -> None:
        """Check only selected graphs after the caller's current execution proof."""
        with self._lock:
            for pin_json, audio_hash in self._selected.items():
                check_speech_input(
                    session, json.loads(pin_json), owner=self.owner, audio_sha256=audio_hash
                )

    def prepare(
        self,
        settings: dict[str, Any],
        *,
        legacy_prepare: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """Materialize a marked pin once, or preserve the original legacy path."""
        retained = retained_generation_input(settings)
        if retained is None:
            return legacy_prepare(settings)
        with self._lock:
            self.before_io()
            result = deepcopy(settings)
            for name in _MATERIAL_KEYS:
                result.pop(name, None)
            pin = retained.pin
            if pin is None:
                return result
            cache_key = canonical_json({"pin": pin, "binding": retained.binding})
            material_json = self._material.get(cache_key)
            if material_json is None:
                loaded = load_speech_input(
                    self.database,
                    self.paths,
                    pin,
                    result,
                    owner=self.owner,
                    before_io=self.before_io,
                )
                material = {name: loaded[name] for name in _MATERIAL_KEYS}
                self._selected[retained.pin_json] = material["audio_cpp_voice_ref_hash"]
                # The final proof includes this newly verified graph before bytes
                # leave the runtime. Failed proof never caches or returns them.
                self.before_io()
                material_json = canonical_json(material)
                self._material[cache_key] = material_json
            result.update(json.loads(material_json))
            return result
