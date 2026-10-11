"""Persistent native Google prompted-voice design and preview handling."""

from __future__ import annotations

import base64
import binascii
import io
import threading
import wave
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

from pandrator.logic import google_tts_catalogue_http, tts_handler
from pandrator.logic.google_voice_scope import google_voice_scope as google_voice_scope
from pandrator.runtime import DataPaths

from .credentials import (
    TTS_SERVICE_ENVS,
    database_reference,
    resolve_provider_credential,
    tts_service_credential_key,
)
from .database import Database
from .models import AppSetting, Artifact, Voice, utcnow

if TYPE_CHECKING:
    from .workflow_voice import VoiceWorkflowContext

_GEMINI_VOICE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/voices"
_VERTEX_VOICE_ENDPOINT = "https://aiplatform.googleapis.com/v1beta1/projects/"
_SUPPORTED_MODELS = (
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
)
_MAX_SAMPLE_BASE64_BYTES = 32 * 1024 * 1024
_MAX_SAMPLE_BYTES = 16 * 1024 * 1024
_MIN_SAMPLE_RATE = 8_000
_MAX_SAMPLE_RATE = 192_000
_PREVIEW_WARNING = (
    "Google created the voice, but its preview audio could not be saved."
)
_UNCERTAIN_WARNING = (
    "Google voice creation could not be confirmed. Refresh Google voices before creating another voice."
)
_QUOTA_WARNING = (
    "Google rejected voice creation because a quota or rate limit was reached (HTTP 429). "
    "Check the project's voice-creation quota before creating another voice."
)
_AUTHENTICATION_WARNING = (
    "Google rejected voice creation because authentication failed (HTTP 401). "
    "Check the selected Google credential before retrying."
)
_PERMISSION_WARNING = (
    "Google rejected voice creation because the credential lacks permission (HTTP 403). "
    "Check the selected project's voice-creation permissions before retrying."
)
_MODEL_WARNING = (
    "Google rejected voice creation because the selected model or endpoint is unavailable "
    "(HTTP 404). Check the provider, region, and model configuration before retrying."
)
_INVALID_REQUEST_WARNING = (
    "Google rejected voice creation because the request is invalid (HTTP {status}). "
    "Check the voice prompt, model, and language settings before retrying."
)
_REJECTED_WARNING = (
    "Google rejected voice creation (HTTP {status}). "
    "Check the selected provider configuration before retrying."
)
_CREATING_WARNING = (
    "Google voice creation is already in progress or has an unresolved outcome."
)
_REJECTION_ERROR_CODES = frozenset(
    {
        "quota_or_rate_limited",
        "authentication_failed",
        "permission_denied",
        "model_or_endpoint_unavailable",
        "invalid_request",
        "request_rejected",
    }
)


def configured_google_design_service(
    database: Database,
    paths: DataPaths,
    service_id: str,
) -> tuple[dict[str, Any], str]:
    """Read configured Google TTS service settings and resolve its credential."""

    if service_id not in {"gemini", "vertex_ai"}:
        raise ValueError("Google voice design supports Gemini and Vertex AI only.")
    with database.session() as session:
        defaults_row = session.get(AppSetting, "defaults.tts")
        connections_row = session.get(AppSetting, "services.tts")
        defaults = (
            deepcopy(defaults_row.value_json)
            if defaults_row is not None and isinstance(defaults_row.value_json, dict)
            else {}
        )
        connections = (
            deepcopy(connections_row.value_json)
            if connections_row is not None
            and isinstance(connections_row.value_json, dict)
            else {}
        )

    service_settings = {**defaults, **connections}
    service = tts_handler.get_service_config(service_settings, service_id)
    if not isinstance(service, dict):
        raise ValueError("The selected Google TTS service is not configured.")
    service = deepcopy(service)
    secret_reference = str(
        service.get("secret_ref")
        or database_reference(tts_service_credential_key(service_id))
    )
    fallback_environment_variable = str(
        service.get("api_key_env") or TTS_SERVICE_ENVS.get(service_id, "")
    ).strip()
    try:
        resolved = resolve_provider_credential(
            database,
            paths,
            service_id,
            secret_reference,
            fallback_environment_variable=fallback_environment_variable,
        )
        credential = resolved.resolved_value()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("The configured Google TTS credential could not be resolved.") from None
    return service, credential


