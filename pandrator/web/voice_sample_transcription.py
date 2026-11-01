"""Public voice transcription choices and revision-fenced workspace settings."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.stt_provider_profiles import CLOUD_STT_ENGINE_IDS

from .accounts.execution_actor import HostActor, current_execution_actor
from .credential_identity import credential_namespace
from .models import WorkspaceSetting
from .settings_policy import BUILTIN_DEFAULTS, validate_stt_settings
from .voice_library import resolve_voice_sample_transcription_settings

_SETTING_KEYS = ("services.stt", "defaults.stt")
_LOCAL_ENGINES = frozenset({"auto", "whisper", "parakeet", "moss", "qwen3"})


class VoiceTranscriptionChoices(BaseModel):
    """Per-operation controls; connection and executable settings are server-owned."""

    model_config = ConfigDict(extra="forbid")

    stt_engine: StrictStr | None = Field(default=None, min_length=1, max_length=80)
    stt_backend: StrictStr | None = Field(default=None, min_length=1, max_length=80)
    stt_language: StrictStr | None = Field(default=None, min_length=1, max_length=40)
    stt_compute_backend: Literal["auto", "cpu", "cuda", "vulkan", "metal"] | None = None
    stt_model_quantization: StrictStr | None = Field(default=None, min_length=1, max_length=40)
    qwen_asr_model: Literal["qwen3_asr_0_6b", "qwen3_asr_1_7b"] | None = None
    qwen_asr_backend: Literal["auto", "cpu", "cuda", "vulkan", "metal"] | None = None
    transcription_vocal_isolation: (
        Literal["off", "bs_roformer", "mel_band_roformer", "htdemucs"] | None
    ) = None
    moss_max_chunk_seconds: float | None = Field(default=None, gt=0, le=600, allow_inf_nan=False)
    moss_vad_enabled: StrictBool | None = None
    moss_ctc_alignment_enabled: StrictBool | None = None
    moss_ctc_padding_seconds: float | None = Field(default=None, ge=0, le=10, allow_inf_nan=False)
    crispasr_vad_enabled: StrictBool | None = None
    crispasr_vad_threshold: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


def _engine(value: object) -> str:
    if type(value) is not str:
        raise ValueError("Choose a configured transcription engine.")
    engine = value.strip().lower().replace("-", "_")
    if engine not in _LOCAL_ENGINES and engine not in CLOUD_STT_ENGINE_IDS:
        raise ValueError("Choose a configured transcription engine.")
    return engine


def _settings(session: Session, workspace_id: str) -> tuple[dict[str, Any], dict[str, int]]:
    rows = session.execute(
        select(
            WorkspaceSetting.key,
            WorkspaceSetting.value_json,
            WorkspaceSetting.revision,
        ).where(
            WorkspaceSetting.workspace_id == workspace_id,
            WorkspaceSetting.key.in_(_SETTING_KEYS),
        )
    ).all()
    values = {key: deepcopy(value) for key, value, _revision in rows if isinstance(value, dict)}
    revisions = {key: 0 for key in _SETTING_KEYS}
    revisions.update({key: revision for key, _value, revision in rows})
    return values, revisions


def _resolved_runtime(values: dict[str, Any], choices: dict[str, Any]) -> dict[str, Any]:
    defaults = values.get("defaults.stt", {})
    connections = values.get("services.stt", {})
    engine = _engine(choices.get("stt_engine") or defaults.get("stt_engine") or "auto")
    records = connections.get("provider_configs", [])
    if not isinstance(records, list):
        records = []
    selected = next(
        (
            row
            for row in records
            if isinstance(row, dict)
            and row.get("enabled") is not False
            and str(row.get("id") or "").strip().lower().replace("-", "_") == engine
        ),
        None,
    )
    if engine in CLOUD_STT_ENGINE_IDS and selected is None:
        raise ValueError("The selected transcription service is not configured in this workspace.")
    provider_defaults = selected.get("settings", {}) if selected else {}
    runtime = {
        **deepcopy(BUILTIN_DEFAULTS["stt"]),
        **deepcopy(defaults),
        **deepcopy(connections),
        **(deepcopy(provider_defaults) if isinstance(provider_defaults, dict) else {}),
        **choices,
        "stt_engine": engine,
        "stt_backend": engine,
        "provider_configs": [deepcopy(selected)] if selected else [],
    }
    return runtime


def capture_voice_transcription_settings(
    session: Session,
    workspace_id: str,
    requested: dict[str, Any],
    voice_language: str | None,
) -> dict[str, Any]:
    """Capture public choices after authority/replay; never put connections in a job."""
    if credential_namespace(session, workspace_id).legacy_host_access:
        return {"settings": resolve_voice_sample_transcription_settings(requested, voice_language)}
    choices = VoiceTranscriptionChoices.model_validate(requested).model_dump(exclude_none=True)
    left, right = choices.get("stt_engine"), choices.get("stt_backend")
    if left and right and _engine(left) != _engine(right):
        raise ValueError("stt_engine and stt_backend must select the same engine.")
    values, revisions = _settings(session, workspace_id)
    default = values.get("defaults.stt", {}).get("stt_engine") or "auto"
    choices["stt_engine"] = _engine(left or right or default)
    choices = resolve_voice_sample_transcription_settings(choices, voice_language)
    choices["stt_backend"] = choices["stt_engine"]
    validate_stt_settings(choices)
    _resolved_runtime(values, choices)
    return {"settings": choices, "stt_settings_revisions": revisions}


def check_voice_transcription_settings(
    session: Session,
    payload: dict[str, Any],
    workspace_id: str,
) -> None:
    """Fail closed if workspace configuration changed after admission."""
    expected = payload.get("stt_settings_revisions")
    if expected is None:
        if (
            type(current_execution_actor.get()) is HostActor
            or credential_namespace(session, workspace_id).legacy_host_access
        ):
            return
        raise ValueError("This transcription has no captured workspace settings. Submit it again.")
    if (
        type(expected) is not dict
        or set(expected) != set(_SETTING_KEYS)
        or any(type(value) is not int or value < 0 for value in expected.values())
    ):
        raise ValueError("This transcription has invalid captured workspace settings.")
    current = {key: 0 for key in _SETTING_KEYS}
    rows = session.execute(
        select(
            WorkspaceSetting.key,
            WorkspaceSetting.revision,
        ).where(
            WorkspaceSetting.workspace_id == workspace_id,
            WorkspaceSetting.key.in_(_SETTING_KEYS),
        )
    ).all()
    current.update({key: revision for key, revision in rows})
    if current != expected:
        raise ValueError(
            "Workspace transcription settings changed. Submit the transcription again."
        )


def resolve_voice_transcription_settings(
    session: Session,
    payload: dict[str, Any],
    workspace_id: str,
) -> dict[str, Any]:
    """Resolve only the captured selection from its unchanged workspace namespace."""
    check_voice_transcription_settings(session, payload, workspace_id)
    requested = dict(payload.get("settings") or {})
    if "stt_settings_revisions" not in payload:
        return requested
    choices = VoiceTranscriptionChoices.model_validate(requested).model_dump(exclude_none=True)
    values, revisions = _settings(session, workspace_id)
    if revisions != payload["stt_settings_revisions"]:
        raise ValueError(
            "Workspace transcription settings changed. Submit the transcription again."
        )
    return _resolved_runtime(values, choices)
