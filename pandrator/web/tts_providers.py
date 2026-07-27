"""Typed TTS provider boundary and catalogue use cases."""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from pandrator.logic import tts_handler
from pandrator.logic.tts_provider_profiles import list_tts_provider_profiles
from pandrator.runtime import DataPaths
from sqlalchemy import select

from .credentials import (
    TTS_SERVICE_ENVS,
    credential_backend,
    credential_reference_input,
    database_reference,
    provider_credential_status,
    redact_inline_secrets,
    resolve_provider_credential,
    resolve_secret_reference,
    tts_credential_key,
    tts_service_credential_key,
)
from .database import Database
from .models import AppSetting, Artifact
from .workspace import BUILTIN_DEFAULTS


def normalize_service_id(value: object) -> str:
    normalized = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    return {
        "qwen3_tts": "kobold_qwen",
        "qwen3": "kobold_qwen",
        "qwen": "kobold_qwen",
        "kobold_qwen3": "kobold_qwen",
        "openai_compatible": "openai_compatible",
    }.get(normalized, normalized)


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
    voice_upload: bool = False


@dataclass(frozen=True, slots=True)
class TtsRetryPolicy:
    max_attempts: int = 5
    maximum_delay_seconds: float = 90.0

    @classmethod
    def from_settings(
        cls,
        settings: dict[str, Any],
        *,
        max_attempts: object | None = None,
    ) -> TtsRetryPolicy:
        try:
            attempts = int(
                max_attempts
                if max_attempts is not None
                else settings.get("max_attempts")
                or 5
            )
        except (TypeError, ValueError):
            attempts = 5
        try:
            maximum_delay = float(
                settings.get("retry_max_delay_seconds")
                or 90.0
            )
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
    ) -> TtsCapabilities:
        ...

    def health(self, service: dict[str, Any]) -> TtsHealth:
        ...

    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        ...

    def synthesize(
        self,
        text: str,
        settings: dict[str, Any],
        **options: Any,
    ):
        ...

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
        ...


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
    ):
        return tts_handler.text_to_audio(text, settings, **options)

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


class KoboldQwenAdapter(LegacyTtsAdapter):
    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        base_url = str(
            service.get("api_base")
            or tts_handler.KOBOLD_QWEN_API_BASE_URL
        )
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
            service.get("default_model")
            or tts_handler.KOBOLD_QWEN_DEFAULT_MODEL
        )
        return {
            "voice_catalogues": catalogues,
            "default_voices": {
                tts_handler.KOBOLD_QWEN_DEFAULT_MODEL:
                    tts_handler.KOBOLD_QWEN_DEFAULT_VOICE,
                "Voice Cloning": tts_handler.KOBOLD_QWEN_SAMPLE_VOICE,
            },
            "voice_metadata": {
                (
                    f"{str(item.get('model') or ('Prebuilt Voices' if item.get('type') == 'preset' else 'Voice Cloning'))}:"
                    f"{item['id']}"
                ): item
                for item in entries
            },
            "voices": list(catalogues.get(active_model, [])),
        }