def _sample_audio_to_wav(sample_audio: object) -> bytes | None:
    if not isinstance(sample_audio, dict):
        return None
    encoded = sample_audio.get("data")
    mime_type = sample_audio.get("mime_type")
    if not isinstance(encoded, str) or not encoded or not isinstance(mime_type, str):
        return None
    if len(encoded) > _MAX_SAMPLE_BASE64_BYTES:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    if not raw or len(raw) > _MAX_SAMPLE_BYTES:
        return None

    parts = [part.strip() for part in mime_type.split(";")]
    media_type = parts[0].casefold()
    parameters: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, value = part.partition("=")
        if separator:
            parameters[key.strip().casefold()] = value.strip().strip('"')

    if media_type in {"audio/wav", "audio/x-wav"}:
        if not raw.startswith(b"RIFF") or raw[8:12] != b"WAVE":
            return None
        try:
            with wave.open(io.BytesIO(raw), "rb") as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                if (
                    wav.getnchannels() != 1
                    or wav.getsampwidth() != 2
                    or wav.getcomptype() != "NONE"
                    or frames <= 0
                    or not _MIN_SAMPLE_RATE <= rate <= _MAX_SAMPLE_RATE
                    or len(wav.readframes(frames)) != frames * 2
                ):
                    return None
        except (wave.Error, EOFError, OSError):
            return None
        return raw

    if media_type != "audio/l16" or len(raw) % 2:
        return None
    try:
        sample_rate = int(parameters["rate"])
        channels = int(parameters["channels"])
    except (KeyError, ValueError):
        return None
    if (
        channels != 1
        or not _MIN_SAMPLE_RATE <= sample_rate <= _MAX_SAMPLE_RATE
    ):
        return None
    output = io.BytesIO()
    try:
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(raw)
    except (wave.Error, OSError):
        return None
    return output.getvalue()


def _safe_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str)
        and isinstance(item, int)
        and not isinstance(item, bool)
        and item >= 0
        and key in {"total_input_tokens", "total_output_tokens", "total_tokens"}
    }


def _load_voice(database: Database, voice_id: str) -> Voice | None:
    with database.session() as session:
        voice = session.get(Voice, voice_id)
        if voice is None:
            return None
        session.expunge(voice)
        return voice


def _design_metadata(voice: Voice) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = deepcopy(voice.metadata_json or {})
    design = metadata.get("google_voice_design")
    if not isinstance(design, dict):
        design = {}
        metadata["google_voice_design"] = design
    return metadata, design


def _update_voice(
    database: Database,
    voice_id: str,
    mutate: Callable[[Voice, dict[str, Any], dict[str, Any]], None],
) -> Voice:
    with database.immediate_session() as session:
        voice = session.get(Voice, voice_id)
        if voice is None:
            raise ValueError("The local Google voice record no longer exists.")
        metadata, design = _design_metadata(voice)
        mutate(voice, metadata, design)
        voice.metadata_json = metadata
        voice.revision += 1
        voice.updated_at = utcnow()
        session.flush()
        session.expunge(voice)
        return voice


