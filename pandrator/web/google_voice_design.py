"""Persistent native Google prompted-voice design and preview handling."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import threading
import uuid
import wave
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

from sqlalchemy.orm import Session

from pandrator.logic import google_tts_catalogue_http, tts_handler
from pandrator.logic.google_voice_scope import google_voice_scope as google_voice_scope
from pandrator.runtime import DataPaths

from .access.voice_sample_execution_policy import require_voice_sample_execution_workspace
from .accounts.execution_actor import HostActor, current_execution_actor
from .credential_identity import credential_namespace
from .credentials import (
    TTS_SERVICE_ENVS,
    database_reference,
    resolve_provider_credential,
    tts_service_credential_key,
)
from .database import Database
from .job_execution import current_job_lease
from .managed_services import configured_tts_provider_ids, normalize_tts_provider_id
from .models import Voice, Workspace, utcnow
from .settings_identity import settings_workspace_id, workspace_setting_in_workspace
from .voice_provider_effects import (
    VoiceProviderEffect,
    VoiceProviderEffectError,
    binding_fingerprint,
)
from .voice_provider_settings import check_voice_provider_settings

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
_PREVIEW_WARNING = "Google created the voice, but its preview audio could not be saved."
_UNCERTAIN_WARNING = "Google voice creation could not be confirmed. Refresh Google voices before creating another voice."
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
_CREATING_WARNING = "Google voice creation is already in progress or has an unresolved outcome."
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
    *,
    workspace_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Read configured Google TTS service settings and resolve its credential."""

    if service_id not in {"gemini", "vertex_ai"}:
        raise ValueError("Google voice design supports Gemini and Vertex AI only.")
    with database.snapshot_session() as session:
        workspace_id = settings_workspace_id(session, workspace_id=workspace_id)
        namespace = credential_namespace(session, workspace_id)
        legacy = namespace.legacy_host_access
        host_allowed = namespace.host_credentials_allowed
        defaults_row = workspace_setting_in_workspace(session, workspace_id, "defaults.tts")
        connections_row = workspace_setting_in_workspace(session, workspace_id, "services.tts")
        defaults = (
            deepcopy(defaults_row.value_json)
            if defaults_row is not None and isinstance(defaults_row.value_json, dict)
            else {}
        )
        connections = (
            deepcopy(connections_row.value_json)
            if connections_row is not None and isinstance(connections_row.value_json, dict)
            else {}
        )

    service_settings = {**defaults, **connections}
    if not legacy:
        raw = [
            record
            for key in ("provider_configs", "service_configs")
            for record in service_settings.get(key, [])
            if isinstance(record, dict)
            and normalize_tts_provider_id(
                record.get("id") or record.get("name") or record.get("provider")
            )
            == service_id
        ]
        if (
            service_id not in configured_tts_provider_ids(service_settings)
            or not raw
            or raw[-1].get("enabled") is False
        ):
            raise ValueError("The selected Google TTS service is not configured or enabled.")
    service = tts_handler.get_service_config(service_settings, service_id)
    if not isinstance(service, dict):
        raise ValueError("The selected Google TTS service is not configured.")
    service = deepcopy(service)
    secret_reference = str(
        service.get("secret_ref") or database_reference(tts_service_credential_key(service_id))
    )
    fallback_environment_variable = (
        str(service.get("api_key_env") or TTS_SERVICE_ENVS.get(service_id, "")).strip()
        if host_allowed
        else ""
    )
    try:
        resolved = resolve_provider_credential(
            database,
            paths,
            service_id,
            secret_reference,
            fallback_environment_variable=fallback_environment_variable,
            workspace_id=workspace_id,
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
    if channels != 1 or not _MIN_SAMPLE_RATE <= sample_rate <= _MAX_SAMPLE_RATE:
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
        and item <= 2**63 - 1
        and key in {"total_input_tokens", "total_output_tokens", "total_tokens"}
    }


def _design_metadata(voice: Voice) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = deepcopy(voice.metadata_json or {})
    design = metadata.get("google_voice_design")
    if not isinstance(design, dict):
        design = {}
        metadata["google_voice_design"] = design
    return metadata, design


