"""TTS catalogue settings, credential, Manager and provider projection use cases."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Any, Protocol

from sqlalchemy import select

from pandrator.logic import tts_handler
from pandrator.logic.tts_provider_policy import DEFAULT_TTS_SERVICE_ID, provider_policy
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
    redact_secret_text,
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
from .models import AppSetting, Artifact, Voice
from .settings_policy import BUILTIN_DEFAULTS
from .tts_catalogue_projection import (
    MAX_TTS_DETAIL_MODEL_IDS,
    MAX_TTS_SERVICE_FILTER_IDS,
    TTS_CATALOGUE_VIEWS,
    TtsCatalogueModelNotFoundError,
    TtsCatalogueServiceNotFoundError,
    _audio_cpp_static_model_catalog,
    _decorate_model_language_support,
    _filter_service_models,
    _project_compact_service,
    _slim_model_catalog,
    _supports_parallel_cloud_synthesis,
    normalize_service_id,
)
from .tts_provider_contracts import TtsCapabilities, TtsHealth, TtsProviderError


class TtsCatalogueProviders(Protocol):
    def capabilities(self, service: dict[str, Any]) -> TtsCapabilities: ...

    def health(self, service: dict[str, Any]) -> TtsHealth: ...

    def enrich_catalog(
        self,
        service: dict[str, Any],
        *,
        api_key: str = "",
    ) -> dict[str, Any]: ...


class TtsCatalogueService:
    """Read and enrich the UI-facing TTS service catalogue."""

    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        providers: TtsCatalogueProviders,
        *,
        manager_bridge: LocalManagerProxy | None = None,
    ):
        self.database = database
        self.paths = paths
        self.providers = providers
        self._owns_manager_bridge = manager_bridge is None
        self.manager_bridge = LocalManagerProxy() if manager_bridge is None else manager_bridge
        self._native_discovery: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._native_discovery_guard = Lock()
        self._health_observations: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._health_observations_guard = Lock()

    _HEALTH_OBSERVATION_TTL_SECONDS = 60.0
    _MAX_HEALTH_OBSERVATIONS = 128

    @staticmethod
    def _native_discovery_key(
        service: dict[str, Any], settings: dict[str, Any], api_key: str = "",
    ) -> str | None:
        service_id = normalize_service_id(service.get("id") or service.get("name"))
        if normalize_service_id(service.get("adapter")) != "audio_cpp" and service_id not in {"gemini", "vertex_ai"}:
            return None
        identity = {
            "service_id": normalize_service_id(service.get("id") or service.get("name")),
            "endpoint": str(service.get("api_base") or "").rstrip("/"),
            "connection_mode": service.get("connection_mode"),
            "settings": settings,
            "credential_hash": hashlib.sha256(api_key.encode()).hexdigest(),
            "adc_identity_hash": service.get("_adc_identity_hash", ""),
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _health_observation_key(
        service: dict[str, Any], settings: dict[str, Any], api_key: str = "",
    ) -> str | None:
        """Fence short-lived health by provider, endpoint, settings, and credential."""
        service_id = normalize_service_id(service.get("id") or service.get("name"))
        if not service_id:
            return None
        identity = {
            "service_id": service_id,
            "endpoint": str(service.get("api_base") or "").rstrip("/"),
            "connection_mode": service.get("connection_mode"),
            "settings": settings,
            "credential_hash": hashlib.sha256(api_key.encode()).hexdigest(),
            "adc_identity_hash": service.get("_adc_identity_hash", ""),
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, default=str).encode()
        ).hexdigest()

    def _remember_health_observations(
        self,
        services: list[dict[str, Any]],
        keys: Sequence[str | None],
        sensitive_values: Sequence[Sequence[str]],
    ) -> None:
        expiry = monotonic() + self._HEALTH_OBSERVATION_TTL_SECONDS
        checked_at = datetime.now(timezone.utc).isoformat()
        with self._health_observations_guard:
            for service, key, values in zip(
                services, keys, sensitive_values, strict=True
            ):
                if key is None:
                    continue
                reason = service.get("availability_reason")
                if isinstance(reason, str):
                    safe_reason = redact_secret_text(reason, tuple(values))
                    for value in values:
                        if value:
                            safe_reason = safe_reason.replace(value, "[REDACTED]")
                else:
                    safe_reason = None
                service["availability_reason"] = safe_reason
                observation = {
                    "online": service.get("online"),
                    "available": service.get("available"),
                    "availability_reason": safe_reason,
                    "health_checked_at": checked_at,
                }
                service["health_checked_at"] = checked_at
                self._health_observations[key] = (expiry, observation)
                self._health_observations.move_to_end(key)
            while len(self._health_observations) > self._MAX_HEALTH_OBSERVATIONS:
                self._health_observations.popitem(last=False)

    def _apply_health_observations(
        self, services: list[dict[str, Any]], keys: Sequence[str | None],
    ) -> None:
        now = monotonic()
        with self._health_observations_guard:
            for service, key in zip(services, keys, strict=True):
                if key is None:
                    service.update(
                        online=None,
                        available=None,
                        availability_reason=None,
                        health_checked_at=None,
                    )
                    continue
                cached = self._health_observations.get(key)
                if cached is None or now >= cached[0]:
                    if cached is not None:
                        del self._health_observations[key]
                    service.update(
                        online=None,
                        available=None,
                        availability_reason=None,
                        health_checked_at=None,
                    )
                    continue
                self._health_observations.move_to_end(key)
                service.update(deepcopy(cached[1]))

    def _cached_native_discovery(self, key: str | None) -> dict[str, Any] | None:
        if key is None:
            return None
        with self._native_discovery_guard:
            cached = self._native_discovery.get(key)
            if cached is None:
                return None
            if monotonic() - cached[0] >= 300:
                del self._native_discovery[key]
                return None
            self._native_discovery.move_to_end(key)
            discovery = deepcopy(cached[1])
            if isinstance(discovery.get("catalogue"), dict):
                discovery["catalogue"]["source"] = "cache"
            return discovery

    @staticmethod
    def _catalogue_metadata(
        service: dict[str, Any], *, source: str = "configured",
        checked_at: str | None = None, complete: bool = False, error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "source": source,
            "scope": "service",
            "service_id": normalize_service_id(service.get("id") or service.get("name")),
            "checked_at": checked_at,
            "complete": complete,
            "error": error,
        }

    def _mark_discovery_failure(
        self, service: dict[str, Any], key: str | None, error: object, api_key: str,
    ) -> None:
        values = (api_key, str(service.get("credential_reference") or ""))
        safe_error = redact_secret_text(error, values)
        for value in values:
            if value:
                safe_error = safe_error.replace(value, "[REDACTED]")
        if service.get("voice_catalogue_warning"):
            service["voice_catalogue_warning"] = safe_error[:500]
        previous = service.get("catalogue") or {}
        service["catalogue"] = self._catalogue_metadata(
            service, source=previous.get("source", "configured"),
            checked_at=previous.get("checked_at"), error=safe_error[:500],
        )
        # Keep prior inventory and its original TTL, but never hide a failed
        # refresh behind the previous complete observation on the next read.
        with self._native_discovery_guard:
            cached = self._native_discovery.get(key) if key else None
            if cached is not None:
                cached[1]["catalogue"] = deepcopy(service["catalogue"])

    def _retain_native_discovery(self, key: str | None, catalogue: dict[str, Any]) -> None:
        if key is None or catalogue.get("voice_catalogue_warning"):
            return
        # Availability belongs to the current manager/health projection, never
        # to retained model discovery. Native loaded flags remain model metadata.
        discovery = {
            name: deepcopy(value) for name, value in catalogue.items()
            if name not in {
                "available", "online", "availability_reason", "health_checked_at",
            }
        }
        with self._native_discovery_guard:
            self._native_discovery[key] = (monotonic(), discovery)
            self._native_discovery.move_to_end(key)
            while len(self._native_discovery) > 64:
                self._native_discovery.popitem(last=False)

    def close(self) -> None:
        """Retire only the manager bridge created by this catalogue."""

        if self._owns_manager_bridge:
            self.manager_bridge.close()

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
        if service_id == "vertex_ai" and not service.get("credential_configured"):
            try:
                import google.auth

                credentials, project = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                if project or service.get("vertex_project"):
                    service["_google_project"] = str(service.get("vertex_project") or project)
                    service.update(
                        credential_configured=True,
                        credential_source="application_default",
                    )
                    identity = [
                        type(credentials).__name__, project,
                        getattr(credentials, "refresh_token", None),
                        getattr(credentials, "service_account_email", None),
                        getattr(credentials, "quota_project_id", None),
                    ]
                    service["_adc_identity_hash"] = hashlib.sha256(
                        json.dumps(identity, default=str).encode()
                    ).hexdigest()
            except Exception:  # noqa: BLE001 - absent ADC is a normal unconfigured state
                pass
        source = str(service.get("credential_source") or "").casefold()
        if source in {"database", "environment", "keyring", "file", "application_default"}:
            service["credential_origin"] = source
        elif source == "none" and service.get("credential_configured") is False:
            service["credential_origin"] = "unconfigured"
        else:
            service["credential_origin"] = "unknown"

    def _resolved_api_key(self, service: dict[str, Any]) -> str:
        service_id, key_env, secret_reference = self._credential_details(service)
        credential = resolve_secret_reference(
            self.database,
            self.paths,
            secret_reference or database_reference(tts_credential_key(service_id)),
            fallback_environment_variable=key_env,
        )
        return credential.resolved_value()

    def _merge_remembered_google_voices(
        self, services: list[dict[str, Any]], credentials: list[ResolvedCredential],
    ) -> None:
        """Keep created voices selectable after restart, within their Google scope."""
        from pandrator.logic.google_tts_audio import STRUCTURED_TTS_MODELS
        from pandrator.logic.google_voice_scope import google_voice_scope

        if not any(service.get("id") in {"gemini", "vertex_ai"} for service in services):
            return
        with self.database.session() as session:
            voices = list(session.scalars(select(Voice)).all())
            saved = [
                (voice.name, voice.language, voice.description, deepcopy(voice.metadata_json or {}))
                for voice in voices
            ]
        for service, credential in zip(services, credentials, strict=True):
            service_id = str(service.get("id") or "")
            if service_id not in {"gemini", "vertex_ai"}:
                continue
            service["managed_google_voice_ids"] = []
            candidates = [
                item for item in saved
                if ((item[3].get("providers") or {}).get(service_id) or {}).get("resource_kind") == "designed_voice"
            ]
            if not candidates:
                continue
            try:
                scope = google_voice_scope(service_id, service, credential.resolved_value())
            except (ValueError, RuntimeError):
                continue
            catalogues = deepcopy(service.get("voice_catalogues") or {})
            metadata = deepcopy(service.get("voice_metadata") or {})
            for name, language, description, voice_metadata in candidates:
                registration = (voice_metadata.get("providers") or {}).get(service_id) or {}
                if (registration.get("resource_kind") != "designed_voice"
                    or registration.get("status") != "ready"
                    or registration.get("scope_fingerprint") != scope["fingerprint"]):
                    continue
                identifier = str(registration.get("voice_id") or "")
                if not identifier.startswith("voice_"):
                    continue
                # Google extends storage after each use. A discovery response
                # may therefore report a later expiry than the creation receipt.
                expiry = next((
                    metadata.get(f"{model}:{identifier}", {}).get("expire_time")
                    for model in STRUCTURED_TTS_MODELS
                    if metadata.get(f"{model}:{identifier}", {}).get("expire_time")
                ), registration.get("expire_time"))
                if expiry:
                    try:
                        expires_at = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                        if expires_at.tzinfo and expires_at <= datetime.now(timezone.utc):
                            continue
                    except ValueError:
                        pass
                service["managed_google_voice_ids"].append(identifier)
                for model in service.get("models", []):
                    if model not in STRUCTURED_TTS_MODELS:
                        continue
                    catalogues[model] = list(dict.fromkeys([
                        *catalogues.get(model, service.get("voices") or []), identifier,
                    ]))
                    metadata[f"{model}:{identifier}"] = {
                        **metadata.get(f"{model}:{identifier}", {}),
                        "id": identifier, "display_name": name,
                        "description": description or "", "language_code": language or "",
                        "type": "prompted", "model": model,
                        **({"expire_time": expiry} if expiry else {}),
                    }
            service["voice_catalogues"] = catalogues
            service["voice_metadata"] = metadata

    def _refresh(
        self,
        services: list[dict[str, Any]],
        *,
        resolved_api_keys: Sequence[str] | Mapping[str, str] | None = None,
        discovery_keys: Sequence[str | None] = (),
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
        for (_index, service, api_key), (catalogue, error) in zip(
            online_services,
            enrichments,
            strict=True,
        ):
            key = discovery_keys[_index] if _index < len(discovery_keys) else None
            if error is not None:
                self._mark_discovery_failure(service, key, error, api_key)
            elif catalogue is not None:
                # Inventory must not replace the independent health observation.
                discovery = {
                    name: value for name, value in catalogue.items()
                    if name not in {
                        "available", "online", "availability_reason", "health_checked_at",
                    }
                }
                service.update(discovery)
                warning = catalogue.get("voice_catalogue_warning")
                if warning:
                    self._mark_discovery_failure(service, key, warning, api_key)
                else:
                    service["catalogue"] = self._catalogue_metadata(
                        service, source="runtime",
                        checked_at=datetime.now(timezone.utc).isoformat(), complete=True,
                    )
                    discovery["catalogue"] = deepcopy(service["catalogue"])
                    self._retain_native_discovery(key, discovery)

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
            preview_artifacts = db_session.execute(
                select(
                    Artifact.id, Artifact.relative_path, Artifact.updated_at,
                    *(Artifact.metadata_json[key].label(key) for key in (
                        "voice", "service_id", "model", "language", "preview_text"
                    )),
                )
                .where(
                    Artifact.role == "tts_voice_preview",
                    Artifact.state == "current",
                    Artifact.metadata_json["voice"].as_string().is_not(None),
                    Artifact.metadata_json["voice"].as_string() != "",
                )
                .order_by(Artifact.updated_at.desc())
            ).all()
            for artifact in preview_artifacts:
                if not artifact.voice:
                    continue
                try:
                    if not self.paths.managed_path(artifact.relative_path).is_file():
                        continue
                except ValueError:
                    continue
                previews.append(
                    {
                        "artifact_id": artifact.id,
                        "service_id": str(artifact.service_id or ""),
                        "model": str(artifact.model or ""),
                        "voice": str(artifact.voice or ""),
                        "language": str(artifact.language or ""),
                        "preview_text": str(artifact.preview_text or ""),
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
        include_profiles: bool = True,
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
        refresh probing happens. ``include_profiles=False`` omits only the
        provider profiles from the full view without building their deepcopy.
        Successful native audio.cpp discoveries remain available to reads for
        five minutes within this process, fenced by settings and endpoint identity.
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
            "previews": self._previews(),
            "manager": manager,
        }
        if include_profiles:
            payload["profiles"] = list_tts_provider_profiles()
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
            try:
                service = _filter_service_models(
                    service, selected_models, service_id=normalized
                )
            except TtsCatalogueModelNotFoundError:
                if normalized != "audio_cpp" or refresh:
                    raise
                # Saved native inventories can predate a model selected by a run.
                # Discover once, retaining the normal strict model validation.
                (
                    services,
                    _manager,
                    _connection_value,
                    revision,
                    default_value,
                    default_revision,
                    _resolved_credentials,
                ) = self._build_services(
                    refresh=True, selected=[normalized], compact=False
                )
                service = _filter_service_models(
                    services[0], selected_models, service_id=normalized
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
        for service, credential in zip(services, resolved_credentials, strict=True):
            self._decorate_credentials(service, resolved_credential=credential)
        discovery_keys = [
            self._native_discovery_key(
                service, {**default_value, **connection_value}, credential.resolved_value(),
            )
            for service, credential in zip(services, resolved_credentials, strict=True)
        ]
        health_keys = [
            self._health_observation_key(
                service, {**default_value, **connection_value}, credential.resolved_value(),
            )
            for service, credential in zip(services, resolved_credentials, strict=True)
        ]
        for service, discovery_key in zip(
            services,
            discovery_keys,
            strict=True,
        ):
            # Configured rows cannot assert a successful runtime discovery.
            service["catalogue"] = self._catalogue_metadata(service)
            if normalize_service_id(service.get("adapter")) == "audio_cpp":
                if not compact:
                    service["model_catalog"] = _audio_cpp_static_model_catalog(service)
            discovery = self._cached_native_discovery(discovery_key)
            if discovery is not None:
                if compact and "model_catalog" in discovery:
                    # Choosers retain the native IDs/modes without importing
                    # full discovery records into their compact model metadata.
                    discovery["model_catalog"] = _slim_model_catalog(discovery)
                service.update(discovery)
            service.pop("_adc_identity_hash", None)
            service_id = normalize_service_id(service.get("id") or service.get("name"))
            service["supports_parallel_synthesis"] = _supports_parallel_cloud_synthesis(
                service
            )
            if service_id in {"xtts", "gemini", "vertex_ai"}:
                capabilities = self.providers.capabilities(service)
                service["supports_dynamic_catalog"] = capabilities.dynamic_catalog
                if service_id == "xtts":
                    service["supports_model_upload"] = capabilities.model_upload
        if refresh:
            self._refresh(
                services,
                resolved_api_keys=[
                    resolved.resolved_value() for resolved in resolved_credentials
                ],
                discovery_keys=discovery_keys,
            )
            self._remember_health_observations(
                services,
                health_keys,
                [
                    tuple(
                        value
                        for value in (
                            credential.resolved_value(),
                            str(service.get("credential_reference") or ""),
                        )
                        if value
                    )
                    for service, credential in zip(
                        services, resolved_credentials, strict=True
                    )
                ],
            )
        else:
            self._apply_health_observations(services, health_keys)
        self._merge_remembered_google_voices(services, resolved_credentials)
        for service in services:
            service.pop("_google_project", None)
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
        raw_default_voices = service.get("default_voices")
        default_voices = raw_default_voices if isinstance(raw_default_voices, dict) else {}
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
        raw_service_settings = service.get("settings")
        service_settings = raw_service_settings if isinstance(raw_service_settings, dict) else {}
        settings = {
            **BUILTIN_DEFAULTS["tts"],
            **default_value,
            **service_settings,
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
        if generation_prompt is not None:
            # A supplied direction is authoritative for this preview. Empty
            # explicitly clears both aliases; omission keeps saved directions.
            settings["generation_prompt"] = generation_prompt.strip()
            settings["openai_audio_instructions"] = ""
        if seed is not None:
            settings["seed"] = int(seed)
            if is_audio_cpp:
                settings["audio_cpp_seed"] = int(seed)
                settings["_preview_seed_explicit"] = True
        if service.get("is_custom"):
            settings["openai_audio_endpoint"] = str(service.get("id") or service_id).strip()
        if is_audio_cpp and model_voice_mode == "design":
            # Validate the effective directions, including inherited settings.
            # This compiles a payload only; no provider request is started.
            tts_handler._build_audio_cpp_audio_payload("", settings, service)
        return settings