def _scope_for_payload(
    service_id: str,
    service: dict[str, Any],
    credential: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    scope = google_voice_scope(service_id, service, credential)
    expected_fingerprint = str(payload.get("scope_fingerprint") or "")
    expected_project = str(payload.get("project") or "")
    if (
        scope["fingerprint"] != expected_fingerprint
        or scope["project"] != expected_project
    ):
        raise ValueError(
            "Google voice settings or credentials changed after this design was queued."
        )
    return scope


def _creation_endpoint_and_headers(
    service_id: str,
    service: dict[str, Any],
    credential: str,
    project: str,
) -> tuple[str, dict[str, str], bool]:
    if service_id == "gemini":
        return (
            _GEMINI_VOICE_ENDPOINT,
            {"x-goog-api-key": credential},
            False,
        )
    try:
        token, token_project = tts_handler._vertex_access_token(
            {**service, "api_key": credential}
        )
    except Exception:
        raise ValueError("Google Vertex AI credentials could not be refreshed.") from None
    resolved_project = str(token_project or "").strip()
    if resolved_project != project:
        raise ValueError("Google Vertex AI project changed before voice creation.")
    endpoint = (
        f"{_VERTEX_VOICE_ENDPOINT}{quote(project, safe='')}/locations/global/voices"
    )
    return endpoint, {"Authorization": f"Bearer {token}"}, True


def _receipt(
    voice: Voice,
    service_id: str,
    model: str,
    design: dict[str, Any],
) -> dict[str, Any]:
    provider_voice_id = str(design.get("provider_voice_id") or "")
    if not provider_voice_id:
        raise ValueError(_CREATING_WARNING)
    result: dict[str, Any] = {
        "voice_id": voice.id,
        "provider_voice_id": provider_voice_id,
        "service_id": service_id,
        "model": model,
        "voice_revision": voice.revision,
    }
    artifact_id = str(design.get("preview_artifact_id") or "")
    warning = str(design.get("warning") or "")
    usage = _safe_usage(design.get("usage"))
    if artifact_id:
        result["artifact_id"] = artifact_id
    if warning:
        result["warning"] = warning
    if usage:
        result["usage"] = usage
    return result


def _validate_queued_voice(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    job_id: str,
) -> tuple[Voice, dict[str, Any]]:
    voice_id = str(payload.get("voice_id") or "")
    voice = _load_voice(context.database, voice_id)
    if voice is None:
        raise ValueError("The local Google voice record no longer exists.")
    metadata, design = _design_metadata(voice)
    del metadata
    if str(design.get("job_id") or "") != job_id:
        raise ValueError("The Google voice job no longer matches its local voice record.")
    if voice.description != str(payload.get("prompt") or ""):
        raise ValueError("The Google voice prompt no longer matches its local description.")
    if voice.name != str(payload.get("name") or ""):
        raise ValueError("The Google voice name no longer matches its local record.")
    if str(design.get("state") or "") == "created":
        providers = voice.metadata_json.get("providers") or {}
        provider = providers.get(str(payload.get("service_id") or "")) if isinstance(providers, dict) else None
        if (
            isinstance(provider, dict)
            and str(provider.get("design_job_id") or "") == job_id
        ):
            return voice, design
        raise ValueError(_CREATING_WARNING)
    if str(design.get("state") or "") in {"creating", "uncertain"}:
        raise ValueError(_CREATING_WARNING)
    if str(design.get("state") or "") != "queued":
        raise ValueError("The Google voice design is not queued for creation.")
    return voice, design


def _mark_creating(database: Database, voice_id: str, job_id: str) -> Voice:
    def mutate(voice: Voice, _metadata: dict[str, Any], design: dict[str, Any]) -> None:
        if str(design.get("job_id") or "") != job_id or design.get("state") != "queued":
            raise ValueError(_CREATING_WARNING)
        design["state"] = "creating"

    return _update_voice(database, voice_id, mutate)


def _mark_uncertain(database: Database, voice_id: str, job_id: str) -> None:
    def mutate(_voice: Voice, _metadata: dict[str, Any], design: dict[str, Any]) -> None:
        if str(design.get("job_id") or "") == job_id and design.get("state") == "creating":
            design["state"] = "uncertain"
            design["warning"] = _UNCERTAIN_WARNING

    _update_voice(database, voice_id, mutate)


def _mark_rejected(
    database: Database,
    voice_id: str,
    job_id: str,
    warning: str,
    *,
    error_code: str | None = None,
    http_status: int | None = None,
) -> None:
    def mutate(_voice: Voice, _metadata: dict[str, Any], design: dict[str, Any]) -> None:
        if str(design.get("job_id") or "") == job_id and design.get("state") == "creating":
            design["state"] = "rejected"
            design["warning"] = warning
            if error_code in _REJECTION_ERROR_CODES:
                design["error_code"] = error_code
            if (
                type(http_status) is int
                and 400 <= http_status < 500
                and http_status != 408
            ):
                design["http_status"] = http_status

    _update_voice(database, voice_id, mutate)


def _save_created_identity(
    database: Database,
    *,
    voice_id: str,
    job_id: str,
    service_id: str,
    model: str,
    project: str,
    service: dict[str, Any],
    provider_voice_id: str,
    expire_time: str,
    usage: dict[str, int],
) -> Voice:
    from .workflow_voice import _provider_endpoint_fingerprint

    supported_models = list(_SUPPORTED_MODELS)
    endpoint_fingerprint = _provider_endpoint_fingerprint(
        str(service.get("api_base") or "")
    )

    def mutate(voice: Voice, metadata: dict[str, Any], design: dict[str, Any]) -> None:
        if str(design.get("job_id") or "") != job_id:
            raise ValueError("The Google voice job no longer matches its local voice record.")
        design.update(
            state="created",
            provider_voice_id=provider_voice_id,
            usage=usage,
        )
        if expire_time:
            design["expire_time"] = expire_time
        else:
            design.pop("expire_time", None)
        design.pop("warning", None)
        providers = metadata.get("providers")
        if not isinstance(providers, dict):
            providers = {}
            metadata["providers"] = providers
        provider_metadata: dict[str, Any] = {
            "status": "ready",
            "voice_id": provider_voice_id,
            "provider_voice_id": provider_voice_id,
            "managed_by": "pandrator",
            "protocol": "google_voices",
            "resource_kind": "designed_voice",
            "scope_fingerprint": str(design.get("scope_fingerprint") or ""),
            "project": project,
            "models": supported_models,
            "model": model,
            "endpoint_fingerprint": endpoint_fingerprint,
            "design_job_id": job_id,
        }
        if expire_time:
            provider_metadata["expire_time"] = expire_time
        providers[service_id] = provider_metadata

    return _update_voice(database, voice_id, mutate)


def _save_warning(
    database: Database,
    *,
    voice_id: str,
    job_id: str,
    service_id: str,
    warning: str,
) -> Voice:
    def mutate(voice: Voice, metadata: dict[str, Any], design: dict[str, Any]) -> None:
        if str(design.get("job_id") or "") != job_id or design.get("state") != "created":
            raise ValueError("The created Google voice no longer matches its local record.")
        design["warning"] = warning
        providers = metadata.get("providers")
        if isinstance(providers, dict):
            provider = providers.get(service_id)
            if isinstance(provider, dict):
                provider.pop("preview_artifact_id", None)

    return _update_voice(database, voice_id, mutate)


def _save_preview_artifact_id(
    database: Database,
    *,
    voice_id: str,
    job_id: str,
    service_id: str,
    artifact_id: str,
) -> Voice:
    def mutate(voice: Voice, metadata: dict[str, Any], design: dict[str, Any]) -> None:
        if str(design.get("job_id") or "") != job_id or design.get("state") != "created":
            raise ValueError("The created Google voice no longer matches its local record.")
        design["preview_artifact_id"] = artifact_id
        design.pop("warning", None)
        providers = metadata.get("providers")
        if not isinstance(providers, dict):
            providers = {}
            metadata["providers"] = providers
        provider = providers.get(service_id)
        if not isinstance(provider, dict):
            provider = {}
            providers[service_id] = provider
        provider["preview_artifact_id"] = artifact_id

    return _update_voice(database, voice_id, mutate)


def _write_preview_artifact(
    context: VoiceWorkflowContext,
    *,
    voice: Voice,
    service: dict[str, Any],
    service_id: str,
    model: str,
    language: str,
    job_id: str,
    wav_data: bytes,
) -> Artifact:
    target_dir = context.paths.artifacts / "google-voice-design"
    target = target_dir / f"{voice.id}-{job_id}.wav"
    target_dir.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        output.write(wav_data)
    design_metadata = voice.metadata_json.get("google_voice_design")
    provider_voice_id = (
        str(design_metadata.get("provider_voice_id") or "")
        if isinstance(design_metadata, dict)
        else ""
    )
    if not provider_voice_id:
        raise ValueError("The created Google voice identifier is missing.")
    artifact = context.artifacts.register(
        target,
        kind="audio",
        role="tts_voice_preview",
        metadata={
            "service_id": service_id,
            "service_adapter": str(
                service.get("adapter") or service.get("provider") or service_id
            ),
            "model": model,
            "voice": provider_voice_id,
            "language": language,
            "preview_text": "",
            "sample_kind": "provider_generated",
            "voice_id": voice.id,
            "design_job_id": job_id,
        },
    )
    return artifact


def _job_identifier(payload: dict[str, Any]) -> str:
    return str(payload.get("job_id") or payload.get("_job_id") or "").strip()


def _known_rejection(
    status_code: int | None,
) -> tuple[str, str] | None:
    if type(status_code) is not int or not 400 <= status_code < 500 or status_code == 408:
        return None
    if status_code == 429:
        return "quota_or_rate_limited", _QUOTA_WARNING
    if status_code == 401:
        return "authentication_failed", _AUTHENTICATION_WARNING
    if status_code == 403:
        return "permission_denied", _PERMISSION_WARNING
    if status_code == 404:
        return "model_or_endpoint_unavailable", _MODEL_WARNING
    if status_code in {400, 422}:
        return "invalid_request", _INVALID_REQUEST_WARNING.format(status=status_code)
    return "request_rejected", _REJECTED_WARNING.format(status=status_code)


def design_google_voice(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Create one provider voice, retain its ID, and optionally save its sample."""

    job_id = _job_identifier(payload)
    if not job_id:
        raise ValueError("The Google voice design job identity is missing.")
    voice, design = _validate_queued_voice(context, payload, job_id)
    service_id = str(payload.get("service_id") or "")
    model = str(payload.get("model") or "")
    if model not in _SUPPORTED_MODELS:
        raise ValueError("The Google voice design model is not supported.")
    if design.get("state") == "created":
        return _receipt(voice, service_id, model, design)
    if cancel_event.is_set():
        return {}

    service, credential = configured_google_design_service(
        context.database,
        context.paths,
        service_id,
    )
    if model not in service.get("models", []):
        raise ValueError("The selected Google TTS model is no longer configured.")
    scope = _scope_for_payload(service_id, service, credential, payload)

    if cancel_event.is_set():
        return {}
    try:
        endpoint, headers, vertex = _creation_endpoint_and_headers(
            service_id,
            service,
            credential,
            scope["project"],
        )
    except ValueError:
        raise
    if cancel_event.is_set():
        return {}
    _mark_creating(context.database, voice.id, job_id)
    if cancel_event.is_set():
        def reset_queued(
            _voice: Voice, _metadata: dict[str, Any], current_design: dict[str, Any]
        ) -> None:
            if (
                str(current_design.get("job_id") or "") == job_id
                and current_design.get("state") == "creating"
            ):
                current_design["state"] = "queued"

        _update_voice(context.database, voice.id, reset_queued)
        return {}

    progress(0.2, "Creating Google voice")
    try:
        created = google_tts_catalogue_http.create_google_voice(
            endpoint,
            headers=headers,
            vertex=vertex,
            model=model,
            display_name=str(payload.get("name") or ""),
            prompt=str(payload.get("prompt") or ""),
            language_code=str(payload.get("language") or "en"),
        )
        provider_voice_id = str(created.get("id") or "")
        if not provider_voice_id:
            raise RuntimeError("Google returned no voice identifier.")
    except google_tts_catalogue_http.GoogleVoiceCreationStatusError as exc:
        rejection = _known_rejection(exc.status_code)
        if rejection is not None:
            error_code, warning = rejection
            try:
                _mark_rejected(
                    context.database,
                    voice.id,
                    job_id,
                    warning,
                    error_code=error_code,
                    http_status=exc.status_code,
                )
            except Exception:
                pass
            raise RuntimeError(warning) from None
        try:
            _mark_uncertain(context.database, voice.id, job_id)
        except Exception:
            pass
        raise RuntimeError(_UNCERTAIN_WARNING) from None
    except Exception:
        try:
            _mark_uncertain(context.database, voice.id, job_id)
        except Exception:
            pass
        raise RuntimeError(_UNCERTAIN_WARNING) from None

    usage = _safe_usage(created.get("usage"))
    expire_time = str(created.get("expire_time") or "").strip()
    voice = _save_created_identity(
        context.database,
        voice_id=voice.id,
        job_id=job_id,
        service_id=service_id,
        model=model,
        project=scope["project"],
        service=service,
        provider_voice_id=provider_voice_id,
        expire_time=expire_time,
        usage=usage,
    )
    _metadata, design = _design_metadata(voice)
    if cancel_event.is_set():
        voice = _save_warning(
            context.database,
            voice_id=voice.id,
            job_id=job_id,
            service_id=service_id,
            warning=_PREVIEW_WARNING,
        )
        _metadata, design = _design_metadata(voice)
        return _receipt(voice, service_id, model, design)

    wav_data = _sample_audio_to_wav(created.get("sample_audio"))
    if wav_data is None:
        voice = _save_warning(
            context.database,
            voice_id=voice.id,
            job_id=job_id,
            service_id=service_id,
            warning=_PREVIEW_WARNING,
        )
        _metadata, design = _design_metadata(voice)
        return _receipt(voice, service_id, model, design)

    try:
        artifact = _write_preview_artifact(
            context,
            voice=voice,
            service=service,
            service_id=service_id,
            model=model,
            language=str(payload.get("language") or "en"),
            job_id=job_id,
            wav_data=wav_data,
        )
        voice = _save_preview_artifact_id(
            context.database,
            voice_id=voice.id,
            job_id=job_id,
            service_id=service_id,
            artifact_id=artifact.id,
        )
    except Exception:
        voice = _save_warning(
            context.database,
            voice_id=voice.id,
            job_id=job_id,
            service_id=service_id,
            warning=_PREVIEW_WARNING,
        )
    _metadata, design = _design_metadata(voice)
    progress(1.0, "Google voice created")
    return _receipt(voice, service_id, model, design)
