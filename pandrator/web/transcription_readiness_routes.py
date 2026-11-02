"""Read-only HTTP endpoint for selected local transcription model files."""

from __future__ import annotations

import json
from typing import Any

from flask import jsonify, request

from pandrator.logic.dubbing import crispasr, qwen_alignment, qwen_asr
from pandrator.logic.dubbing.stt_backends import (
    STT_ENGINE_QWEN3_ALIASES,
    normalize_stt_backend,
)
from pandrator.logic.dubbing.stt_provider_profiles import CLOUD_STT_ENGINE_IDS
from pandrator.logic.dubbing.transcription_readiness import (
    is_named_vad_selection,
)

from .access.contracts import ProjectAction
from .domain_blueprints import DomainBlueprints
from .http_session_access import require_session_action
from .route_context import RouteContext
from .settings_policy import adapt_runtime_settings

MAXIMUM_READINESS_REQUEST_BYTES = 32 * 1024

_ENGINE_FIELDS = ("stt_engine", "stt_backend", "engine")
_QUANTIZATION_FIELDS = (
    "stt_model_quantization",
    "crispasr_model_quantization",
    "parakeet_quantization",
)
_LANGUAGE_FIELDS = ("stt_language", "whisper_language")
_ALLOWED_FIELDS = frozenset(
    {
        *_ENGINE_FIELDS,
        *_QUANTIZATION_FIELDS,
        *_LANGUAGE_FIELDS,
        "stt_lid_backend",
        "crispasr_vad_enabled",
        "crispasr_vad_model",
        "moss_vad_enabled",
        "moss_ctc_alignment_enabled",
        "moss_ctc_aligner_model",
        "qwen_asr_model",
        "qwen_asr_chunk_mode",
    }
)
_ENGINE_ALIASES = {
    "auto",
    "automatic",
    "parakeet_preferred",
    "whisper",
    "whisper_cpp",
    "whisperx",
    "faster_whisper",
    "parakeet",
    "parakeet_onnx",
    "onnx_parakeet",
    "parakeet_tdt_0.6b_v3",
    "nemo_parakeet_tdt_0.6b_v3",
    "moss",
    "moss_diarize",
    "moss_transcribe_diarize",
    "moss_transcribe_diarize_0.9b",
    "moss_diarize_0.9b",
    *{
        str(value).strip().lower().replace("-", "_").replace(" ", "_")
        for value in STT_ENGINE_QWEN3_ALIASES
    },
    *CLOUD_STT_ENGINE_IDS,
}
_QUANTIZATION_ALIASES = {
    *crispasr.MODEL_QUANTIZATION_ALIASES,
    *crispasr.MODEL_QUANTIZATION_ALIASES.values(),
    "f16",
}
_LID_BACKENDS = {"", "auto", "whisper", "off", "none", "disabled", "ecapa"}
_MOSS_ALIGNER_ALIASES = {
    "auto",
    "canary-ctc-aligner",
    crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT.filename,
    *qwen_alignment.ALIASES,
}


class ReadinessRequestError(ValueError):
    """A safe, user-independent error for invalid readiness selections."""


