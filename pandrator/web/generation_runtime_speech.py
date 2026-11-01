"""Run-local speech connections without changing frozen generation preferences."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from copy import deepcopy
from threading import RLock
from typing import Any

from sqlalchemy.orm import Session

from pandrator.logic import tts_handler
from pandrator.logic.tts_service_identity import normalize_service_id
from pandrator.runtime import DataPaths

from .access.generation_execution_policy import GenerationExecutionProof
from .accounts.execution_actor import UserActor, current_execution_actor
from .database import Database
from .generation_speech_connections import (
    GenerationSpeechConnection,
    capture_generation_speech_connection,
    require_generation_speech_connection,
)
from .manager_proxy import LocalManagerProxy
from .tts_catalogue_service import TtsCatalogueService
from .tts_providers import TtsProviderRegistry
from .workflow_generation_protocols import HydrateTtsSettingsProtocol

_URL_KEYS = {
    "audio_cpp": "audio_cpp_base_url",
    "xtts": "xtts_base_url",
    "voxcpm": "voxcpm_base_url",
    "fishs2": "fishs2_base_url",
    "voxtral": "voxtral_base_url",
    "kokoro": "kokoro_base_url",
    "silero": "silero_base_url",
    "chatterbox": "chatterbox_base_url",
    "kobold_qwen": "kobold_qwen_base_url",
    "magpie": "magpie_base_url",
}
_CHOOSE = "Choose a speech connection configured for this workspace."
_CHANGED = (
    "The speech connection or credential changed. "
    "Review the workspace connection and submit a new run."
)


class GenerationSpeechRuntime:
    """Bind only used profiles/models; refresh their authority at actual IO."""

    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        providers: TtsProviderRegistry,
        manager_bridge: LocalManagerProxy | None,
        captured: GenerationExecutionProof,
        prove: Callable[[Session | None], GenerationExecutionProof],
        hydrate: HydrateTtsSettingsProtocol,
        remember_secret: Callable[[str], None],
    ):
        self.database = database
        self.paths = paths
        self.manager_bridge = manager_bridge
        self.captured = captured
        self._prove = prove
        self._hydrate = hydrate
        self._remember_secret = remember_secret
        self._scoped = (
            not captured.legacy_host_access and type(current_execution_actor.get()) is UserActor
        )
        self._connection_config_cache = {} if self._scoped else None
        self._catalogue = None
        if self._scoped:
            self._catalogue = TtsCatalogueService(
                database,
                paths,
                providers,
                manager_bridge=manager_bridge,
                _connection_config_cache=self._connection_config_cache,
            )
        self._bindings: dict[tuple[str, str], GenerationSpeechConnection] = {}
        self._frozen_profiles: dict[str, dict[str, Any]] = {}
        self._runtime_profiles: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def close(self) -> None:
        try:
            if self._catalogue is not None:
                self._catalogue.close()
        finally:
            if self._connection_config_cache is not None:
                self._connection_config_cache.clear()

    @staticmethod
    def _selected_profile(settings: dict[str, Any]) -> dict[str, Any]:
        selected = str(settings.get("service") or settings.get("tts_service") or "XTTS")
        if normalize_service_id(selected) in {"custom", "openai_compatible"}:
            selected = settings.get("openai_audio_endpoint")
            if type(selected) is not str:
                raise ValueError(_CHOOSE)
        profile = tts_handler.get_service_config(settings, selected)
        if profile is None:
            raise ValueError(_CHOOSE)
        return profile

    @staticmethod
    def _canonical(
        settings: dict[str, Any],
        profile: dict[str, Any],
        *,
        selected_model: str | None = None,
    ) -> dict[str, Any]:
        canonical_profile = profile
        if selected_model is not None:
            selected_id = str(selected_model).strip().casefold()
            selected_ids = {selected_id} if selected_id else set()
            adapter = str(profile.get("adapter") or "").strip().casefold().replace("-", "_")
            if adapter == "azure_speech" and selected_id:
                from pandrator.logic.azure_speech_emotions import canonical_azure_speech_model

                canonical_model = canonical_azure_speech_model(str(selected_model).strip())
                if canonical_model:
                    selected_ids.add(str(canonical_model).strip().casefold())

            memo: dict[int, Any] = {}
            canonical_profile = deepcopy(
                {key: value for key, value in profile.items() if key != "model_catalog"},
                memo,
            )
            if "model_catalog" in profile:
                model_catalog = profile["model_catalog"]
                if isinstance(model_catalog, list):
                    selected_rows = [
                        record
                        for record in model_catalog
                        if isinstance(record, dict)
                        and str(record.get("id") or "").strip().casefold() in selected_ids
                    ]
                    canonical_profile["model_catalog"] = deepcopy(selected_rows, memo)
                else:
                    canonical_profile["model_catalog"] = deepcopy(model_catalog, memo)

        result = {
            key: value
            for key, value in settings.items()
            if key
            not in {
                "provider_configs",
                "service_configs",
                "openai_audio_endpoints_json",
                "external_server_url",
                "api_key",
                "api_key_env",
                "secret_ref",
                "preview_service_id",
                "service_id",
                "preview_adapter",
                "preview_api_base",
            }
            and not key.endswith("_base_url")
        }
        result["provider_configs"] = [canonical_profile]
        if canonical_profile.get("is_custom"):
            result["openai_audio_endpoint"] = str(canonical_profile["id"])
        else:
            result.pop("openai_audio_endpoint", None)
        # First-class transports dispatch by service id even when their catalogue
        # profile advertises an OpenAI-compatible adapter. Custom profiles use
        # their configured adapter instead.
        route = normalize_service_id(
            canonical_profile.get("adapter")
            if canonical_profile.get("is_custom")
            else canonical_profile.get("id")
        )
        if route == "audio_cpp_experimental":
            route = "audio_cpp"
        url_key = _URL_KEYS.get(route)
        if url_key:
            base = str(canonical_profile.get("api_base") or "").strip()
            if not base:
                raise ValueError(_CHOOSE)
            result[url_key] = base
        return result

    def check(self, session: Session) -> None:
        """The caller proves current actor/root/lease in this same unit first."""
        if self._catalogue is None:
            return
        with self._lock:
            bindings = tuple(self._bindings.values())
        for binding in bindings:
            require_generation_speech_connection(session, self._catalogue, binding)

    def _capture(self, settings: dict[str, Any]) -> GenerationSpeechConnection:
        assert self._catalogue is not None
        with self.database.snapshot_session() as session:
            self._prove(session)
            self.check(session)
            binding = capture_generation_speech_connection(
                session, self._catalogue, settings, workspace_id=self.captured.workspace_id
            )
        with self._lock:
            for previous in self._bindings.values():
                if previous.service_id == binding.service_id and (
                    previous.connection_fingerprint != binding.connection_fingerprint
                    or previous.credential_fingerprint != binding.credential_fingerprint
                ):
                    raise ValueError(_CHANGED)
            self._bindings[(binding.service_id, binding.model)] = binding
        return binding

    def hydrate(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Validate frozen transport before resolving its workspace credential."""
        self._prove(None)
        if self._catalogue is not None:
            binding = self._capture(settings)
            profile = deepcopy(self._selected_profile(settings))
            settings = self._canonical(settings, profile)
            settings.update(model=binding.model, xtts_model=binding.model)
        else:
            binding = None
            profile = None
        current = self._hydrate(
            self.database,
            self.paths,
            settings,
            manager_bridge=self.manager_bridge,
            session_id=self.captured.session_id,
            workspace_id=self.captured.workspace_id,
        )
        self._prove(None)
        try:
            hydrated_profile = self._selected_profile(current)
        except ValueError:
            if binding is not None:
                raise
            hydrated_profile = {}
        credential = str(hydrated_profile.get("api_key") or "")
        self._remember_secret(credential)
        if binding is not None and profile is not None:
            if hashlib.sha256(credential.encode()).hexdigest() != binding.credential_fingerprint:
                raise ValueError(_CHANGED)
            with self._lock:
                self._frozen_profiles[binding.service_id] = profile
                self._runtime_profiles[binding.service_id] = deepcopy(hydrated_profile)
            current = self._canonical(
                current, hydrated_profile, selected_model=binding.model
            )
        return current

    def validate(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Approve a cast/segment model without rehydrating per-segment secrets."""
        if self._catalogue is None:
            return settings
        profile = self._selected_profile(settings)
        service_id = normalize_service_id(profile.get("id"))
        raw_model = (
            settings.get("xtts_model") or settings.get("model") or profile.get("default_model")
        )
        if type(raw_model) is not str:
            raise ValueError(_CHOOSE)
        with self._lock:
            frozen = self._frozen_profiles.get(service_id)
            runtime = self._runtime_profiles.get(service_id)
            known = self._bindings.get((service_id, raw_model))
        if frozen is None or runtime is None:
            raise ValueError(_CHOOSE)
        if known is None:
            known = self._capture(self._canonical(settings, frozen))
        result = self._canonical(settings, runtime, selected_model=known.model)
        result.update(model=known.model, xtts_model=known.model)
        return result