def _scope_for_payload(
    service_id: str,
    service: dict[str, Any],
    credential: str,
    payload: dict[str, Any],
    *,
    allow_host_adc: bool = True,
    before_io: Callable[[], None] | None = None,
) -> dict[str, str]:
    scope = google_voice_scope(
        service_id,
        service,
        credential,
        allow_host_adc=allow_host_adc,
        **({"before_io": before_io} if before_io is not None else {}),
    )
    expected_fingerprint = str(payload.get("scope_fingerprint") or "")
    expected_project = str(payload.get("project") or "")
    if scope["fingerprint"] != expected_fingerprint or scope["project"] != expected_project:
        raise ValueError(
            "Google voice settings or credentials changed after this design was queued."
        )
    return scope


def _creation_endpoint_and_headers(
    service_id: str,
    service: dict[str, Any],
    credential: str,
    project: str,
    *,
    allow_host_adc: bool = True,
    before_io: Callable[[], None] | None = None,
) -> tuple[str, dict[str, str], bool]:
    if service_id == "gemini":
        return (
            _GEMINI_VOICE_ENDPOINT,
            {"x-goog-api-key": credential},
            False,
        )
    callback_error: Exception | None = None

    def guard() -> None:
        nonlocal callback_error
        assert before_io is not None
        try:
            before_io()
        except Exception as error:
            callback_error = error
            raise

    token_options: dict[str, Any] = {}
    if before_io is not None:
        token_options["before_io"] = guard
    if not allow_host_adc:
        token_options["allow_host_adc"] = False
    try:
        token, token_project = tts_handler._vertex_access_token(
            {**service, "api_key": credential},
            **token_options,
        )
    except PermissionError:
        raise
    except Exception as exc:
        if callback_error is not None:
            raise callback_error from None
        if isinstance(exc, VoiceProviderEffectError):
            raise
        raise ValueError("Google Vertex AI credentials could not be refreshed.") from None
    resolved_project = str(token_project or "").strip()
    if resolved_project != project:
        raise ValueError("Google Vertex AI project changed before voice creation.")
    endpoint = f"{_VERTEX_VOICE_ENDPOINT}{quote(project, safe='')}/locations/global/voices"
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


def _execution_workspace(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    *,
    session: Session,
    workspace_id: str | None = None,
    voice_id: str | None = None,
) -> Workspace:
    workspace = require_voice_sample_execution_workspace(
        session,
        payload,
        "voice.google_design",
        voice_id=voice_id,
        expected_workspace_id=workspace_id,
    )
    check_voice_provider_settings(session, payload, workspace.id)
    pin = payload.get("google_credential_sha256")
    if "google_credential_sha256" in payload:
        if type(pin) is not str or len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
            raise ValueError("Google voice design has an invalid captured credential fingerprint.")
    elif not (
        type(current_execution_actor.get()) is HostActor
        or credential_namespace(session, workspace.id).legacy_host_access
    ):
        raise ValueError(
            "Google voice design has no captured credential fingerprint. Submit it again."
        )
    return workspace