def _normalized_token(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _select_alias(
    settings: dict[str, Any], fields: tuple[str, ...], label: str
) -> Any | None:
    supplied = [settings[field] for field in fields if field in settings]
    if not supplied:
        return None
    first = supplied[0]
    if any(item != first for item in supplied[1:]):
        raise ReadinessRequestError(f"Conflicting {label} selections.")
    return first


def _validate_overrides(
    supplied: object, stored: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(supplied, dict):
        raise ReadinessRequestError("settings must be an object.")
    if any(not isinstance(key, str) or key not in _ALLOWED_FIELDS for key in supplied):
        raise ReadinessRequestError("Only transcription selection fields are accepted.")

    overrides = dict(supplied)
    for fields, label in (
        (_ENGINE_FIELDS, "engine"),
        (_QUANTIZATION_FIELDS, "quantization"),
        (_LANGUAGE_FIELDS, "language"),
    ):
        selected = _select_alias(overrides, fields, label)
        for field in fields:
            overrides.pop(field, None)
        if selected is not None:
            canonical = fields[0]
            if not isinstance(selected, str) or len(selected) > 80:
                raise ReadinessRequestError(f"Invalid {label} selection.")
            if label == "engine":
                token = _normalized_token(selected)
                if token not in _ENGINE_ALIASES:
                    raise ReadinessRequestError("Invalid engine selection.")
                selected = normalize_stt_backend(selected)
            elif label == "quantization":
                normalized_quant = crispasr.normalize_model_quantization_alias(selected)
                if normalized_quant not in _QUANTIZATION_ALIASES:
                    raise ReadinessRequestError("Invalid quantization selection.")
                selected = normalized_quant
            overrides[canonical] = selected

    for field in ("crispasr_vad_enabled", "moss_vad_enabled", "moss_ctc_alignment_enabled"):
        if field in overrides and type(overrides[field]) is not bool:
            raise ReadinessRequestError(f"{field} must be a boolean.")

    for field in ("stt_language", "stt_lid_backend"):
        if field in overrides and (
            not isinstance(overrides[field], str) or len(overrides[field]) > 80
        ):
            raise ReadinessRequestError(f"Invalid {field} selection.")
    if "stt_lid_backend" in overrides:
        backend = str(overrides["stt_lid_backend"]).strip().lower()
        if backend not in _LID_BACKENDS:
            raise ReadinessRequestError("Invalid language-detection backend selection.")
        overrides["stt_lid_backend"] = backend

    if "crispasr_vad_model" in overrides:
        selection = overrides["crispasr_vad_model"]
        if not isinstance(selection, str) or len(selection) > 4096:
            raise ReadinessRequestError("Invalid VAD selection.")
        if not is_named_vad_selection(selection):
            stored_selection = str(stored.get("crispasr_vad_model") or "silero")
            if selection != stored_selection:
                raise ReadinessRequestError(
                    "Save the custom VAD file selection before assessing it."
                )

    if "moss_ctc_aligner_model" in overrides:
        selection = overrides["moss_ctc_aligner_model"]
        if not isinstance(selection, str) or len(selection) > 4096:
            raise ReadinessRequestError("Invalid aligner selection.")
        if selection.strip().lower() not in _MOSS_ALIGNER_ALIASES:
            stored_selection = str(stored.get("moss_ctc_aligner_model") or "auto")
            if selection != stored_selection:
                raise ReadinessRequestError(
                    "Save the custom aligner selection before assessing it."
                )

    if "qwen_asr_model" in overrides:
        try:
            overrides["qwen_asr_model"] = qwen_asr.normalize_qwen_asr_model(
                overrides["qwen_asr_model"]
            )
        except (TypeError, ValueError, qwen_asr.QwenASRError) as error:
            raise ReadinessRequestError("Invalid Qwen3 model selection.") from error

    if "qwen_asr_chunk_mode" in overrides:
        mode = overrides["qwen_asr_chunk_mode"]
        if not isinstance(mode, str) or mode.strip().lower() not in qwen_asr.QWEN3_CHUNK_MODES:
            raise ReadinessRequestError("Invalid Qwen3 chunk mode selection.")
        overrides["qwen_asr_chunk_mode"] = mode.strip().lower()

    return overrides


def register_transcription_readiness_routes(
    app: DomainBlueprints, context: RouteContext
) -> None:
    require_scope = context.guards.require_scope
    error_response = context.guards.error_response
    workspace_settings = context.services.workspace_settings

    @app.post(
        "/api/v1/sessions/<session_id>/transcription-readiness"
    )
    @require_scope("app.read")
    @require_session_action(context, ProjectAction.READ)
    def transcription_readiness(session_id: str):
        if (
            request.content_length is not None
            and request.content_length > MAXIMUM_READINESS_REQUEST_BYTES
        ):
            return error_response(
                "request_too_large",
                "The transcription-readiness request exceeds the size limit.",
                413,
            )
        body = request.stream.read(MAXIMUM_READINESS_REQUEST_BYTES + 1)
        if len(body) > MAXIMUM_READINESS_REQUEST_BYTES:
            return error_response(
                "request_too_large",
                "The transcription-readiness request exceeds the size limit.",
                413,
            )
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return error_response(
                "validation_error", "The request body must be valid JSON.", 422
            )
        if not isinstance(payload, dict) or set(payload) != {"settings"}:
            return error_response(
                "validation_error",
                "The request must contain only a settings object.",
                422,
            )

        try:
            stored_result = workspace_settings.get(session_id, "stt")
            stored = adapt_runtime_settings(
                "stt", dict(stored_result.get("effective") or {})
            )
            overrides = _validate_overrides(payload["settings"], stored)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except (ReadinessRequestError, ValueError) as error:
            return error_response("validation_error", str(error), 422)

        from pandrator.logic.dubbing.transcription_readiness import (
            diagnose_transcription_readiness,
        )

        selected = {**stored, **overrides}
        return jsonify(diagnose_transcription_readiness(selected))
