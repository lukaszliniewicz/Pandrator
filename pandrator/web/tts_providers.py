"""Typed TTS provider boundary and catalogue use cases."""

from __future__ import annotations

import logging
import socket
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, replace
from threading import Lock, RLock
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import requests
from pydub import AudioSegment
from sqlalchemy import select

from pandrator.logic import tts_handler
from pandrator.logic.tts_endpoint_transport import EndpointSessionPool
from pandrator.logic.tts_language_support import tts_language_support as tts_language_support
from pandrator.logic.tts_provider_policy import (
    DEFAULT_TTS_SERVICE_ID,
    provider_policy,
)
from pandrator.logic.tts_provider_profiles import (
    AUDIO_CPP_MODEL_CATALOG as AUDIO_CPP_MODEL_CATALOG,
)
from pandrator.logic.tts_provider_profiles import (
    AUDIO_CPP_VOICE_DESIGN_MODELS,
    list_tts_provider_profiles,
)
from pandrator.runtime import DataPaths

from .credentials import (
    TTS_SERVICE_ENVS,
    ProviderCredentialInput,
    ResolvedCredential,
    credential_backend,
    credential_reference_input,
    database_reference,
    provider_credential_status,
    redact_inline_secrets,
    resolve_provider_credential,
    resolve_provider_credentials,
    resolve_secret_reference,
    tts_credential_key,
    tts_service_credential_key,
)
from .database import Database
from .managed_services import (
    binding_for_provider,
    configured_tts_provider_ids,
    effective_tts_connection_mode,
)
from .manager_proxy import LocalManagerProxy, ManagerProxyError
from .models import AppSetting, Artifact
from .settings_policy import BUILTIN_DEFAULTS
from .tts_catalogue_projection import (
    _LIVE_LANGUAGE_NATIVE_ROUTES as _LIVE_LANGUAGE_NATIVE_ROUTES,
)
from .tts_catalogue_projection import (
    _NATIVE_LANGUAGE_PROVIDER_BY_ADAPTER as _NATIVE_LANGUAGE_PROVIDER_BY_ADAPTER,
)
from .tts_catalogue_projection import (
    _SENSITIVE_SUPPORT_TEXT_MARKERS as _SENSITIVE_SUPPORT_TEXT_MARKERS,
)
from .tts_catalogue_projection import (
    _VERIFIED_LIVE_LANGUAGE_ROW as _VERIFIED_LIVE_LANGUAGE_ROW,
)
from .tts_catalogue_projection import (
    COMPACT_TTS_SERVICE_FIELDS as COMPACT_TTS_SERVICE_FIELDS,
)
from .tts_catalogue_projection import (
    MAX_TTS_DETAIL_MODEL_IDS as MAX_TTS_DETAIL_MODEL_IDS,
)
from .tts_catalogue_projection import (
    MAX_TTS_SERVICE_FILTER_IDS as MAX_TTS_SERVICE_FILTER_IDS,
)
from .tts_catalogue_projection import (
    SLIM_LANGUAGE_SUPPORT_FIELDS as SLIM_LANGUAGE_SUPPORT_FIELDS,
)
from .tts_catalogue_projection import (
    SLIM_MODEL_CATALOG_FIELDS as SLIM_MODEL_CATALOG_FIELDS,
)
from .tts_catalogue_projection import (
    TTS_CATALOGUE_VIEWS as TTS_CATALOGUE_VIEWS,
)
from .tts_catalogue_projection import (
    TtsCatalogueModelNotFoundError as TtsCatalogueModelNotFoundError,
)
from .tts_catalogue_projection import (
    TtsCatalogueServiceNotFoundError as TtsCatalogueServiceNotFoundError,
)
from .tts_catalogue_projection import (
    _audio_cpp_static_model_catalog as _audio_cpp_static_model_catalog,
)
from .tts_catalogue_projection import (
    _decorate_model_language_support as _decorate_model_language_support,
)
from .tts_catalogue_projection import (
    _dedupe_catalogue_values as _dedupe_catalogue_values,
)
from .tts_catalogue_projection import (
    _filter_service_models as _filter_service_models,
)
from .tts_catalogue_projection import (
    _language_provider_id as _language_provider_id,
)
from .tts_catalogue_projection import (
    _mark_live_language_catalog as _mark_live_language_catalog,
)
from .tts_catalogue_projection import (
    _model_language_metadata as _model_language_metadata,
)
from .tts_catalogue_projection import (
    _project_compact_service as _project_compact_service,
)
from .tts_catalogue_projection import (
    _safe_support_text as _safe_support_text,
)
from .tts_catalogue_projection import (
    _slim_model_catalog as _slim_model_catalog,
)
from .tts_catalogue_projection import (
    _slim_model_fields as _slim_model_fields,
)
from .tts_catalogue_projection import (
    normalize_service_id as normalize_service_id,
)


def _supports_parallel_cloud_synthesis(service: Mapping[str, Any]) -> bool:
    """Keep local compatible servers serial; enable known cloud transports."""
    adapter = normalize_service_id(service.get("adapter"))
    service_id = normalize_service_id(service.get("id") or service.get("name"))
    cloud_ids = {"openai", "gemini", "vertex_ai", "elevenlabs"}
    if adapter in {"azure_speech", "elevenlabs_native"}:
        return True
    if adapter and adapter != "openai_compatible":
        return False
    if service_id in cloud_ids:
        return True
    if adapter == "openai_compatible":
        host = (
            urlparse(
                str(service.get("api_base") or service.get("base_url") or "")
            ).hostname
            or ""
        ).lower()
        return host == "api.openai.com" or any(
            host.endswith(suffix)
            for suffix in (
                ".openai.azure.com",
                ".cognitiveservices.azure.com",
                ".services.ai.azure.com",
                ".inference.ai.azure.com",
                ".models.ai.azure.com",
            )
        )
    return False


@dataclass(frozen=True, slots=True)
class TtsHealth:
    online: bool
    available: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TtsCapabilities:
    synthesis: bool = True
    health: bool = True
    dynamic_catalog: bool = False
    model_upload: bool = False
    voice_upload: bool = False
    voice_delete: bool = False
    batch_synthesis: bool = False
    streaming_batch: bool = False
    parallel_synthesis: bool = False
    default_batch_size: int = 1
    max_batch_size: int = 1


@dataclass(frozen=True, slots=True)
class TtsBatchItem:
    id: str
    text: str
    settings: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TtsBatchResult:
    id: str
    audio: AudioSegment | None = None
    error: TtsProviderError | None = None


@dataclass(frozen=True, slots=True)
class TtsRetryPolicy:
    max_attempts: int = 5
    maximum_delay_seconds: float = 90.0

    @classmethod
    def from_settings(
        cls,
        settings: dict[str, Any],
        *,
        max_attempts: float | str | None = None,
    ) -> TtsRetryPolicy:
        try:
            attempts = int(
                max_attempts
                if max_attempts is not None
                else settings.get("max_attempts") or 5
            )
        except (TypeError, ValueError):
            attempts = 5
        try:
            maximum_delay = float(settings.get("retry_max_delay_seconds") or 90.0)
        except (TypeError, ValueError):
            maximum_delay = 90.0
        return cls(
            max_attempts=max(1, min(20, attempts)),
            maximum_delay_seconds=max(1.0, min(300.0, maximum_delay)),
        )


class TtsProviderError(RuntimeError):
    """Stable provider failure projected across adapter implementations."""

    def __init__(
        self,
        service_id: str,
        operation: str,
        message: str,
        *,
        retryable: bool,
    ):
        super().__init__(message)
        self.service_id = normalize_service_id(service_id)
        self.operation = operation
        self.retryable = retryable


class TtsProviderConfigurationError(TtsProviderError):
    def __init__(self, service_id: str, operation: str, message: str):
        super().__init__(
            service_id,
            operation,
            message,
            retryable=False,
        )


