"""TTS catalogue settings, credential, Manager and provider projection use cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
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
    MAX_TTS_DETAIL_MODEL_IDS,
    MAX_TTS_SERVICE_FILTER_IDS,
    TTS_CATALOGUE_VIEWS,
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
