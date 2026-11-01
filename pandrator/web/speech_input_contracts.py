"""Value contracts for retained speech references; no authority or I/O."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

AUDIO_LIMIT = 5 * 1024 * 1024
JSON_LIMIT = 256 * 1024
TEXT_LIMIT = 64 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
PROJECT_SPEECH_INPUT_KEY = "_project_speech_input"
PROJECT_SPEECH_BINDING_KEY = "_project_speech_binding"
PROJECT_SPEECH_SELECTOR_KEY = "_project_speech_selector_hash"
PROJECT_SPEECH_SOURCE_KEY = "_project_speech_source_identity"


class SpeechInputError(ValueError):
    """A selected or retained speech input is unavailable or inconsistent."""


@dataclass(frozen=True, slots=True)
class SpeechInputOwner:
    workspace_id: str
    project_id: str | None = None
    session_id: str | None = None


def validate_speech_input_pin(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"version", "artifact_id", "content_hash"}:
        raise SpeechInputError("Invalid speech input pin.")
    version, identifier, digest = value["version"], value["artifact_id"], value["content_hash"]
    try:
        valid_id = type(identifier) is str and str(UUID(identifier)) == identifier
    except (ValueError, AttributeError):
        valid_id = False
    if type(version) is not int or version != 1 or not valid_id:
        raise SpeechInputError("Invalid speech input pin.")
    if type(digest) is not str or SHA256.fullmatch(digest) is None:
        raise SpeechInputError("Invalid speech input pin.")
    return dict(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class SpeechInputFile:
    artifact_id: str
    relative_path: str
    content_hash: str
    size_bytes: int
    identity: FileIdentity
    owned: bool = False
    # Exact DB descriptor for borrowed rows; never interpreted as authority.
    row_json: str | None = None


@dataclass(frozen=True, slots=True)
class SpeechInputCapture:
    owner: SpeechInputOwner
    settings_json: str
    requires_files: bool
    source_fingerprint: str | None = None
    source_state_json: str | None = None
    source_relative_path: str | None = None
    source_identity: FileIdentity | None = None
    envelope_json: str | None = None
    voice_id: str | None = None

    @property
    def settings(self) -> dict[str, Any]:
        return json.loads(self.settings_json)


@dataclass(frozen=True, slots=True)
class PreparedSpeechInput:
    owner: SpeechInputOwner
    source_state_json: str
    source_identity: FileIdentity
    source_fingerprint: str
    audio: SpeechInputFile
    snapshot: SpeechInputFile
    directory_relative_path: str | None = None
    directory_identity: tuple[int, int] | None = None
    managed_root: str = ""


@dataclass(frozen=True, slots=True)
class PreparedSpeechSelection:
    """Finite copied metadata for one process-local prepared speech choice."""

    selector_json: str
    binding_json: str
    pin_json: str
    source_json: str | None = None

    @property
    def selector(self) -> dict[str, Any]:
        return json.loads(self.selector_json)

    @property
    def binding(self) -> dict[str, str]:
        return json.loads(self.binding_json)

    @property
    def pin(self) -> dict[str, Any] | None:
        return json.loads(self.pin_json)

    @property
    def source(self) -> dict[str, Any] | None:
        if self.source_json is None:
            return None
        value = json.loads(self.source_json)
        pin = validate_speech_input_pin(self.pin)
        if validate_speech_source_reference(value, pin, self.binding) is None:
            raise SpeechInputError("Invalid prepared speech source identity.")
        return value


MissingSpeechSelection = Callable[
    ["Session", dict[str, Any], dict[str, Any]], PreparedSpeechSelection | None
]
RootSpeechBinding = Callable[["Session", dict[str, Any], bool], dict[str, Any]]
PendingSpeechProof = Callable[
    ["Session", SpeechInputOwner, str | None, dict[str, Any] | None, dict[str, Any]], bool
]


BASE_VOICE_SETTING_KEYS = (
    "voice", "speaker", "voice_id", "model", "xtts_model", "_cast_voice_id",
    PROJECT_SPEECH_INPUT_KEY, PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_SELECTOR_KEY, PROJECT_SPEECH_SOURCE_KEY,
)


def retained_speech_selection(
    settings: dict[str, Any], adapter: str, capability: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Pure eligibility and raw-selector vocabulary shared by run and saved inputs."""
    if adapter != "audio_cpp" or capability.get("voice_mode") in {"prebuilt", "design"}:
        return None
    model = str(capability.get("model") or settings.get("model") or "").strip()
    voice_id = settings.get("voice_id")
    voice = str(settings.get("voice") or settings.get("speaker") or "").strip()
    if not model or not (voice_id or voice):
        return None
    selected = deepcopy(settings)
    selected.update(model=model, xtts_model=model, voice=voice, speaker=voice)
    return {
        "voice": "" if voice_id else voice, "voice_id": voice_id,
        "service": str(settings.get("service") or settings.get("tts_service") or "audio_cpp"),
        "model": model,
    }, selected


def validate_speech_source_reference(
    value: object, pin: dict[str, Any] | None, binding: dict[str, Any],
) -> dict[str, str] | None:
    """Copy finite v2 compatibility facts only for the supplied exact pin/binding."""
    fields = {"version", "pin", "binding", "reference_identity"}
    if (type(value) is not dict or set(value) not in (fields, fields | {"cast_reference_hashes"})
            or type(value["version"]) is not int or value["version"] != 2
            or pin is None or value["pin"] != pin or value["binding"] != binding):
        return None
    if "cast_reference_hashes" in value:
        hashes = value["cast_reference_hashes"]
        if (type(hashes) is not list or not hashes or len(hashes) > JSON_LIMIT // 64
                or any(type(item) is not str or SHA256.fullmatch(item) is None for item in hashes)
                or hashes != sorted(set(hashes))):
            return None
    reference = value["reference_identity"]
    if type(reference) is not dict or set(reference) != {
        "sample_id", "artifact_id", "content_hash", "transcript_hash",
    }:
        return None
    if any(type(reference[name]) is not str or not reference[name]
           or len(reference[name]) > 512 or reference[name] != reference[name].strip()
           for name in ("sample_id", "artifact_id")):
        return None
    if any(type(reference[name]) is not str or SHA256.fullmatch(reference[name]) is None
           for name in ("content_hash", "transcript_hash")):
        return None
    return dict(reference)