@runtime_checkable
class TtsProviderAdapter(Protocol):
    """Standard operations supported by a TTS provider adapter."""

    service_id: str

    def capabilities(
        self,
        service: dict[str, Any],
    ) -> TtsCapabilities: ...

    def health(self, service: dict[str, Any]) -> TtsHealth: ...

    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]: ...

    def synthesize(
        self,
        text: str,
        settings: dict[str, Any],
        **options: Any,
    ) -> AudioSegment | None: ...

    def upload_voice(
        self,
        wav_file_path: str | list[str],
        *,
        base_url: str,
        service: str,
        prompt_text: str | None = None,
        mode: str | None = None,
        voice_id: str | None = None,
        api_key: str = "",
    ) -> str: ...

    def delete_voice(
        self,
        voice_id: str,
        *,
        base_url: str,
        service: str,
        api_key: str = "",
    ) -> bool: ...


class LegacyTtsAdapter:
    """Adapter for the stable function-based provider implementation."""

    def __init__(self, service_id: str):
        self.service_id = normalize_service_id(service_id)

    def capabilities(
        self,
        service: dict[str, Any],
    ) -> TtsCapabilities:
        return TtsCapabilities(
            dynamic_catalog=self.service_id in {"kobold_qwen", "silero"},
            voice_upload=bool(service.get("supports_voice_cloning")),
            voice_delete=bool(service.get("supports_voice_deletion")),
            parallel_synthesis=self.service_id
            in {
                "openai",
                "gemini",
                "vertex_ai",
                "elevenlabs",
                "openai_compatible",
            },
        )

    def health(self, service: dict[str, Any]) -> TtsHealth:
        parsed = urlparse(str(service.get("api_base") or ""))
        online = False
        if parsed.hostname:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            try:
                with socket.create_connection(
                    (parsed.hostname, port),
                    timeout=0.35,
                ):
                    online = True
            except OSError:
                pass

        if service.get("kind") == "commercial":
            available = bool(service.get("credential_configured"))
            return TtsHealth(
                online=online,
                available=available,
                reason="" if available else "API key not configured",
            )
        return TtsHealth(
            online=online,
            available=online,
            reason="" if online else "Service is not running",
        )

    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        del api_key
        return {}

    def synthesize(
        self,
        text: str,
        settings: dict[str, Any],
        **options: Any,
    ) -> AudioSegment | None:
        try:
            return tts_handler.text_to_audio(text, settings, **options)
        except tts_handler.TtsGenerationError as error:
            raise TtsProviderError(
                self.service_id,
                "synthesize",
                str(error),
                retryable=error.retryable,
            ) from error

    def synthesize_batch(
        self,
        items: list[TtsBatchItem],
        *,
        batch_size: int,
        **options: Any,
    ) -> Iterator[TtsBatchResult]:
        del batch_size
        for item in items:
            try:
                yield TtsBatchResult(
                    id=item.id,
                    audio=self.synthesize(
                        item.text,
                        item.settings,
                        **options,
                    ),
                )
            except Exception as error:  # noqa: BLE001 - stable result boundary
                projected = (
                    error
                    if isinstance(error, TtsProviderError)
                    else TtsProviderError(
                        self.service_id,
                        "synthesize",
                        str(error),
                        retryable=True,
                    )
                )
                yield TtsBatchResult(id=item.id, error=projected)

    def upload_voice(
        self,
        wav_file_path: str | list[str],
        *,
        base_url: str,
        service: str,
        prompt_text: str | None = None,
        mode: str | None = None,
        voice_id: str | None = None,
        api_key: str = "",
    ) -> str:
        return tts_handler.upload_speaker_voice(
            wav_file_path,
            base_url=base_url,
            service=service,
            prompt_text=prompt_text,
            mode=mode,
            voice_id=voice_id,
            api_key=api_key,
        )

    def delete_voice(
        self,
        voice_id: str,
        *,
        base_url: str,
        service: str,
        api_key: str = "",
    ) -> bool:
        return tts_handler.delete_speaker_voice(
            voice_id,
            base_url=base_url,
            service=service,
            api_key=api_key,
        )


class XttsAdapter(LegacyTtsAdapter):
    """Expose XTTS' server-side model and voice catalogues to the UI."""

    def capabilities(
        self,
        service: dict[str, Any],
    ) -> TtsCapabilities:
        return TtsCapabilities(
            dynamic_catalog=True,
            model_upload=True,
            voice_upload=bool(service.get("supports_voice_cloning")),
            voice_delete=bool(service.get("supports_voice_deletion")),
        )

    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        del api_key
        base_url = str(service.get("api_base") or tts_handler.XTTS_API_BASE_URL)
        models = _dedupe_catalogue_values(
            [
                *list(service.get("models") or []),
                *tts_handler.get_xtts_models(base_url),
            ]
        )
        voices = _dedupe_catalogue_values(
            [
                *list(service.get("voices") or []),
                *tts_handler.get_xtts_speakers(base_url),
            ]
        )
        catalogues = {
            str(model): _dedupe_catalogue_values(
                [
                    *list(
                        (service.get("voice_catalogues") or {}).get(model, [])
                        if isinstance(service.get("voice_catalogues"), dict)
                        else []
                    ),
                    *voices,
                ]
            )
            for model in models
        }
        default_model = str(
            service.get("default_model") or tts_handler.XTTS_DEFAULT_MODEL
        )
        if default_model not in models:
            default_model = (
                tts_handler.XTTS_DEFAULT_MODEL
                if tts_handler.XTTS_DEFAULT_MODEL in models
                else (models[0] if models else "")
            )
        default_voice = str(service.get("default_voice") or "")
        if default_voice and default_voice not in voices:
            voices.append(default_voice)
            for catalogue in catalogues.values():
                if default_voice not in catalogue:
                    catalogue.append(default_voice)
        result: dict[str, Any] = {
            "models": models,
            "voices": voices,
            "voice_catalogues": catalogues,
        }
        if default_model:
            result["default_model"] = default_model
        if default_voice:
            result["default_voice"] = default_voice
        return result