def design_google_voice(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Create once, retain the remote receipt, then prove each local publication."""
    job_id = _job_identifier(payload)
    voice_id = str(payload.get("voice_id") or "")
    service_id = str(payload.get("service_id") or "")
    model = str(payload.get("model") or "")
    infer_host_root = (
        type(current_execution_actor.get()) is HostActor
        and current_job_lease.get() is None
        and payload.get("workspace_id") is None
    )
    # A direct trusted Host has no Job receipt to read first. Retain its old
    # resource-owned namespace inference; durable jobs always prove their root.
    proof_payload = (
        {key: value for key, value in payload.items() if key != "workspace_id"}
        if infer_host_root
        else payload
    )
    with context.database.snapshot_session() as session:
        workspace = _execution_workspace(
            context,
            proof_payload,
            session=session,
            voice_id=voice_id if infer_host_root else None,
        )
        workspace_id = workspace.id
        host_allowed = credential_namespace(session, workspace_id).host_credentials_allowed
        effect = VoiceProviderEffect.current(
            context.database,
            "voice.google_design",
            workspace_id,
            voice_id,
            service_id,
        )
        if not job_id or (effect.job_id is not None and effect.job_id != job_id):
            raise ValueError("The Google voice design job identity is invalid.")
        retained = effect.previous_result(session)
        if retained is not None:
            return retained
        stored_job = effect._job(session)
        previous = effect._load(stored_job) if stored_job is not None else None
        prepared = previous is not None and previous["state"] == "prepared"
        _execution_workspace(
            context, proof_payload, session=session, workspace_id=workspace_id, voice_id=voice_id
        )
        voice = session.get_one(Voice, voice_id)
        _, design = _design_metadata(voice)
        if (
            design.get("job_id") != job_id
            or voice.name != str(payload.get("name") or "")
            or voice.description != str(payload.get("prompt") or "")
        ):
            raise ValueError("The Google voice design no longer matches its local record.")
        if model not in _SUPPORTED_MODELS:
            raise ValueError("The Google voice design model is not supported.")
        if design.get("state") == "created":
            provider = (voice.metadata_json or {}).get("providers", {}).get(service_id)
            if (
                not isinstance(provider, dict)
                or provider.get("design_job_id") != job_id
                or provider.get("voice_id") != design.get("provider_voice_id")
            ):
                raise ValueError(_CREATING_WARNING)
            preview_ids = tuple(
                str(value)
                for value in (
                    design.get("preview_artifact_id"),
                    provider.get("preview_artifact_id"),
                )
                if value
            )
            if preview_ids:
                require_voice_sample_execution_workspace(
                    session,
                    proof_payload,
                    "voice.google_design",
                    voice_id=voice_id,
                    artifact_ids=preview_ids,
                    expected_workspace_id=workspace_id,
                )
            return _receipt(voice, service_id, model, design)
        if design.get("state") != "queued" and not (prepared and design.get("state") == "creating"):
            raise ValueError(_CREATING_WARNING)
        baseline = (
            previous["baseline_sha256"]
            if prepared and previous is not None
            else binding_fingerprint(voice, service_id)
        )

    def prove(session: Session | None = None, *, created: bool = False) -> Voice:
        if session is None:
            with context.database.snapshot_session() as current:
                return prove(current, created=created)
        _execution_workspace(
            context, proof_payload, session=session, workspace_id=workspace_id, voice_id=voice_id
        )
        if credential_namespace(session, workspace_id).host_credentials_allowed != host_allowed:
            raise ValueError("The host credential policy changed during execution.")
        current = session.get_one(Voice, voice_id)
        _, current_design = _design_metadata(current)
        if (
            current_design.get("job_id") != job_id
            or current.name != str(payload.get("name") or "")
            or current.description != str(payload.get("prompt") or "")
            or current_design.get("service_id", service_id) != service_id
            or current_design.get("model", model) != model
        ):
            raise ValueError("The Google voice design no longer matches its captured source.")
        expected_binding = created_binding if created else baseline
        if binding_fingerprint(current, service_id) != expected_binding:
            raise VoiceProviderEffectError(
                "provider_binding_changed", "The Google provider binding changed."
            )
        if created:
            provider = (current.metadata_json or {}).get("providers", {}).get(service_id)
            if (
                current_design.get("state") != "created"
                or current_design.get("provider_voice_id") != provider_voice_id
                or not isinstance(provider, dict)
                or provider.get("design_job_id") != job_id
                or provider.get("voice_id") != provider_voice_id
            ):
                raise ValueError("The created Google voice no longer matches its binding.")
        elif current_design.get("state") not in {"queued", "creating"}:
            raise ValueError(_CREATING_WARNING)
        return current

    def resolve_current() -> tuple[dict[str, Any], str]:
        prove()
        service, credential = configured_google_design_service(
            context.database,
            context.paths,
            service_id,
            workspace_id=workspace_id,
        )
        prove()
        pin = payload.get("google_credential_sha256")
        if pin is not None and hashlib.sha256(credential.encode()).hexdigest() != pin:
            raise ValueError("Google voice credentials changed after this design was queued.")
        if model not in service.get("models", []):
            raise ValueError("The selected Google TTS model is no longer configured.")
        return service, credential

    def before_auth() -> None:
        if cancel_event.is_set():
            raise ValueError("Google voice creation was cancelled before dispatch.")
        resolve_current()

    if cancel_event.is_set():
        return {}
    service, credential = resolve_current()
    scope = _scope_for_payload(
        service_id,
        service,
        credential,
        payload,
        allow_host_adc=host_allowed,
        before_io=before_auth,
    )
    endpoint, headers, vertex = _creation_endpoint_and_headers(
        service_id,
        service,
        credential,
        scope["project"],
        allow_host_adc=host_allowed,
        before_io=before_auth,
    )
    if cancel_event.is_set():
        return {}
    progress(0.2, "Creating Google voice")
    before_auth()
    with context.database.immediate_session() as session:
        current = prove(session)
        metadata, current_design = _design_metadata(current)
        if effect.job_id is None and current_design.get("state") != "queued":
            raise ValueError(_CREATING_WARNING)
        token = effect.reserve(
            {
                "endpoint": endpoint,
                "model": model,
                "name": payload.get("name"),
                "prompt": payload.get("prompt"),
                "language": payload.get("language"),
                "credential_sha256": hashlib.sha256(credential.encode()).hexdigest(),
            },
            baseline,
            lambda current_session: prove(current_session),
            session=session,
        )
        if current_design.get("state") == "queued":
            current_design["state"] = "creating"
            current.metadata_json = metadata
            current.revision += 1
            current.updated_at = utcnow()

    dispatched = False

    def before_create() -> None:
        nonlocal dispatched
        if dispatched:
            raise VoiceProviderEffectError("provider_effect_uncertain", _CREATING_WARNING)
        before_auth()
        effect.dispatch(token, lambda session: prove(session))
        dispatched = True

    def mark_failure(
        state: str, warning: str, *, error_code: str | None = None, status: int | None = None
    ) -> None:
        # A remote failure marker is retained independently; Voice metadata may
        # only be updated under current authority and the original full binding.
        try:
            with context.database.immediate_session() as session:
                current = prove(session)
                metadata, current_design = _design_metadata(current)
                current_design.update(state=state, warning=warning)
                if error_code is not None:
                    current_design["error_code"] = error_code
                    current_design["http_status"] = status
                current.metadata_json = metadata
                current.revision += 1
                current.updated_at = utcnow()
        except Exception:
            return

    try:
        created = google_tts_catalogue_http.create_google_voice(
            endpoint,
            headers=headers,
            vertex=vertex,
            model=model,
            display_name=str(payload.get("name") or ""),
            prompt=str(payload.get("prompt") or ""),
            language_code=str(payload.get("language") or "en"),
            before_io=before_create,
        )
        provider_voice_id = str(created.get("id") or "")
        effect.completed(token, provider_voice_id)
    except Exception as exc:
        if not dispatched:
            # A lost actor leaves the prepared token/creating marker available
            # for a freshly proved lease takeover. Never spend to repair it.
            if cancel_event.is_set():
                mark_failure("queued", "")
            raise
        status = (
            exc.status_code
            if isinstance(exc, google_tts_catalogue_http.GoogleVoiceCreationStatusError)
            else None
        )
        rejection = _known_rejection(status)
        if rejection is not None:
            assert type(status) is int
            error_code, warning = rejection
            effect.rejected(token, status)
            mark_failure("rejected", warning, error_code=error_code, status=status)
            raise RuntimeError(warning) from None
        effect.failed(token)
        mark_failure("uncertain", _UNCERTAIN_WARNING)
        raise RuntimeError(_UNCERTAIN_WARNING) from None

    # Response recording above must finish before any local identity mutation.
    try:
        resolve_current()
        from .workflow_voice import _provider_endpoint_fingerprint

        with context.database.immediate_session() as session:
            current = prove(session)
            metadata, current_design = _design_metadata(current)
            current_design.update(
                state="created",
                provider_voice_id=provider_voice_id,
                usage=_safe_usage(created.get("usage")),
            )
            current_design.pop("warning", None)
            expire_time = str(created.get("expire_time") or "").strip()
            if expire_time:
                current_design["expire_time"] = expire_time
            else:
                current_design.pop("expire_time", None)
            providers = dict(metadata.get("providers") or {})
            providers[service_id] = {
                "status": "ready",
                "voice_id": provider_voice_id,
                "provider_voice_id": provider_voice_id,
                "managed_by": "pandrator",
                "protocol": "google_voices",
                "resource_kind": "designed_voice",
                "scope_fingerprint": payload.get("scope_fingerprint"),
                "project": scope["project"],
                "models": list(_SUPPORTED_MODELS),
                "model": model,
                "design_job_id": job_id,
                "endpoint_fingerprint": _provider_endpoint_fingerprint(
                    str(service.get("api_base") or "")
                ),
                **({"expire_time": expire_time} if expire_time else {}),
            }
            metadata["providers"] = providers
            current.metadata_json = metadata
            current.revision += 1
            current.updated_at = utcnow()
            result = _receipt(current, service_id, model, current_design)
            effect.applied(session, token, "ready", result)
            created_binding = binding_fingerprint(current, service_id)
    except Exception as exc:
        raise VoiceProviderEffectError(
            "provider_effect_local_not_applied",
            "Google created the voice; its receipt was retained and no local binding was applied.",
        ) from exc

    def save_warning() -> dict[str, Any]:
        with context.database.immediate_session() as session:
            current = prove(session, created=True)
            metadata, current_design = _design_metadata(current)
            current_design["warning"] = _PREVIEW_WARNING
            current.metadata_json = metadata
            current.revision += 1
            current.updated_at = utcnow()
            result = _receipt(current, service_id, model, current_design)
            effect.update_result(session, token, result)
            return result

    # This audition comes from the same response. Never issue another provider
    # request, and never discard the already committed identity for media errors.
    prove(created=True)
    if cancel_event.is_set():
        return save_warning()
    wav_data = _sample_audio_to_wav(created.get("sample_audio"))
    if wav_data is None:
        return save_warning()
    target = (
        context.paths.artifacts
        / "google-voice-design"
        / f"{voice_id}-{job_id}-{uuid.uuid4().hex}.wav"
    )
    identity: tuple[int, int] | None = None
    committed = False
    try:
        prove(created=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        prove(created=True)
        with target.open("xb") as output:
            stat = os.fstat(output.fileno())
            identity = stat.st_dev, stat.st_ino
            output.write(wav_data)
        prepared_file = context.artifacts.prepare_registration(target)
        with context.database.immediate_session() as session:
            current = prove(session, created=True)
            artifact = context.artifacts.register_in_session(
                session,
                target,
                kind="audio",
                role="tts_voice_preview",
                workspace_id=workspace_id,
                metadata={
                    "service_id": service_id,
                    "service_adapter": str(
                        service.get("adapter") or service.get("provider") or service_id
                    ),
                    "model": model,
                    "voice": provider_voice_id,
                    "language": str(payload.get("language") or "en"),
                    "preview_text": "",
                    "sample_kind": "provider_generated",
                    "voice_id": voice_id,
                    "design_job_id": job_id,
                },
                _prepared=prepared_file,
            )
            metadata, current_design = _design_metadata(current)
            current_design["preview_artifact_id"] = artifact.id
            current_design.pop("warning", None)
            metadata["providers"][service_id]["preview_artifact_id"] = artifact.id
            current.metadata_json = metadata
            current.revision += 1
            current.updated_at = utcnow()
            result = _receipt(current, service_id, model, current_design)
            effect.update_result(session, token, result)
            created_binding = binding_fingerprint(current, service_id)
        committed = True
    except Exception:
        result = save_warning()
    finally:
        if not committed and identity is not None:
            try:
                stat = target.lstat()
                if (stat.st_dev, stat.st_ino) == identity:
                    target.unlink()
            except OSError:
                pass
    progress(1.0, "Google voice created")
    prove(created=True)
    return result