class SileroAdapter(LegacyTtsAdapter):
    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        del api_key
        base_url = str(
            service.get("api_base")
            or tts_handler.SILERO_API_BASE_URL
        )
        model_catalog = tts_handler.get_silero_model_catalog(base_url)
        installed_models = [
            str(item["id"])
            for item in model_catalog
            if isinstance(item.get("status"), dict)
            and item["status"].get("installed")
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
            voice_catalogues[model_id] = [
                str(item["id"])
                for item in entries
            ]
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


class TtsProviderRegistry:
    """Resolve all provider operations through one typed adapter contract."""

    BUILTIN_SERVICE_IDS = (
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
        "openai_compatible",
    )

    def __init__(self) -> None:
        self._adapters: dict[str, TtsProviderAdapter] = {}
        for service_id in self.BUILTIN_SERVICE_IDS:
            self.register(LegacyTtsAdapter(service_id))
        self.replace(SileroAdapter("silero"))
        self.replace(KoboldQwenAdapter("kobold_qwen"))

    def register(self, adapter: TtsProviderAdapter) -> None:
        service_id = normalize_service_id(adapter.service_id)
        if not service_id:
            raise ValueError("TTS adapter service ID must not be empty.")
        if service_id in self._adapters:
            raise ValueError(f"TTS adapter '{service_id}' is already registered.")
        self._adapters[service_id] = adapter

    def replace(self, adapter: TtsProviderAdapter) -> None:
        service_id = normalize_service_id(adapter.service_id)
        if not service_id:
            raise ValueError("TTS adapter service ID must not be empty.")
        self._adapters[service_id] = adapter

    def get(self, service_id: str) -> TtsProviderAdapter:
        normalized = normalize_service_id(service_id)
        adapter = self._adapters.get(normalized)
        if adapter is not None:
            return adapter
        # Custom OpenAI-compatible profiles share the stable legacy adapter.
        return self._adapters["openai_compatible"]

    def service_ids(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def service_id_for_settings(self, settings: dict[str, Any]) -> str:
        explicit = normalize_service_id(
            settings.get("preview_service_id")
            or settings.get("service_id")
        )
        if explicit:
            return explicit
        service_name = normalize_service_id(
            settings.get("service")
            or settings.get("tts_service")
        )
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
    ):
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

    def capabilities(
        self,
        service: dict[str, Any],
    ) -> TtsCapabilities:
        service_id = normalize_service_id(
            service.get("id")
            or service.get("name")
        )
        return self.get(service_id).capabilities(service)

    def health(self, service: dict[str, Any]) -> TtsHealth:
        service_id = normalize_service_id(
            service.get("id")
            or service.get("name")
        )
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
        service_id = normalize_service_id(
            service.get("id")
            or service.get("name")
        )
        try:
            return self.get(service_id).enrich_catalog(
                service,
                api_key=api_key,
            )
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


class TtsCatalogueService:
    """Read and enrich the UI-facing TTS service catalogue."""

    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        providers: TtsProviderRegistry,
    ):
        self.database = database
        self.paths = paths
        self.providers = providers

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
            if (
                not connection_value
                and isinstance(default_value.get("provider_configs"), list)
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
        service_id = normalize_service_id(
            service.get("id")
            or service.get("name")
        )
        key_env = str(
            service.get("api_key_env")
            or TTS_SERVICE_ENVS.get(service_id, "")
        ).strip()
        secret_reference = str(
            service.get("secret_ref")
            or database_reference(tts_service_credential_key(service_id))
        )
        return service_id, key_env, secret_reference

    def _decorate_credentials(
        self,
        service: dict[str, Any],
    ) -> None:
        service_id, key_env, secret_reference = self._credential_details(service)
        service.update(
            provider_credential_status(
                self.database,
                self.paths,
                service_id,
                secret_reference,
                fallback_environment_variable=key_env,
            )
        )
        service["credential_backend"] = credential_backend(secret_reference)
        service["credential_reference"] = credential_reference_input(
            secret_reference
        )

    def _resolved_api_key(self, service: dict[str, Any]) -> str:
        service_id, key_env, secret_reference = self._credential_details(service)
        credential = resolve_secret_reference(
            self.database,
            self.paths,
            secret_reference
            or database_reference(tts_credential_key(service_id)),
            fallback_environment_variable=key_env,
        )
        return credential.resolved_value()

    def _refresh(self, services: list[dict[str, Any]]) -> None:
        def probe(service: dict[str, Any]) -> TtsHealth:
            try:
                return self.providers.health(service)
            except TtsProviderError as error:
                return TtsHealth(False, False, str(error))

        with ThreadPoolExecutor(
            max_workers=min(12, max(1, len(services)))
        ) as executor:
            states = list(executor.map(probe, services))
        for service, state in zip(services, states):
            service.update(
                {
                    "online": state.online,
                    "available": state.available,
                    "availability_reason": state.reason,
                }
            )
            if not state.online:
                continue
            try:
                service.update(
                    self.providers.enrich_catalog(
                        service,
                        api_key=self._resolved_api_key(service),
                    )
                )
            except TtsProviderError as error:
                service.update(
                    {
                        "available": False,
                        "availability_reason": str(error),
                    }
                )

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
                    if not self.paths.managed_path(
                        artifact.relative_path
                    ).is_file():
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
                        "preview_text": str(
                            metadata.get("preview_text")
                            or ""
                        ),
                        "updated_at": artifact.updated_at.isoformat(),
                    }
                )
        return previews

    def snapshot(self, *, refresh: bool = False) -> tuple[dict[str, Any], int]:
        connection_value, revision, default_value, default_revision = (
            self._settings()
        )
        services = [
            dict(item)
            for item in tts_handler.get_service_configs(
                {**default_value, **connection_value}
            )
        ]
        for service in services:
            self._decorate_credentials(service)
        if refresh:
            self._refresh(services)
        payload = {
            "value": redact_inline_secrets(connection_value),
            "revision": revision,
            "default_value": redact_inline_secrets(default_value),
            "default_service": str(
                default_value.get("service")
                or BUILTIN_DEFAULTS["tts"]["service"]
            ),
            "default_revision": default_revision,
            "builtin_defaults": redact_inline_secrets(
                BUILTIN_DEFAULTS["tts"]
            ),
            "services": redact_inline_secrets(services),
            "profiles": list_tts_provider_profiles(),
            "previews": self._previews(),
        }
        return payload, revision

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
        normalized = normalize_service_id(
            service.get("id")
            or service_id
        )
        resolved = resolve_provider_credential(
            self.database,
            self.paths,
            normalized,
            service.get("secret_ref")
            or database_reference(tts_service_credential_key(normalized)),
            fallback_environment_variable=str(
                service.get("api_key_env")
                or TTS_SERVICE_ENVS.get(normalized, "")
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
    ) -> dict[str, Any] | None:
        connection_value, _, default_value, _ = self._settings()
        service = tts_handler.get_service_config(
            {**default_value, **connection_value},
            service_id,
        )
        if service is None:
            return None
        resolved_id = normalize_service_id(
            service.get("id")
            or service_id
        )
        resolved_model = model or str(service.get("default_model") or "")
        default_voices = (
            service.get("default_voices")
            if isinstance(service.get("default_voices"), dict)
            else {}
        )
        resolved_voice = (
            voice
            or str(default_voices.get(resolved_model) or "")
            or str(service.get("default_voice") or "")
        )
        service_name = (
            "OpenAI Compatible"
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
            "language": language
            or str(default_value.get("language") or "en"),
            "preview_service_id": resolved_id,
            "preview_api_base": str(service.get("api_base") or ""),
        }
        if service.get("is_custom"):
            settings["openai_audio_endpoint"] = resolved_id
        return settings