class AudioCppAdapter(LegacyTtsAdapter):
    """Adapter for a resident, externally managed audio.cpp server."""

    def __init__(self, service_id: str):
        super().__init__(service_id)
        self._session_pool = EndpointSessionPool()

    def close(self) -> None:
        self._session_pool.close()

    def _session_for_base_url(self, base_url: str) -> requests.Session:
        key = tts_handler._audio_cpp_endpoint_key(base_url)
        return self._session_pool.session_for_key(key, create_session=requests.Session)

    def _session_for(self, settings: dict[str, Any]) -> requests.Session:
        endpoint, error = tts_handler.resolve_openai_audio_endpoint(settings)
        if endpoint is None:
            raise ValueError(error)
        return self._session_for_base_url(str(endpoint.get("base_url") or ""))

    @staticmethod
    def _endpoint_settings(
        settings: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, Any]:
        service = str(settings.get("service") or "").strip().lower()
        override = str(options.get("audio_cpp_base_url") or "").strip()
        if (
            override
            and service in {"audio.cpp", "audio_cpp", "audio-cpp", "audiocpp"}
            and "audio_cpp_base_url" not in settings
        ):
            return {**settings, "audio_cpp_base_url": override}
        return settings

    def synthesize(
        self,
        text: str,
        settings: dict[str, Any],
        **options: Any,
    ) -> AudioSegment | None:
        with (
            nullcontext() if "request_session" in options else self._session_pool.operation()
        ):
            if "request_session" not in options:
                options["request_session"] = self._session_for(self._endpoint_settings(settings, options))
            return super().synthesize(text, settings, **options)

    def capabilities(
        self,
        service: dict[str, Any],
    ) -> TtsCapabilities:
        del service
        return TtsCapabilities(
            dynamic_catalog=True,
            batch_synthesis=False,
            streaming_batch=False,
            default_batch_size=1,
            max_batch_size=1,
        )

    def health(self, service: dict[str, Any]) -> TtsHealth:
        base_url = str(service.get("api_base") or "").strip().rstrip("/")
        if not base_url:
            return TtsHealth(False, False, "audio.cpp API base is not configured")
        with self._session_pool.operation():
            try:
                session = self._session_for_base_url(base_url)
                with tts_handler._audio_cpp_endpoint_lock_for(base_url):
                    response = session.get(f"{base_url}/health", timeout=2)
                    response.raise_for_status()
                    payload = response.json()
            except (requests.exceptions.RequestException, ValueError) as error:
                return TtsHealth(False, False, f"audio.cpp health check failed: {error}")
            ready = isinstance(payload, dict) and payload.get("status") == "ok"
            return TtsHealth(
                ready,
                ready,
                "" if ready else "audio.cpp returned an invalid health response",
            )

    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        with self._session_pool.operation():
            return self._enrich_catalog(service, api_key=api_key)

    def _enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        base_url = str(service.get("api_base") or "").strip()
        auth_mode = str(service.get("auth_mode") or "none").strip().lower()
        headers = (
            {"Authorization": f"Bearer {api_key}"}
            if api_key and auth_mode not in {"", "none"}
            else {}
        )
        request_session = self._session_for_base_url(base_url)
        live_model_catalog = tts_handler.get_audio_cpp_model_catalog(
            base_url,
            models_path=str(service.get("models_path") or "/v1/models"),
            headers=headers,
            request_session=request_session,
        )
        static_catalog = _audio_cpp_static_model_catalog(service)
        static_by_id = {
            str(item.get("id") or "").strip(): item
            for item in static_catalog
            if str(item.get("id") or "").strip()
        }
        catalog_by_id: dict[str, dict[str, Any]] = {}
        live_model_ids = {
            str(item.get("id") or "").strip()
            for item in live_model_catalog
            if str(item.get("id") or "").strip()
        }
        for item in live_model_catalog:
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            enriched = dict(static_by_id.get(model_id) or {})
            enriched.update(item)
            inferred = tts_handler._audio_cpp_model_metadata(model_id, service)
            for key in (
                "family",
                "voice_mode",
                "experimental",
                "license",
                "usage_note",
            ):
                if key not in enriched and key in inferred:
                    enriched[key] = inferred[key]
            catalog_by_id[model_id] = enriched
        model_catalog = list(catalog_by_id.values())
        models = list(catalog_by_id)
        configured_voices = _dedupe_catalogue_values(list(service.get("voices") or []))
        raw_catalogues = service.get("voice_catalogues")
        configured_catalogues: dict[str, Any] = (
            raw_catalogues if isinstance(raw_catalogues, dict) else {}
        )
        voice_catalogues: dict[str, list[str]] = {}
        for model in models:
            values = [
                *list(configured_catalogues.get(model) or []),
                *configured_voices,
            ]
            if model in live_model_ids:
                values.extend(
                    tts_handler.get_audio_cpp_voice_catalog(
                        base_url,
                        model,
                        voices_path=str(
                            service.get("voices_path") or "/v1/audio/voices"
                        ),
                        headers=headers,
                        request_session=request_session,
                    )
                )
            voice_catalogues[model] = _dedupe_catalogue_values(values)
        configured_default = str(service.get("default_model") or "").strip()
        default_model = (
            configured_default
            if configured_default in models
            else (models[0] if models else "")
        )
        configured_voice = str(service.get("default_voice") or "").strip()
        if default_model and configured_voice:
            voice_catalogues[default_model] = _dedupe_catalogue_values(
                [configured_voice, *voice_catalogues.get(default_model, [])]
            )
        voices = _dedupe_catalogue_values(
            [voice for model in models for voice in voice_catalogues[model]]
        )
        model_voice_modes = {}
        for model in models:
            catalog_item = catalog_by_id.get(model) or {}
            mode = str(catalog_item.get("voice_mode") or "").strip().lower()
            if not mode:
                mode = str(
                    tts_handler._audio_cpp_model_metadata(model, service).get(
                        "voice_mode"
                    )
                    or "cloning"
                )
            model_voice_modes[model] = mode
        result: dict[str, Any] = {
            "models": models,
            "model_catalog": model_catalog,
            "voices": voices,
            "voice_catalogues": voice_catalogues,
            "model_voice_modes": model_voice_modes,
            "supports_dynamic_catalog": True,
            "supports_batch_synthesis": False,
            "batch_synthesis": {
                "supported": False,
                "streaming": False,
                "default_batch_size": 1,
                "max_batch_size": 1,
                "parallelism": 1,
            },
        }
        if default_model:
            result["default_model"] = default_model
        if configured_voice:
            result["default_voice"] = configured_voice
        return result

    @staticmethod
    def _batch_key(settings: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
        endpoint, error = tts_handler.resolve_openai_audio_endpoint(settings)
        if endpoint is None:
            raise ValueError(error)
        if (
            tts_handler._normalize_custom_adapter(endpoint.get("adapter"))
            != "audio_cpp"
        ):
            raise ValueError("The selected endpoint is not an audio.cpp service.")
        return (
            tts_handler._audio_cpp_endpoint_key(str(endpoint.get("base_url") or "")),
            tuple(
                tts_handler._audio_cpp_endpoint_lock_key(url)
                for url in tts_handler._audio_cpp_endpoint_lock_urls(endpoint)
            ),
        )

    def synthesize_batch(
        self,
        items: list[TtsBatchItem],
        *,
        batch_size: int,
        **options: Any,
    ) -> Iterator[TtsBatchResult]:
        if not items:
            return
        with (
            nullcontext() if "request_session" in options else self._session_pool.operation()
        ):
            yield from self._synthesize_batch(items, batch_size=batch_size, **options)

    def _synthesize_batch(
        self,
        items: list[TtsBatchItem],
        *,
        batch_size: int,
        **options: Any,
    ) -> Iterator[TtsBatchResult]:
        batch_settings = self._endpoint_settings(items[0].settings, options)
        batch_key = self._batch_key(batch_settings)
        if any(
            self._batch_key(self._endpoint_settings(item.settings, options)) != batch_key
            for item in items[1:]
        ):
            raise ValueError("Every audio.cpp batch item must use the same endpoint.")

        size = max(1, min(32, int(batch_size or 1)))
        batch_options = dict(options)
        if "request_session" not in batch_options:
            batch_options["request_session"] = self._session_for(batch_settings)
        with tts_handler.audio_cpp_endpoint_lock(batch_settings, options.get("cancel_event")):
            for start in range(0, len(items), size):
                for item in items[start : start + size]:
                    try:
                        audio = self.synthesize(
                            item.text,
                            item.settings,
                            **batch_options,
                        )
                        if audio is None:
                            raise TtsProviderError(
                                self.service_id,
                                "synthesize_batch",
                                "audio.cpp synthesis ended without returning audio.",
                                retryable=True,
                            )
                        yield TtsBatchResult(
                            id=item.id,
                            audio=audio,
                        )
                    except Exception as error:  # noqa: BLE001 - result boundary
                        projected = (
                            error
                            if isinstance(error, TtsProviderError)
                            else TtsProviderError(
                                self.service_id,
                                "synthesize_batch",
                                str(error),
                                retryable=not isinstance(error, ValueError),
                            )
                        )
                        yield TtsBatchResult(id=item.id, error=projected)


class KoboldQwenAdapter(LegacyTtsAdapter):
    def capabilities(
        self,
        service: dict[str, Any],
    ) -> TtsCapabilities:
        base_url = str(service.get("api_base") or tts_handler.KOBOLD_QWEN_API_BASE_URL)
        advertised = tts_handler.get_kobold_qwen_batch_capabilities(
            base_url,
            api_key=str(service.get("api_key") or ""),
        )
        supported = bool(
            advertised.get("supported") and advertised.get("protocol") == "ndjson-v1"
        )
        return TtsCapabilities(
            dynamic_catalog=True,
            voice_upload=bool(service.get("supports_voice_cloning")),
            voice_delete=bool(service.get("supports_voice_deletion")),
            batch_synthesis=supported,
            streaming_batch=(supported and bool(advertised.get("streaming"))),
            default_batch_size=int(advertised.get("default_batch_size") or 1),
            max_batch_size=int(advertised.get("max_batch_size") or 1),
        )

    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        base_url = str(service.get("api_base") or tts_handler.KOBOLD_QWEN_API_BASE_URL)
        entries = tts_handler.get_kobold_qwen_voice_catalog(
            base_url,
            api_key=api_key,
        )
        preset_voices = [
            str(item["id"])
            for item in entries
            if str(item.get("type") or "").lower() == "preset"
        ]
        cloned_voices = [
            str(item["id"])
            for item in entries
            if str(item.get("type") or "").lower() != "preset"
        ]
        catalogues = {
            tts_handler.KOBOLD_QWEN_DEFAULT_MODEL: preset_voices,
            "Voice Cloning": cloned_voices,
        }
        active_model = str(
            service.get("default_model") or tts_handler.KOBOLD_QWEN_DEFAULT_MODEL
        )
        advertised = tts_handler.get_kobold_qwen_batch_capabilities(
            base_url,
            api_key=api_key,
        )
        batch_supported = bool(
            advertised.get("supported")
            and advertised.get("streaming")
            and advertised.get("protocol") == "ndjson-v1"
        )
        return {
            "voice_catalogues": catalogues,
            "default_voices": {
                tts_handler.KOBOLD_QWEN_DEFAULT_MODEL: tts_handler.KOBOLD_QWEN_DEFAULT_VOICE,
                "Voice Cloning": tts_handler.KOBOLD_QWEN_SAMPLE_VOICE,
            },
            "voice_metadata": {
                (
                    f"{str(item.get('model') or ('Prebuilt Voices' if item.get('type') == 'preset' else 'Voice Cloning'))!s}:"
                    f"{item['id']}"
                ): item
                for item in entries
            },
            "voices": list(catalogues.get(active_model, [])),
            "supports_batch_synthesis": bool(batch_supported),
            "batch_synthesis": {
                **advertised,
                "supported": batch_supported,
            },
        }

    def synthesize_batch(
        self,
        items: list[TtsBatchItem],
        *,
        batch_size: int,
        **options: Any,
    ) -> Iterator[TtsBatchResult]:
        if not items:
            return
        base_url = str(
            options.get("kobold_qwen_base_url") or tts_handler.KOBOLD_QWEN_API_BASE_URL
        )
        size = max(1, min(32, int(batch_size or 1)))
        for start in range(0, len(items), size):
            chunk = items[start : start + size]
            pending = {item.id: item for item in chunk}
            try:
                for event in tts_handler.iter_kobold_qwen_batch_audio(
                    [
                        {
                            "id": item.id,
                            "text": item.text,
                            "settings": item.settings,
                        }
                        for item in chunk
                    ],
                    base_url=base_url,
                    cancel_event=options.get("cancel_event"),
                ):
                    item_id = str(event.get("id") or "")
                    item = pending.pop(item_id, None)
                    if item is None:
                        continue
                    error_payload = event.get("error")
                    if isinstance(error_payload, dict):
                        detail = str(
                            error_payload.get("detail")
                            or "Qwen batch synthesis failed."
                        )
                        yield TtsBatchResult(
                            id=item_id,
                            error=TtsProviderError(
                                self.service_id,
                                "synthesize_batch",
                                detail,
                                retryable=bool(error_payload.get("retryable")),
                            ),
                        )
                    else:
                        yield TtsBatchResult(
                            id=item_id,
                            audio=event.get("audio"),
                        )
            except Exception as error:  # noqa: BLE001 - fall back per item
                projected = TtsProviderError(
                    self.service_id,
                    "synthesize_batch",
                    str(error),
                    retryable=True,
                )
                for item in chunk:
                    if item.id in pending:
                        yield TtsBatchResult(id=item.id, error=projected)
                continue
            for item in chunk:
                if item.id in pending:
                    yield TtsBatchResult(
                        id=item.id,
                        error=TtsProviderError(
                            self.service_id,
                            "synthesize_batch",
                            "Qwen batch stream ended before this item completed.",
                            retryable=True,
                        ),
                    )


class SileroAdapter(LegacyTtsAdapter):
    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        del api_key
        base_url = str(service.get("api_base") or tts_handler.SILERO_API_BASE_URL)
        model_catalog = _mark_live_language_catalog(
            tts_handler.get_silero_model_catalog(base_url),
            provider_id="silero",
            native_route="silero_audio_speech",
        )
        installed_models = [
            str(item["id"])
            for item in model_catalog
            if isinstance(item.get("status"), dict) and item["status"].get("installed")
        ]
        voice_catalogues: dict[str, list[str]] = {}
        voice_metadata: dict[str, dict[str, Any]] = {}
        defaults_by_language: dict[str, dict[str, str]] = {}
        for model_id in installed_models:
            entries = tts_handler.get_silero_voice_catalog(
                base_url,
                model=model_id,
                include_unavailable=False,
            )
            voice_catalogues[model_id] = [str(item["id"]) for item in entries]
            language_defaults: dict[str, str] = {}
            for item in entries:
                voice_id = str(item["id"])
                voice_metadata[f"{model_id}:{voice_id}"] = item
                language = str(item.get("language") or "")
                if language and language not in language_defaults:
                    language_defaults[language] = voice_id
            defaults_by_language[model_id] = language_defaults

        default_model = str(service.get("default_model") or "")
        if installed_models and default_model not in installed_models:
            default_model = (
                tts_handler.SILERO_DEFAULT_MODEL
                if tts_handler.SILERO_DEFAULT_MODEL in installed_models
                else installed_models[0]
            )
        default_catalogue = voice_catalogues.get(default_model, [])
        default_voice = str(service.get("default_voice") or "")
        if default_catalogue and default_voice not in default_catalogue:
            default_voice = default_catalogue[0]
        result: dict[str, Any] = {
            "model_catalog": model_catalog,
            "voice_catalogues": voice_catalogues,
            "voice_metadata": voice_metadata,
            "default_voices_by_language": defaults_by_language,
            "voices": default_catalogue,
        }
        if installed_models:
            result["models"] = installed_models
            result["default_model"] = default_model
        if default_voice:
            result["default_voice"] = default_voice
        return result


class ElevenLabsAdapter(LegacyTtsAdapter):
    """Adapter for ElevenLabs' native (non-OpenAI-compatible) API."""

    def capabilities(
        self,
        service: dict[str, Any],
    ) -> TtsCapabilities:
        del service
        return TtsCapabilities(
            dynamic_catalog=True,
            parallel_synthesis=True,
        )

    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        base_url = str(service.get("api_base") or tts_handler.ELEVENLABS_API_BASE_URL)
        try:
            model_entries = tts_handler.get_elevenlabs_model_catalog(
                base_url,
                api_key=api_key,
                strict=True,
            )
            model_entries = _mark_live_language_catalog(
                model_entries,
                provider_id="elevenlabs",
                native_route="elevenlabs_text_to_speech",
            )
            voice_entries = tts_handler.get_elevenlabs_voice_catalog(
                base_url,
                api_key=api_key,
                strict=True,
            )
        except tts_handler.ElevenLabsCatalogError as error:
            raise TtsProviderError(
                self.service_id,
                "catalog",
                str(error),
                retryable=error.status_code not in {401, 403},
            ) from error
        configured_models = _dedupe_catalogue_values(service.get("models") or [])
        models = _dedupe_catalogue_values(
            configured_models + [str(item.get("id") or "") for item in model_entries]
        )
        if not models:
            models = [tts_handler.ELEVENLABS_TTS_DEFAULT_MODEL]
        configured_voices = _dedupe_catalogue_values(service.get("voices") or [])
        voices = _dedupe_catalogue_values(
            configured_voices
            + [str(item.get("voice_id") or "") for item in voice_entries]
        )
        default_model = str(service.get("default_model") or "").strip()
        if default_model not in models:
            default_model = models[0]
        default_voice = str(service.get("default_voice") or "").strip()
        if default_voice not in voices:
            default_voice = voices[0] if voices else ""
        catalogue = {model: list(voices) for model in models}
        result: dict[str, Any] = {
            "models": models,
            "voices": voices,
            "voice_catalogues": catalogue,
            "voice_metadata": {
                f"{model}:{voice_id}": item
                for model in models
                for item in voice_entries
                for voice_id in [str(item.get("voice_id") or "").strip()]
                if voice_id
            },
            "model_catalog": model_entries,
            "default_model": default_model,
        }
        if default_voice:
            result["default_voice"] = default_voice
        return result


class TtsProviderRegistry:
    """Resolve all provider operations through one typed adapter contract."""

    BUILTIN_SERVICE_IDS = (
        "audio_cpp",
        "xtts",
        "voxcpm",
        "fishs2",
        "voxtral",
        "kokoro",
        "magpie",
        "silero",
        "chatterbox",
        "kobold_qwen",
        "openai",
        "gemini",
        "vertex_ai",
        "elevenlabs",
        "openai_compatible",
    )

    def __init__(self) -> None:
        self._adapters_guard = Lock()
        self._lifecycle_guard = RLock()
        self._closed = False
        self._adapters: dict[str, TtsProviderAdapter] = {}
        for service_id in self.BUILTIN_SERVICE_IDS:
            self.register(LegacyTtsAdapter(service_id))
        self.replace(XttsAdapter("xtts"))
        self.replace(SileroAdapter("silero"))
        self.replace(KoboldQwenAdapter("kobold_qwen"))
        self.replace(ElevenLabsAdapter(tts_handler.ELEVENLABS_PROVIDER))
        self.replace(AudioCppAdapter(tts_handler.AUDIO_CPP_ADAPTER))

    def register(self, adapter: TtsProviderAdapter) -> None:
        service_id = normalize_service_id(adapter.service_id)
        if not service_id:
            raise ValueError("TTS adapter service ID must not be empty.")
        with self._lifecycle_guard:
            with self._adapters_guard:
                if self._closed:
                    raise RuntimeError("The TTS provider registry is closed.")
            if isinstance(adapter, AudioCppAdapter) and adapter._session_pool.closed:
                raise RuntimeError("Cannot register a retired audio.cpp adapter.")
            with self._adapters_guard:
                if service_id in self._adapters:
                    raise ValueError(f"TTS adapter '{service_id}' is already registered.")
                self._adapters[service_id] = adapter

    def replace(self, adapter: TtsProviderAdapter) -> None:
        service_id = normalize_service_id(adapter.service_id)
        if not service_id:
            raise ValueError("TTS adapter service ID must not be empty.")
        with self._lifecycle_guard:
            with self._adapters_guard:
                if self._closed:
                    raise RuntimeError("The TTS provider registry is closed.")
                previous = self._adapters.get(service_id)
                if previous is adapter:
                    return
            if isinstance(adapter, AudioCppAdapter) and adapter._session_pool.closed:
                raise RuntimeError("Cannot register a retired audio.cpp adapter.")
            with self._adapters_guard:
                self._adapters[service_id] = adapter
                retire_previous = previous is not None and not any(
                    current is previous for current in self._adapters.values()
                )
            if retire_previous and previous is not None:
                self._retire_adapter(previous)

    @staticmethod
    def _retire_adapter(adapter: TtsProviderAdapter) -> None:
        try:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        except Exception:
            logging.exception("Could not close a TTS provider adapter")

    def close(self) -> None:
        with self._lifecycle_guard:
            with self._adapters_guard:
                self._closed = True
                adapters = list(
                    {id(adapter): adapter for adapter in self._adapters.values()}.values()
                )
                self._adapters.clear()
            for adapter in adapters:
                self._retire_adapter(adapter)

    def get(self, service_id: str) -> TtsProviderAdapter:
        normalized = normalize_service_id(service_id)
        with self._adapters_guard:
            if self._closed:
                raise RuntimeError("The TTS provider registry is closed.")
            adapter = self._adapters.get(normalized)
            if adapter is not None:
                return adapter
            # Custom OpenAI-compatible profiles share the stable legacy adapter.
            return self._adapters["openai_compatible"]

    def service_ids(self) -> tuple[str, ...]:
        with self._adapters_guard:
            return tuple(self._adapters)

    def service_id_for_settings(self, settings: dict[str, Any]) -> str:
        service_ids = self.service_ids()
        explicit = normalize_service_id(
            settings.get("preview_service_id") or settings.get("service_id")
        )
        if explicit in service_ids:
            return explicit
        service_name = normalize_service_id(
            settings.get("service") or settings.get("tts_service")
        )
        if service_name in {"custom", "openai_compatible"}:
            adapter_id = normalize_service_id(
                tts_handler.resolve_custom_tts_adapter_id(settings)
            )
            if adapter_id in service_ids:
                return adapter_id
        if explicit:
            return explicit
        return {
            "qwen3_tts": "kobold_qwen",
            "openai_compatible": "openai_compatible",
            "vertex_ai": "vertex_ai",
        }.get(service_name, service_name or "xtts")

    def synthesize(
        self,
        text: str,
        settings: dict[str, Any],
        **options: Any,
    ) -> AudioSegment | None:
        service_id = self.service_id_for_settings(settings)
        adapter = self.get(service_id)
        policy = TtsRetryPolicy.from_settings(
            settings,
            max_attempts=options.pop("max_attempts", None),
        )
        prepared_settings = dict(settings)
        prepared_settings.setdefault(
            "retry_max_delay_seconds",
            policy.maximum_delay_seconds,
        )
        try:
            return adapter.synthesize(
                text,
                prepared_settings,
                max_attempts=policy.max_attempts,
                **options,
            )
        except TtsProviderError:
            raise
        except ValueError as error:
            raise TtsProviderConfigurationError(
                service_id,
                "synthesize",
                str(error),
            ) from error
        except Exception as error:
            raise TtsProviderError(
                service_id,
                "synthesize",
                str(error),
                retryable=True,
            ) from error

    def synthesis_capabilities(
        self,
        settings: dict[str, Any],
        **options: Any,
    ) -> TtsCapabilities:
        service_id = self.service_id_for_settings(settings)
        api_base = ""
        if service_id == "kobold_qwen":
            api_base = str(
                options.get("kobold_qwen_base_url")
                or tts_handler.KOBOLD_QWEN_API_BASE_URL
            )
        elif service_id == "audio_cpp":
            api_base = str(
                options.get("audio_cpp_base_url") or tts_handler.AUDIO_CPP_API_BASE_URL
            )
        capabilities = self.get(service_id).capabilities(
            {
                "id": service_id,
                "api_base": api_base,
                "supports_voice_cloning": service_id
                in {
                    "audio_cpp",
                    "xtts",
                    "voxcpm",
                    "fishs2",
                    "chatterbox",
                    "kobold_qwen",
                },
            }
        )

        if capabilities.parallel_synthesis:
            service = tts_handler.get_service_config(settings, service_id)
            if service is None:
                service, _ = tts_handler.resolve_openai_audio_endpoint(settings)
            capabilities = replace(
                capabilities,
                parallel_synthesis=_supports_parallel_cloud_synthesis(service or {}),
            )
        return capabilities

    def synthesize_batch(
        self,
        items: list[TtsBatchItem],
        *,
        batch_size: int,
        **options: Any,
    ) -> Iterator[TtsBatchResult]:
        if not items:
            return iter(())
        service_id = self.service_id_for_settings(items[0].settings)
        if any(
            self.service_id_for_settings(item.settings) != service_id for item in items
        ):
            raise TtsProviderConfigurationError(
                service_id,
                "synthesize_batch",
                "Every item in a TTS batch must use the same service.",
            )
        adapter = self.get(service_id)
        requested_batch_size = 1
        try:
            requested_batch_size = max(1, int(batch_size or 1))
        except (TypeError, ValueError):
            pass
        if (
            requested_batch_size > 1
            and self.synthesis_capabilities(
                items[0].settings, **options
            ).parallel_synthesis
        ):
            return self._synthesize_parallel_batch(
                items,
                batch_size=requested_batch_size,
                service_id=service_id,
                **options,
            )
        batch_method = getattr(adapter, "synthesize_batch", None)
        if callable(batch_method):
            return batch_method(
                items,
                batch_size=batch_size,
                **options,
            )
        return LegacyTtsAdapter(service_id).synthesize_batch(
            items,
            batch_size=1,
            **options,
        )

    def _synthesize_parallel_batch(
        self,
        items: list[TtsBatchItem],
        *,
        batch_size: int,
        service_id: str,
        **options: Any,
    ) -> Iterator[TtsBatchResult]:
        """Synthesize bounded consecutive waves while preserving input order."""
        workers = max(1, min(8, batch_size))
        cancel_event = options.get("cancel_event")

        def is_cancelled() -> bool:
            return bool(cancel_event is not None and cancel_event.is_set())

        def synthesize_one(item: TtsBatchItem) -> TtsBatchResult | None:
            if is_cancelled():
                return None
            try:
                return TtsBatchResult(
                    id=item.id,
                    audio=self.synthesize(
                        item.text,
                        dict(item.settings),
                        **dict(options),
                    ),
                )
            except Exception as error:  # noqa: BLE001 - stable result boundary
                projected = (
                    error
                    if isinstance(error, TtsProviderError)
                    else TtsProviderError(
                        service_id,
                        "synthesize",
                        str(error),
                        retryable=True,
                    )
                )
                return TtsBatchResult(id=item.id, error=projected)

        for start in range(0, len(items), workers):
            if is_cancelled():
                return
            wave = items[start : start + workers]
            futures = []
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for item in wave:
                    if is_cancelled():
                        break
                    futures.append(executor.submit(synthesize_one, item))
                wave_results = [future.result() for future in futures]
            for result in wave_results:
                if result is not None:
                    yield result
            if is_cancelled():
                return

    def upload_voice(
        self,
        service_id: str,
        wav_file_path: str | list[str],
        **options: Any,
    ) -> str:
        try:
            return self.get(service_id).upload_voice(
                wav_file_path,
                **options,
            )
        except TtsProviderError:
            raise
        except ValueError as error:
            raise TtsProviderConfigurationError(
                service_id,
                "upload_voice",
                str(error),
            ) from error
        except Exception as error:
            raise TtsProviderError(
                service_id,
                "upload_voice",
                str(error),
                retryable=True,
            ) from error

    def delete_voice(
        self,
        service_id: str,
        voice_id: str,
        **options: Any,
    ) -> bool:
        try:
            return self.get(service_id).delete_voice(
                voice_id,
                **options,
            )
        except TtsProviderError:
            raise
        except ValueError as error:
            raise TtsProviderConfigurationError(
                service_id,
                "delete_voice",
                str(error),
            ) from error
        except Exception as error:
            raise TtsProviderError(
                service_id,
                "delete_voice",
                str(error),
                retryable=True,
            ) from error

    def capabilities(
        self,
        service: dict[str, Any],
    ) -> TtsCapabilities:
        service_id = self._service_adapter_id(service)
        capabilities = self.get(service_id).capabilities(service)
        return replace(
            capabilities,
            parallel_synthesis=capabilities.parallel_synthesis
            and _supports_parallel_cloud_synthesis(service),
        )

    def health(self, service: dict[str, Any]) -> TtsHealth:
        service_id = self._service_adapter_id(service)
        try:
            return self.get(service_id).health(service)
        except Exception as error:
            raise TtsProviderError(
                service_id,
                "health",
                str(error),
                retryable=True,
            ) from error

    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        service_id = self._service_adapter_id(service)
        try:
            changes = self.get(service_id).enrich_catalog(service, api_key=api_key)
            from pandrator.logic.speech_performance import decorate_service_capabilities

            effective = {**service, **changes}
            decorate_service_capabilities(effective)
            changes.update({
                "expressive_capabilities": effective["expressive_capabilities"],
                "generation_prompt_models": effective["generation_prompt_models"],
            })
            return changes
        except TtsProviderError:
            raise
        except ValueError as error:
            raise TtsProviderConfigurationError(
                service_id,
                "catalog",
                str(error),
            ) from error
        except Exception as error:
            raise TtsProviderError(
                service_id,
                "catalog",
                str(error),
                retryable=True,
            ) from error

    def _service_adapter_id(self, service: dict[str, Any]) -> str:
        adapter_id = normalize_service_id(service.get("adapter"))
        if adapter_id in self.service_ids():
            return adapter_id
        return normalize_service_id(service.get("id") or service.get("name"))


class TtsCatalogueService:
    """Read and enrich the UI-facing TTS service catalogue."""

    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        providers: TtsProviderRegistry,
        *,
        manager_bridge: LocalManagerProxy | None = None,
    ):
        self.database = database
        self.paths = paths
        self.providers = providers
        self.manager_bridge = manager_bridge or LocalManagerProxy()

    def _settings(
        self,
    ) -> tuple[dict[str, Any], int, dict[str, Any], int]:
        with self.database.session() as db_session:
            connections = db_session.get(AppSetting, "services.tts")
            defaults = db_session.get(AppSetting, "defaults.tts")
            connection_value = (
                dict(connections.value_json or {})
                if connections and isinstance(connections.value_json, dict)
                else {}
            )
            default_value = (
                dict(defaults.value_json or {})
                if defaults and isinstance(defaults.value_json, dict)
                else {}
            )
            if not connection_value and isinstance(
                default_value.get("provider_configs"), list
            ):
                connection_value = {
                    "provider_configs": list(default_value["provider_configs"])
                }
            return (
                connection_value,
                connections.revision if connections else 0,
                default_value,
                defaults.revision if defaults else 0,
            )

    def _credential_details(
        self,
        service: dict[str, Any],
    ) -> tuple[str, str, str]:
        service_id = normalize_service_id(service.get("id") or service.get("name"))
        key_env = str(
            service.get("api_key_env") or TTS_SERVICE_ENVS.get(service_id, "")
        ).strip()
        secret_reference = str(
            service.get("secret_ref")
            or database_reference(tts_service_credential_key(service_id))
        )
        return service_id, key_env, secret_reference

    def _decorate_credentials(
        self,
        service: dict[str, Any],
        *,
        resolved_credential: ResolvedCredential | None = None,
    ) -> None:
        service_id, key_env, secret_reference = self._credential_details(service)
        service["credential_required"] = bool(
            service.get("credential_required")
            or str(service.get("kind") or "").casefold() == "commercial"
        )
        if resolved_credential is None:
            service.update(
                provider_credential_status(
                    self.database,
                    self.paths,
                    service_id,
                    secret_reference,
                    fallback_environment_variable=key_env,
                )
            )
        else:
            service.update(
                {
                    "credential_configured": resolved_credential.configured,
                    "credential_source": resolved_credential.source,
                }
            )
        service["credential_backend"] = credential_backend(secret_reference)
        service["credential_reference"] = credential_reference_input(secret_reference)

    def _resolved_api_key(self, service: dict[str, Any]) -> str:
        service_id, key_env, secret_reference = self._credential_details(service)
        credential = resolve_secret_reference(
            self.database,
            self.paths,
            secret_reference or database_reference(tts_credential_key(service_id)),
            fallback_environment_variable=key_env,
        )
        return credential.resolved_value()

    def _refresh(
        self,
        services: list[dict[str, Any]],
        *,
        resolved_api_keys: Sequence[str] | Mapping[str, str] | None = None,
    ) -> None:
        def probe(service: dict[str, Any]) -> TtsHealth:
            if service.get("connection_mode") == "managed_local":
                managed = service.get("manager_service")
                endpoint = (
                    str(managed.get("endpoint") or "").strip()
                    if isinstance(managed, dict)
                    else ""
                )
                if not endpoint:
                    return TtsHealth(
                        False,
                        False,
                        str(
                            service.get("availability_reason")
                            or "The selected manager-owned service is not available."
                        ),
                    )
            try:
                return self.providers.health(service)
            except TtsProviderError as error:
                return TtsHealth(False, False, str(error))

        with ThreadPoolExecutor(max_workers=min(12, max(1, len(services)))) as executor:
            states = list(executor.map(probe, services))
        online_services: list[tuple[int, dict[str, Any], str]] = []
        for index, (service, state) in enumerate(zip(services, states, strict=True)):
            service.update(
                {
                    "online": state.online,
                    "available": state.available,
                    "availability_reason": state.reason,
                }
            )
            if not state.online:
                continue

            if resolved_api_keys is None:
                api_key = self._resolved_api_key(service)
            elif isinstance(resolved_api_keys, Mapping):
                service_id = normalize_service_id(
                    service.get("id") or service.get("name")
                )
                api_key = str(resolved_api_keys.get(service_id, "") or "")
            else:
                api_key = (
                    str(resolved_api_keys[index] or "")
                    if index < len(resolved_api_keys)
                    else ""
                )
            online_services.append((index, service, api_key))

        def enrich(
            item: tuple[int, dict[str, Any], str],
        ) -> tuple[dict[str, Any] | None, TtsProviderError | None]:
            _index, service, api_key = item
            try:
                return (
                    self.providers.enrich_catalog(
                        service,
                        api_key=api_key,
                    ),
                    None,
                )
            except TtsProviderError as error:
                return None, error

        if not online_services:
            return
        with ThreadPoolExecutor(max_workers=min(12, len(online_services))) as executor:
            enrichments = list(executor.map(enrich, online_services))
        for (_index, service, _api_key), (catalogue, error) in zip(
            online_services,
            enrichments,
            strict=True,
        ):
            if error is not None:
                service.update(
                    {
                        "available": False,
                        "availability_reason": str(error),
                    }
                )
            elif catalogue is not None:
                service.update(catalogue)

    def _project_manager(
        self,
        services: list[dict[str, Any]],
        *,
        configured_provider_ids: frozenset[str],
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "configured": self.manager_bridge.configured,
            "available": False,
        }
        for service in services:
            service["connection_mode"] = effective_tts_connection_mode(
                service,
                configured_provider_ids=configured_provider_ids,
                manager_configured=self.manager_bridge.configured,
            )
        try:
            inventory = self.manager_bridge.inventory()
        except ManagerProxyError as error:
            summary["error"] = {
                "code": error.code,
                "message": str(error),
            }
            for service in services:
                if binding_for_provider(service.get("id")) is not None:
                    service["manager_available"] = False
                    if service.get("connection_mode") == "managed_local":
                        service.update(
                            {
                                "online": False,
                                "available": False,
                                "availability_reason": str(error),
                            }
                        )
            return summary

        summary.update(
            {
                "available": True,
                "status": inventory.get("status") or {},
            }
        )
        components = {
            str((item.get("definition") or {}).get("id") or ""): item
            for item in inventory.get("components") or []
            if isinstance(item, dict)
        }
        managed_services = {
            str(item.get("id") or ""): item
            for item in inventory.get("services") or []
            if isinstance(item, dict)
        }
        for service in services:
            binding = binding_for_provider(service.get("id"))
            if binding is None:
                continue
            component = components.get(binding.component_id, {})
            definition = component.get("definition") or {}
            inspection = component.get("inspection") or {}
            managed = managed_services.get(binding.service_id)
            connection_mode = str(service["connection_mode"])
            service.update(
                {
                    "connection_mode": connection_mode,
                    "manager_available": True,
                    "manager_component_id": binding.component_id,
                    "manager_component_state": str(
                        inspection.get("state") or "unknown"
                    ),
                    "manager_supported_actions": list(
                        definition.get("supported_actions") or []
                    ),
                    "managed_service_id": binding.service_id,
                    "manager_service": managed,
                    "manager_endpoint_read_only": (connection_mode == "managed_local"),
                }
            )
            if connection_mode != "managed_local":
                continue
            if managed is None or not str(managed.get("endpoint") or "").strip():
                service.update(
                    {
                        "online": False,
                        "available": False,
                        "availability_reason": (
                            "The selected manager-owned service is not available."
                        ),
                    }
                )
                continue
            service["api_base"] = str(managed["endpoint"]).rstrip("/")
        return summary

    def _previews(self) -> list[dict[str, Any]]:
        previews: list[dict[str, Any]] = []
        with self.database.session() as db_session:
            preview_artifacts = list(
                db_session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.role == "tts_voice_preview",
                        Artifact.state == "current",
                    )
                    .order_by(Artifact.updated_at.desc())
                ).all()
            )
            for artifact in preview_artifacts:
                metadata = dict(artifact.metadata_json or {})
                if not metadata.get("voice"):
                    continue
                try:
                    if not self.paths.managed_path(artifact.relative_path).is_file():
                        continue
                except ValueError:
                    continue
                previews.append(
                    {
                        "artifact_id": artifact.id,
                        "service_id": str(metadata.get("service_id") or ""),
                        "model": str(metadata.get("model") or ""),
                        "voice": str(metadata.get("voice") or ""),
                        "language": str(metadata.get("language") or ""),
                        "preview_text": str(metadata.get("preview_text") or ""),
                        "updated_at": artifact.updated_at.isoformat(),
                    }
                )
        return previews

    def snapshot(
        self,
        *,
        refresh: bool = False,
        view: str = "full",
        service_ids: Sequence[str] | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Return the UI-facing TTS catalogue.

        ``view="compact"`` is an opt-in slim projection for the session view:
        it skips the audio.cpp static model-catalogue merge, the provider
        profile deepcopy and the preview query, slims each service's
        ``model_catalog`` to chooser-grade entries (id/label/family/
        voice_mode/supported_languages/language_support) while keeping ``voice_metadata``
        verbatim, and projects each service onto
        :data:`COMPACT_TTS_SERVICE_FIELDS`. The default ``"full"`` view is
        unchanged for legacy and MCP consumers. ``service_ids`` restricts
        the payload to selected services (matched by id or name) before any
        refresh probing happens.
        """
        if view not in TTS_CATALOGUE_VIEWS:
            raise ValueError("Unknown TTS catalogue view. Use 'full' or 'compact'.")
        selected = self._normalize_service_selection(service_ids)
        compact = view == "compact"
        (
            services,
            manager,
            connection_value,
            revision,
            default_value,
            default_revision,
            resolved_credentials,
        ) = self._build_services(refresh=refresh, selected=selected, compact=compact)
        if compact:
            payload = {
                "view": "compact",
                "revision": revision,
                "default_service": str(
                    default_value.get("service") or BUILTIN_DEFAULTS["tts"]["service"]
                ),
                "recommended_service": DEFAULT_TTS_SERVICE_ID,
                "default_revision": default_revision,
                "services": redact_inline_secrets(
                    [_project_compact_service(service) for service in services]
                ),
                "manager": manager,
            }
            return payload, revision
        payload = {
            "value": redact_inline_secrets(connection_value),
            "revision": revision,
            "default_value": redact_inline_secrets(default_value),
            "recommended_service": DEFAULT_TTS_SERVICE_ID,
            "default_service": str(
                default_value.get("service") or BUILTIN_DEFAULTS["tts"]["service"]
            ),
            "default_revision": default_revision,
            "builtin_defaults": redact_inline_secrets(BUILTIN_DEFAULTS["tts"]),
            "services": redact_inline_secrets(services),
            "profiles": list_tts_provider_profiles(),
            "previews": self._previews(),
            "manager": manager,
        }
        return payload, revision

    def service_detail(
        self,
        service_id: str,
        *,
        refresh: bool = False,
        models: Sequence[str] | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Return the full catalogue entry for one selected service.

        The returned ``service`` entry is identical to the matching entry of
        the full collection view, so detail consumers can merge it over slim
        compact rows. ``models`` optionally restricts the heavy per-model
        maps (model_catalog, voice_catalogues, voice_metadata) to the chosen
        models so callers never ship all 127 audio.cpp records; id lists and
        defaults stay whole. Server-side limit: the full heavy catalogue is
        still built and then filtered, so this pass wins transfer bytes, not
        server build cost. Unknown ids raise
        :class:`TtsCatalogueServiceNotFoundError`; unknown models raise
        :class:`TtsCatalogueModelNotFoundError`.
        """
        normalized = normalize_service_id(service_id)
        if not normalized or len(normalized) > 64:
            raise ValueError("A TTS service id must be 1 to 64 characters.")
        selected_models = self._normalize_model_selection(models)
        (
            services,
            _manager,
            _connection_value,
            revision,
            default_value,
            default_revision,
            _resolved_credentials,
        ) = self._build_services(refresh=refresh, selected=[normalized], compact=False)
        if not services:  # pragma: no cover - _build_services raises first
            raise TtsCatalogueServiceNotFoundError([service_id])
        service = services[0]
        if selected_models is not None:
            service = _filter_service_models(
                service, selected_models, service_id=normalized
            )
        payload: dict[str, Any] = {
            "view": "detail",
            "revision": revision,
            "default_service": str(
                default_value.get("service") or BUILTIN_DEFAULTS["tts"]["service"]
            ),
            "recommended_service": DEFAULT_TTS_SERVICE_ID,
            "default_revision": default_revision,
            "service": redact_inline_secrets(service),
        }
        if selected_models is not None:
            payload["selected_models"] = selected_models
        return payload, revision

    @staticmethod
    def _normalize_model_selection(
        models: Sequence[str] | None,
    ) -> list[str] | None:
        if models is None:
            return None
        selected: list[str] = []
        for raw in models:
            model_id = str(raw or "").strip()
            if not model_id or len(model_id) > 256:
                raise ValueError(
                    "Each selected TTS model id must be 1 to 256 characters."
                )
            if model_id not in selected:
                selected.append(model_id)
        if not selected:
            raise ValueError("Select at least one TTS model.")
        if len(selected) > MAX_TTS_DETAIL_MODEL_IDS:
            raise ValueError(
                "Select at most "
                f"{MAX_TTS_DETAIL_MODEL_IDS} TTS models per request."
            )
        return selected

    def _normalize_service_selection(
        self,
        service_ids: Sequence[str] | None,
    ) -> list[str] | None:
        if service_ids is None:
            return None
        selected: list[str] = []
        for raw in service_ids:
            normalized = normalize_service_id(raw)
            if not normalized or len(normalized) > 64:
                raise ValueError(
                    "Each TTS service filter id must be 1 to 64 characters."
                )
            if normalized not in selected:
                selected.append(normalized)
        if len(selected) > MAX_TTS_SERVICE_FILTER_IDS:
            raise ValueError(
                "Select at most "
                f"{MAX_TTS_SERVICE_FILTER_IDS} TTS services per request."
            )
        return selected

    @staticmethod
    def _apply_service_selection(
        services: list[dict[str, Any]],
        selected: Sequence[str],
    ) -> list[dict[str, Any]]:
        wanted = set(selected)
        filtered = [
            service
            for service in services
            if normalize_service_id(service.get("id") or service.get("name"))
            in wanted
            or normalize_service_id(service.get("name")) in wanted
        ]
        found = {
            normalize_service_id(service.get("id") or service.get("name"))
            for service in filtered
        } | {
            normalize_service_id(service.get("name")) for service in filtered
        }
        missing = [item for item in selected if item not in found]
        if missing:
            raise TtsCatalogueServiceNotFoundError(missing)
        return filtered

    def _build_services(
        self,
        *,
        refresh: bool,
        selected: Sequence[str] | None,
        compact: bool,
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, Any],
        int,
        dict[str, Any],
        int,
        list[ResolvedCredential],
    ]:
        connection_value, revision, default_value, default_revision = self._settings()
        services = [
            dict(item)
            for item in tts_handler.get_service_configs(
                {**default_value, **connection_value}
            )
        ]
        if selected is not None:
            services = self._apply_service_selection(services, selected)
        manager = self._project_manager(
            services,
            configured_provider_ids=configured_tts_provider_ids(
                default_value,
                connection_value,
            ),
        )
        credential_inputs: list[ProviderCredentialInput] = []
        for service in services:
            service_id, key_env, secret_reference = self._credential_details(service)
            credential_inputs.append((service_id, secret_reference, key_env, True))
        resolved_credentials = resolve_provider_credentials(
            self.database,
            self.paths,
            credential_inputs,
        )
        for service, resolved_credential in zip(
            services,
            resolved_credentials,
            strict=True,
        ):
            if normalize_service_id(service.get("adapter")) == "audio_cpp":
                if not compact:
                    service["model_catalog"] = _audio_cpp_static_model_catalog(service)
            self._decorate_credentials(
                service,
                resolved_credential=resolved_credential,
            )
            service_id = normalize_service_id(service.get("id") or service.get("name"))
            service["supports_parallel_synthesis"] = _supports_parallel_cloud_synthesis(
                service
            )
            if service_id == "xtts":
                capabilities = self.providers.capabilities(service)
                service["supports_dynamic_catalog"] = capabilities.dynamic_catalog
                service["supports_model_upload"] = capabilities.model_upload
        if refresh:
            self._refresh(
                services,
                resolved_api_keys=[
                    resolved.resolved_value() for resolved in resolved_credentials
                ],
            )
        for service in services:
            service.update(
                provider_policy(
                    normalize_service_id(service.get("id") or service.get("name"))
                )
            )
            _decorate_model_language_support(service)
        if compact:
            # Slim after refresh so live discovered records feed the chooser
            # with the same record-over-builtin precedence as the full view.
            for service in services:
                service["model_catalog"] = _slim_model_catalog(service)
        return (
            services,
            manager,
            connection_value,
            revision,
            default_value,
            default_revision,
            resolved_credentials,
        )

    def discovery_api_key(self, service_id: str | None) -> str:
        if not service_id:
            return ""
        connection_value, _, default_value, _ = self._settings()
        service = tts_handler.get_service_config(
            {**default_value, **connection_value},
            service_id,
        )
        if service is None:
            return ""
        normalized = normalize_service_id(service.get("id") or service_id)
        resolved = resolve_provider_credential(
            self.database,
            self.paths,
            normalized,
            service.get("secret_ref")
            or database_reference(tts_service_credential_key(normalized)),
            fallback_environment_variable=str(
                service.get("api_key_env") or TTS_SERVICE_ENVS.get(normalized, "")
            ),
        )
        return resolved.resolved_value()

    def preview_settings(
        self,
        service_id: str,
        *,
        model: str | None,
        voice: str | None,
        language: str | None,
        generation_prompt: str | None = None,
        seed: int | None = None,
        preserve_blank_voice: bool = False,
    ) -> dict[str, Any] | None:
        connection_value, _, default_value, _ = self._settings()
        service = tts_handler.get_service_config(
            {**default_value, **connection_value},
            service_id,
        )
        if service is None:
            return None
        resolved_id = normalize_service_id(service.get("id") or service_id)
        resolved_adapter = str(service.get("adapter") or "").strip().casefold()
        is_audio_cpp = resolved_adapter == "audio_cpp" or resolved_id in {
            "audio_cpp",
            "audio_cpp_experimental",
        }
        resolved_model = model or str(service.get("default_model") or "")
        model_voice_mode = str(
            tts_handler._audio_cpp_model_metadata(resolved_model, service).get(
                "voice_mode"
            )
            or ""
        ).strip().lower()
        if is_audio_cpp and model_voice_mode == "design":
            # Use the same validation as generation, including inferred model
            # metadata for custom audio.cpp model IDs. This constructs a payload
            # only; no provider request or inference is started.
            tts_handler._build_audio_cpp_audio_payload(
                "",
                {
                    "model": resolved_model,
                    "language": language or str(default_value.get("language") or "en"),
                    "generation_prompt": generation_prompt,
                    "audio_cpp_seed": seed,
                },
                service,
            )
        default_voices = (
            service.get("default_voices")
            if isinstance(service.get("default_voices"), dict)
            else {}
        )
        if (
            preserve_blank_voice
            and is_audio_cpp
            and resolved_model.strip().casefold()
            in {item.casefold() for item in AUDIO_CPP_VOICE_DESIGN_MODELS}
            and not str(voice or "").strip()
        ):
            resolved_voice = ""
        else:
            resolved_voice = (
                voice
                or str(default_voices.get(resolved_model) or "")
                or str(service.get("default_voice") or "")
            )
        service_name = (
            tts_handler.OPENAI_COMPAT_SERVICE
            if service.get("is_custom")
            else str(service.get("name") or service_id)
        )
        settings = {
            **BUILTIN_DEFAULTS["tts"],
            **default_value,
            **(
                service.get("settings")
                if isinstance(service.get("settings"), dict)
                else {}
            ),
            **connection_value,
            "service": service_name,
            "model": resolved_model,
            "xtts_model": resolved_model,
            "voice": resolved_voice,
            "speaker": resolved_voice,
            "language": language or str(default_value.get("language") or "en"),
            "preview_service_id": resolved_id,
            "preview_adapter": resolved_adapter,
            "preview_api_base": str(service.get("api_base") or ""),
        }
        normalized_generation_prompt = str(generation_prompt or "").strip()
        if normalized_generation_prompt:
            settings["generation_prompt"] = normalized_generation_prompt
        if seed is not None:
            settings["seed"] = int(seed)
            if is_audio_cpp:
                settings["audio_cpp_seed"] = int(seed)
        if service.get("is_custom"):
            settings["openai_audio_endpoint"] = str(service.get("id") or service_id).strip()
        return settings
