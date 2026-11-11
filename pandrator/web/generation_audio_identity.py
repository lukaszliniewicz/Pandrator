"""Audio compatibility, independent of speech-plan topology and output settings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .access.contracts import AccessDenied, WorkspaceNotFound
from .access.voice_policy import require_current_voice_library_read
from .accounts.models import Workspace
from .generation_voice_references import voice_reference_index
from .models import Artifact, AudioTake, GenerationPlan, GenerationPlanRevision, GenerationSegment
from .projects.identity import session_project
from .speech_input_contracts import (
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
    SHA256,
    PendingSpeechProof,
    validate_speech_input_pin,
    validate_speech_source_reference,
)
from .voice_identity import voice_creation_workspace

IDENTITY_KEY = "generation_audio_identity"
IDENTITY_VERSION = 1
_MATERIAL_PROVIDER_IDENTITY_FIELDS = frozenset({
    "id",
    "adapter",
    "api_base",
    "settings",
    "request_defaults",
    "extra_body",
    "speech_path",
})
_MATERIAL_PROVIDER_LOOKUP_FIELDS = _MATERIAL_PROVIDER_IDENTITY_FIELDS | {"default_model"}

if TYPE_CHECKING:
    from .project_segment_speech import SegmentSpeechReferenceIndex


def _voice_key(value: str) -> str:
    return value.strip().casefold().removesuffix(".wav")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def audio_cpp_reference_identity(
    sample_id: str, artifact_id: str, audio_hash: str, transcript: object,
    transcript_reviewed: bool,
) -> dict[str, str]:
    """The legacy live-reference vocabulary, shared with captured source facts."""
    return {
        "sample_id": sample_id,
        "artifact_id": artifact_id,
        "content_hash": audio_hash,
        "transcript_hash": _hash(str(transcript or "").strip() if transcript_reviewed else ""),
    }


def voice_reference_hash(references: list[dict[str, Any]]) -> str:
    """Preserve the version1 sorted/deduplicated reference hash exactly."""
    return _hash(sorted({_hash(item) for item in references}))


def audio_cpp_source_reference_hash(
    sample_id: str, artifact_id: str, audio_hash: str, reviewed_reference_text: str,
) -> str:
    return voice_reference_hash([audio_cpp_reference_identity(
        sample_id, artifact_id, audio_hash, reviewed_reference_text, True,
    )])


def source_reference_identity(settings: dict[str, Any]) -> dict[str, str] | None:
    """Validate v2 compatibility evidence against this exact retained choice."""
    from .generation_speech_inputs import retained_generation_input
    try:
        retained = retained_generation_input(settings)
    except (ValueError, TypeError):
        return None
    if retained is None:
        return None
    return validate_speech_source_reference(
        settings.get(PROJECT_SPEECH_SOURCE_KEY), retained.pin, retained.binding,
    )


def source_cast_reference_hashes(settings: dict[str, Any]) -> list[str] | None:
    """Copy optional legacy item hashes only from exact bound v2 evidence."""
    if source_reference_identity(settings) is None:
        return None
    value = settings[PROJECT_SPEECH_SOURCE_KEY]
    return list(value["cast_reference_hashes"]) if "cast_reference_hashes" in value else None


def source_voice_reference_hash(settings: dict[str, Any]) -> str | None:
    """Read optional compatibility evidence only for its exact retained choice."""
    reference = source_reference_identity(settings)
    if reference is not None:
        return voice_reference_hash([reference])
    from .generation_speech_inputs import retained_generation_input
    from .project_speech_references import normalize_speech_reference_binding

    value = settings.get(PROJECT_SPEECH_SOURCE_KEY)
    if type(value) is not dict or set(value) != {
        "version", "pin", "binding", "source_voice_reference_hash",
    } or type(value["version"]) is not int or value["version"] != 1:
        return None
    digest = value["source_voice_reference_hash"]
    if type(digest) is not str or SHA256.fullmatch(digest) is None:
        return None
    try:
        pin = validate_speech_input_pin(value["pin"])
        binding = normalize_speech_reference_binding(value["binding"])
        retained = retained_generation_input(settings)
    except (ValueError, TypeError):
        return None
    if (pin is None or retained is None or pin != retained.pin
            or binding != value["binding"] or binding != retained.binding):
        return None
    return digest


def _material_settings(snapshot: dict[str, Any], _service_config_cache=None) -> dict[str, Any]:
    # Imported lazily: workspace owns the persisted settings vocabulary and
    # consumes this module at its generation boundary.
    from pandrator.logic import tts_handler
    from pandrator.logic.audio_cpp_parameters import (
        request_parameters_for_family,
        validate_audio_cpp_model_options,
    )

    from .settings_policy import (
        BUILTIN_DEFAULTS,
        RUNTIME_SETTING_ALIASES,
        _secret_free,
        adapt_runtime_settings,
    )

    values = {
        **adapt_runtime_settings("tts", snapshot.get("tts") or {}, _service_config_cache),
        **adapt_runtime_settings("audio", snapshot.get("audio") or {}),
    }
    inline_reference = values.get("audio_cpp_voice_ref")
    if inline_reference is not None:
        values["inline_voice_reference_hash"] = _hash(
            {
                "reference": inline_reference,
                "transcript": values.get("audio_cpp_reference_text"),
            }
        )
    unrelated = {
        key
        for section, defaults in BUILTIN_DEFAULTS.items()
        if section != "tts"
        for key in (*defaults, *RUNTIME_SETTING_ALIASES.get(section, {}).values())
    } - set(BUILTIN_DEFAULTS["tts"])
    unrelated.update(
        {
            "max_attempts",
            "retry_timeout_seconds",
            "tts_batch_size",
            "tts_concurrent_requests",
            "provider_configs",
            "service_configs",
            "pricing",
            "timeout",
            "request_timeout_seconds",
            "audio_cpp_voice_ref",
            "audio_cpp_voice_ref_hash",
            "audio_cpp_reference_text",
            PROJECT_SPEECH_INPUT_KEY,
            PROJECT_SPEECH_BINDING_KEY,
            PROJECT_SPEECH_SELECTOR_KEY,
            PROJECT_SPEECH_SOURCE_KEY,
            "selected_segment_override",
            "use_existing_speech_plans",
            "performance_enabled",
            "casting_enabled",
            "voice_mode_version",
            "speech_analysis_preferences",
            "performance_allow_vocalizations",
            "performance_context_before",
            "performance_context_after",
            "performance_context_max_chars",
            "tts_context_mode",
            "secret_ref",
            "api_key_env",
        }
    )
    selected = str(values.get("service") or values.get("tts_service") or "")
    if selected == tts_handler.OPENAI_COMPAT_SERVICE:
        selected = str(values.get("openai_audio_endpoint") or selected)
    raw_model_settings = values.pop("audio_cpp_model_settings", None)
    selected_model_options: tuple[str, dict[str, Any]] | None = None
    selected_model = str(
        values.get("xtts_model") or values.get("model") or ""
    ).strip()
    provider = tts_handler.get_service_config(
        values,
        selected,
        _cache=_service_config_cache,
        _fields=_MATERIAL_PROVIDER_LOOKUP_FIELDS,
    )
    if not selected_model and isinstance(provider, dict):
        selected_model = str(provider.get("default_model") or "").strip()
    provider_adapter = str((provider or {}).get("adapter") or "").strip().lower()
    audio_cpp_selected = provider_adapter == "audio_cpp" or selected in {
        "audio_cpp",
        "audio.cpp",
        "audio-cpp",
        "audiocpp",
    }
    if audio_cpp_selected and isinstance(raw_model_settings, dict):
        raw_selected_options = raw_model_settings.get(selected_model)
        if selected_model in raw_model_settings and (
            raw_selected_options is None or isinstance(raw_selected_options, dict)
        ):
            model_metadata = tts_handler.get_service_config(
                values,
                selected,
                _cache=_service_config_cache,
                _fields=frozenset({"model_catalog", "model_voice_modes"}),
                _model_catalog_id=selected_model,
            )
            metadata = tts_handler._audio_cpp_model_metadata(
                selected_model,
                model_metadata if isinstance(model_metadata, dict) else {},
            )
            family = str(metadata.get("family") or "").strip()
            normalized_options = validate_audio_cpp_model_options(
                family,
                raw_selected_options or {},
            )
            if normalized_options is not None:
                selected_model_options = (family, normalized_options)
    values = {
        key: value
        for key, value in _secret_free(values).items()
        if key not in unrelated
        and not key.startswith(
            ("_", "speech_block_", "speech_plan_", "llm_", "source_")
        )
    }
    if selected_model_options is not None:
        family, model_options = selected_model_options
        # A selected model map suppresses legacy tuning sources at request
        # time, so ignored values must not invalidate reusable audio either.
        ignored_tuning_keys = {
            "audio_cpp_options",
            "options",
            "seed",
            "speed",
            "audio_cpp_speed",
        }
        ignored_tuning_keys.update(
            f"audio_cpp_{key}"
            for key in request_parameters_for_family(family)
        )
        values = {
            key: value
            for key, value in values.items()
            if key not in ignored_tuning_keys
        }
        values["audio_cpp_model_settings"] = {
            selected_model: model_options,
        }
    # Other installed providers, credentials, labels and catalog ordering do
    # not change the selected service's audio.
    if provider:
        values["provider"] = _secret_free(
            {
                key: value
                for key, value in provider.items()
                if key in _MATERIAL_PROVIDER_IDENTITY_FIELDS
            }
        )
    return values


class AudioIdentityContext:
    """Resolve one settings snapshot and managed voice inventory per inspection."""

    def __init__(
        self, session: Session, snapshot: dict[str, Any], *, _service_config_cache=None,
        _segment_references: SegmentSpeechReferenceIndex | None = None,
        _pending_proof: PendingSpeechProof | None = None,
        _live_cast_material: bool = True,
    ):
        from .generation_cast_runtime import filter_segment_tts_override
        from .settings_policy import adapt_runtime_settings
        from .tts_providers import TtsProviderRegistry

        self._pending_proof = _pending_proof
        self._live_cast_material = _live_cast_material and "generation_audio_identities" not in snapshot
        self._live_cast_references: dict[tuple[str, str, str, str], list[dict[str, Any]] | None] = {}
        self.session = session
        self.snapshot = snapshot
        # Request-scoped memoization only: this context is built fresh for one
        # inspection call, so entries can never leak across requests. Every key
        # hashes the complete input settings, never a subset, so a settings,
        # voice, or casting change always misses and recomputes.
        self._service_config_cache: dict = _service_config_cache if _service_config_cache is not None else {}
        self._material_settings_cache: dict[str, dict[str, Any]] = {}
        self._voice_reference_cache: dict[str, Any] = {}
        self._service_id_cache: dict[str, str] = {}
        self._inspection_catalogue_copies: dict[int, Any] = {}
        self._inspection_catalogue_sources: dict[int, Any] = {}
        self._registry = TtsProviderRegistry()
        self.performance_states: dict[str, tuple[dict[str, Any], dict[str, dict[str, str]]]] = {}
        self.effective_settings_cache: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._root_narrators: dict[str, tuple[dict[str, Any], str | None]] = {}
        # The SQLite database and managed artifacts share the DataPaths root.
        self.paths = DataPaths(Path(str(session.get_bind().engine.url.database)).parent)
        self.settings = {
            **adapt_runtime_settings("tts", snapshot.get("tts") or {}, _service_config_cache=self._service_config_cache),
            **adapt_runtime_settings("audio", snapshot.get("audio") or {}),
        }
        selected = snapshot.get("selected_segment_override") or {}
        self.selected_tts = filter_segment_tts_override(
            snapshot.get("tts") or {}, selected.get("tts") or {}
        )
        self.selected_rvc = dict(selected.get("rvc") or {})
        self.voices: dict[str, list[dict[str, Any]]] = {}
        self._voice_references_loaded = False
        self.workspace_id: str | None = None
        # Ownership facts belong to this request's caller-owned DB unit, just
        # like the settings and voice snapshots above. Keep each owning session
        # separate so a context still rejects segments from another workspace.
        self._segment_workspaces: dict[str, Workspace] = {}
        self._session_ids_by_revision: dict[str, str] = {}
        # SQLAlchemy's identity map weakly retains clean rows. Keep the plan and
        # revision ORM objects alive for this inspection so 5,000 segments do
        # not each fetch the same two rows again. All access/lease checks below
        # still execute for every segment.
        self._revision_plan_rows: dict[str, tuple[GenerationPlanRevision, GenerationPlan]] = {}
        self._segment_references = _segment_references
        self.cache: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    def _ensure_segment_workspace(self, segment: GenerationSegment) -> None:
        revision = self.session.get(GenerationPlanRevision, segment.plan_revision_id)
        plan = self.session.get(GenerationPlan, revision.plan_id) if revision else None
        if revision is None or plan is None:
            raise ValueError("The speech plan is unavailable.")
        self._revision_plan_rows[segment.plan_revision_id] = (revision, plan)
        known_workspace = self._segment_workspaces.get(plan.session_id)
        workspace_id = (
            known_workspace.id if known_workspace is not None
            else session_project(self.session, plan.session_id).workspace_id
        )
        # Retain the row within this unit so the identity map avoids repeated
        # SELECTs, but still check active state before every identity-cache hit.
        workspace = voice_creation_workspace(self.session, workspace_id=workspace_id)
        self._segment_workspaces[plan.session_id] = workspace
        if self.workspace_id is not None and self.workspace_id != workspace_id:
            raise ValueError("Audio identity segments have inconsistent workspace ownership.")
        if self.workspace_id is None:
            self.workspace_id = workspace_id
        self._session_ids_by_revision[segment.plan_revision_id] = plan.session_id

    def _ensure_voice_references(self) -> None:
        if not self._voice_references_loaded:
            try:
                self.voices = voice_reference_index(
                    self.session, self.paths, workspace_id=self.workspace_id
                )
            except (AccessDenied, WorkspaceNotFound):
                # Project readers may inspect history without a library grant.
                # Actual fresh use has its own required source authority.
                self.voices = {}
            self._voice_references_loaded = True

    def _current_narrator_settings(
        self, session_id: str, settings: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        from .project_tts_settings import bind_project_tts_reference
        from .settings_policy import adapt_runtime_settings
        from .speech_inputs import speech_input_adapter

        if (PROJECT_SPEECH_INPUT_KEY in settings or PROJECT_SPEECH_BINDING_KEY in settings
                or "audio_cpp_voice_ref" in settings or speech_input_adapter(settings)[0] != "audio_cpp"):
            return settings, None
        if session_id not in self._root_narrators:
            snapshot = {"tts": {**(self.snapshot.get("tts") or {}), **self.selected_tts}}
            error_message = None
            selected = settings
            try:
                bind_project_tts_reference(
                    self.session, session_id, snapshot, _resolve_missing_managed=True,
                )
                base = {key: value for key, value in settings.items() if key not in {
                    PROJECT_SPEECH_INPUT_KEY, PROJECT_SPEECH_BINDING_KEY,
                    PROJECT_SPEECH_SELECTOR_KEY, PROJECT_SPEECH_SOURCE_KEY,
                }}
                selected = {**base, **adapt_runtime_settings(
                    "tts", snapshot["tts"], self._service_config_cache,
                )}
            except (ValueError, AccessDenied, WorkspaceNotFound) as error:
                error_message = str(error)
            self._root_narrators[session_id] = selected, error_message
        selected, error_message = self._root_narrators[session_id]
        return selected, error_message

    def _references_for_settings(self, settings: dict[str, Any]) -> list[dict[str, Any]]:
        from .generation_speech_inputs import retained_generation_input

        retained = retained_generation_input(settings)
        if retained is not None:
            return [{"retained_content_hash": retained.pin["content_hash"]}] if retained.pin else []
        self._ensure_voice_references()
        voice = str(settings.get("voice") or settings.get("speaker") or "")
        references = list(self.voices.get(_voice_key(voice), []))
        managed_id = settings.get("_cast_voice_id")
        if managed_id:
            references.extend(self.voices.get(_voice_key(managed_id), []))
        return references

    def _live_cast_material_references(self, settings: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Current inspection only: guarded selected metadata, never source bytes."""
        from .speech_inputs import speech_input_adapter
        from .voice_library import resolve_audio_cpp_voice_reference

        if not self._live_cast_material or speech_input_adapter(settings)[0] != "audio_cpp":
            return None
        service = str(settings.get("preview_service_id") or settings.get("service_id")
                      or settings.get("openai_audio_endpoint") or settings.get("service")
                      or settings.get("tts_service") or "")
        model = str(settings.get("xtts_model") or settings.get("model") or "")
        alias = str(settings.get("voice") or settings.get("speaker") or "").strip()
        managed_id = str(settings.get("_cast_voice_id") or settings.get("voice_id") or "").strip()
        key = (service, model, alias, managed_id)
        try:
            assert self.workspace_id is not None
            require_current_voice_library_read(self.session, self.workspace_id)
            if key not in self._live_cast_references:
                reference = resolve_audio_cpp_voice_reference(
                    self.session, self.paths, settings, self._service_config_cache,
                    workspace_id=self.workspace_id, voice_id=managed_id or None,
                    strict_selection=True, _metadata_only=True,
                )
                if reference is None:
                    self._live_cast_references[key] = None
                    return None
                material = []
                if reference is not None:
                    audio_hash, _path, artifact, sample = reference
                    if type(audio_hash) is not str or SHA256.fullmatch(audio_hash) is None:
                        raise ValueError("The audio.cpp reference has an invalid published content hash.")
                    material = [audio_cpp_reference_identity(sample.id, artifact.id, audio_hash,
                        sample.transcript, sample.transcript_reviewed)]
                self._live_cast_references[key] = material
        except (ValueError, AccessDenied, WorkspaceNotFound):
            self._live_cast_references[key] = None
        value = self._live_cast_references[key]
        return deepcopy(value) if value is not None else None

    def _memoized_material_settings(
        self, settings: dict[str, Any]
    ) -> dict[str, Any]:
        key = _hash(settings)
        result = self._material_settings_cache.get(key)
        if result is None:
            result = _material_settings({"tts": settings}, self._service_config_cache)
            self._material_settings_cache[key] = deepcopy(result)
        return deepcopy(result)

    def _copy_inspection_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Own catalogue copies once for transient, read-only identity planning.

        Internal cast callbacks change only top-level settings. Keep source
        objects alive so their IDs cannot be reused within this request; public
        planners and synthesis never use this copy policy.
        """
        for key in ("provider_configs", "service_configs"):
            source = settings.get(key)
            if not isinstance(source, (dict, list)):
                continue
            source_id = id(source)
            if source_id not in self._inspection_catalogue_copies:
                owned = deepcopy(source)
                self._inspection_catalogue_sources[source_id] = source
                self._inspection_catalogue_copies[source_id] = owned
                self._inspection_catalogue_copies[id(owned)] = owned
        return deepcopy(settings, dict(self._inspection_catalogue_copies))

    def _with_performance(
        self, segment: GenerationSegment, identity: dict[str, Any], settings: dict[str, Any]
    ) -> dict[str, Any]:
        """Only effective directions/context invalidate a previously rendered take.

        History inspection must still work when a newly edited plan has no
        compatible performance sidecar. Such audio is non-reusable; the actual
        generation boundary independently rejects the missing/stale snapshot.
        """
        directed = settings.get("performance_enabled") or settings.get("casting_enabled") or str(settings.get("tts_context_mode") or "off") != "off"
        general = str(settings.get("generation_prompt") or settings.get("openai_audio_instructions") or "").strip()
        if not directed and not general:
            return identity
        from pandrator.logic.speech_performance import compile_performance

        from .generation_performance_snapshot import (
            freeze_generation_performance_snapshot,
            frozen_semantic_contexts,
            segment_performance_settings,
        )

        revision_id = segment.plan_revision_id
        try:
            if revision_id not in self.performance_states:
                pinned = self.snapshot
                frozen_revision = (pinned.get("generation_control_snapshot") or pinned.get("performance_snapshot") or pinned.get("semantic_context_snapshot") or {}).get("plan_revision_id")
                if not frozen_revision or frozen_revision != revision_id:
                    pinned = deepcopy(self.snapshot)
                    freeze_generation_performance_snapshot(
                        self.session, revision_id, pinned, self._service_config_cache,
                        _settings_copy=self._copy_inspection_settings,
                        _voice_reference_index=self.voices if self._voice_references_loaded else None,
                    )
                self.performance_states[revision_id] = (pinned, frozen_semantic_contexts(pinned))
            pinned, contexts = self.performance_states[revision_id]
            if pinned.get("_performance_error"):
                raise ValueError(pinned["_performance_error"])
            text = segment.optimized_text or segment.text
            prepared = segment_performance_settings(settings, pinned, segment.id, text, contexts=contexts)
            if settings.get("casting_enabled"):
                from .generation_cast_runtime import (
                    segment_render_parts,
                    validate_retained_cast_resolution,
                )

                def validate_binding(binding, base, resolved):
                    if PROJECT_SPEECH_INPUT_KEY in resolved or PROJECT_SPEECH_BINDING_KEY in resolved:
                        validate_retained_cast_resolution(
                            self.session, self._session_ids_by_revision[segment.plan_revision_id],
                            binding, base, resolved, self._service_config_cache,
                            _pending_proof=self._pending_proof,
                            _pending_revision_id=segment.plan_revision_id,
                        )
                parts = segment_render_parts(
                    prepared, pinned, segment.id, text,
                    _settings_copy=self._copy_inspection_settings,
                    _validate_binding=validate_binding,
                )
                requests = []
                has_source_identity = False
                has_live_material = False
                complete_live_material = True
                has_legacy_source = False
                complete_legacy_source = True
                from .generation_speech_inputs import retained_generation_input
                from .speech_inputs import speech_input_adapter

                for part in parts:
                    part_settings = part["settings"]
                    references = self._references_for_settings(part_settings)
                    source_reference = source_reference_identity(part_settings)
                    has_source_identity = has_source_identity or source_reference is not None
                    alternate_references = [source_reference] if source_reference is not None else references
                    live_references = alternate_references
                    retained = retained_generation_input(part_settings)
                    if retained is None and self._live_cast_material and speech_input_adapter(part_settings)[0] == "audio_cpp":
                        live_material = self._live_cast_material_references(part_settings)
                        if live_material is None:
                            complete_live_material = False
                        else:
                            live_references = live_material
                            has_live_material = has_live_material or bool(live_material)
                    primary_items = sorted({_hash(item) for item in references})
                    legacy_items = primary_items
                    if retained is not None and PROJECT_SPEECH_SOURCE_KEY in part_settings:
                        legacy_source = source_cast_reference_hashes(part_settings)
                        if legacy_source is None:
                            complete_legacy_source = False
                        else:
                            legacy_items = legacy_source
                            has_legacy_source = True
                    requests.append({
                        "range": [part["start"], part["end"]],
                        "settings": self._memoized_material_settings(part_settings),
                        "references": primary_items,
                        "source_references": sorted({_hash(item) for item in alternate_references}),
                        "live_references": sorted({_hash(item) for item in live_references}),
                        "legacy_references": legacy_items,
                        "request": compile_performance(part["text"], part_settings, None, self._service_config_cache).fingerprint,
                    })
                # Base narrator settings/references are not audible when every
                # part has a cast binding. Hash the actual resolved requests.
                return {
                    "schema_version": IDENTITY_VERSION,
                    "settings_hash": _hash({
                        "parts": [part["settings"] for part in requests],
                        "rvc": self.selected_rvc if self.selected_rvc.get("enabled") else {},
                    }),
                    "voice_reference_hash": _hash([part["references"] for part in requests]),
                    **({"source_voice_reference_hash": _hash([
                        part["live_references"] if complete_live_material and has_live_material
                        else part["source_references"] for part in requests
                    ])} if has_source_identity or (complete_live_material and has_live_material) else {}),
                    **({"legacy_voice_reference_hash": _hash([part["legacy_references"] for part in requests])}
                       if complete_legacy_source and has_legacy_source else {}),
                    "performance_request_hash": _hash({
                        "render_silence_policy": "assembly-v1",
                        "parts": [{"range": part["range"], "request": part["request"]} for part in requests],
                    }),
                }
            compiled = compile_performance(text, prepared, None, self._service_config_cache)
            baseline_settings = dict(settings)
            for key in ("_performance", "_semantic_context", "generation_prompt", "openai_audio_instructions"):
                baseline_settings.pop(key, None)
            baseline = compile_performance(text, baseline_settings, None, self._service_config_cache)
        except (ValueError, AccessDenied, WorkspaceNotFound) as error:
            self.performance_states[revision_id] = ({"_performance_error": str(error)}, {})
            return {**identity, "performance_request_hash": "unavailable", "performance_error": str(error)}
        if compiled.fingerprint == baseline.fingerprint:
            return identity
        return {**identity, "performance_request_hash": compiled.fingerprint}

    def for_segment(self, segment: GenerationSegment) -> dict[str, Any]:
        self._ensure_segment_workspace(segment)
        from .generation_cast_runtime import (
            apply_resolved_binding,
            cast_resolution_key,
            cast_snapshot_version,
            lookup_cast_resolution,
            resolve_binding,
            resolve_project_cast_binding,
            segment_cast_snapshot,
            segment_voice_binding,
            validate_retained_cast_resolution,
        )
        from .generation_rendering import _canonical_segment_slot, is_strict_single_voice
        from .project_segment_speech import (
            SegmentSpeechReferenceIndex,
            has_selected_voice_override,
            uses_segment_speech_references,
        )

        strict_single_voice = is_strict_single_voice(self.settings)
        binding = None if strict_single_voice else segment_voice_binding(segment, self.snapshot)
        frozen = (
            segment_cast_snapshot(self.snapshot.get("generation_control_snapshot") or {}, segment.id)
            if not strict_single_voice else {}
        )
        entry = (frozen.get("segments") or {}).get(segment.id) or {}
        slot_key = _canonical_segment_slot(segment.id)
        saved_references = None
        saved_segment = None
        if not strict_single_voice and not has_selected_voice_override(self.snapshot):
            if "voice_binding" in entry:
                # A frozen choice, including inheritance, outranks mutable
                # legacy columns and today's saved selection.
                binding = deepcopy(entry["voice_binding"])
            elif uses_segment_speech_references(self.settings):
                if self._segment_references is None:
                    self._segment_references = SegmentSpeechReferenceIndex(self.session)
                saved_references = self._segment_references.for_revision(
                    self._session_ids_by_revision[segment.plan_revision_id], segment.plan_revision_id,
                )
                saved_segment = saved_references.get(slot_key) if slot_key is not None else None
                if saved_segment is not None:
                    binding = deepcopy(saved_segment.selector)
        recorded_resolution = None
        if binding and entry.get("voice_binding") == binding:
            recorded_resolution = lookup_cast_resolution(frozen, binding, slot_key)
        language = str(segment.language or "").strip()
        if language.casefold() in {"auto", "und", "unknown"}:
            language = ""
        voice = str(segment.voice or "").strip()
        reference_key = recorded_resolution
        if reference_key is None and saved_segment is not None:
            reference_key = {"selector": saved_segment.selector_hash,
                "binding": saved_segment.binding, "pin": saved_segment.pin}
        session_id = self._session_ids_by_revision[segment.plan_revision_id]
        key = (session_id, language, voice, segment.voice_id or "", _hash({
            "reference": reference_key, "base": (entry.get("controls") or {}).get("_base_voice_settings"),
        }))
        if key in self.cache:
            return self._with_performance(segment, self.cache[key], self.effective_settings_cache[key])
        root_settings = {**self.settings, **self.selected_tts}
        controls = entry.get("controls", frozen.get("controls") or {})
        for name, value in (controls.get("_base_voice_settings") or {}).items():
            if value is None and not (name == PROJECT_SPEECH_INPUT_KEY and (controls.get("_base_voice_settings") or {}).get(PROJECT_SPEECH_BINDING_KEY)):
                root_settings.pop(name, None)
            else:
                root_settings[name] = deepcopy(value)
        root_error = None
        selected_voice_override = any(self.selected_tts.get(name) for name in ("voice", "speaker", "voice_id"))
        if strict_single_voice or (
            not binding and not selected_voice_override
            and not root_settings.get("casting_enabled") and not root_settings.get("performance_enabled")
        ):
            root_settings, root_error = self._current_narrator_settings(session_id, root_settings)
        settings = deepcopy(root_settings)
        if language:
            settings.update(language=language, target_language=language)
        if voice and not strict_single_voice and "voice_binding" not in entry:
            settings.update(voice=voice, speaker=voice)
        if self.selected_tts:
            # These overrides have precedence over persistent segment choices.
            from .settings_policy import adapt_runtime_settings

            settings = adapt_runtime_settings("tts", {**settings, **self.selected_tts}, self._service_config_cache)
            language = str(
                self.selected_tts.get("language")
                or self.selected_tts.get("target_language")
                or ""
            ).strip()
            voice = str(
                self.selected_tts.get("voice") or self.selected_tts.get("speaker") or ""
            ).strip()
            if language:
                settings.update(language=language, target_language=language)
            if voice:
                settings.update(voice=voice, speaker=voice)
        binding_error = root_error
        if binding:
            try:
                session_id = self._session_ids_by_revision[segment.plan_revision_id]
                if entry.get("voice_binding") == binding:
                    resolutions = frozen.get("resolved_bindings") or {}
                    version = cast_snapshot_version(frozen)
                    if recorded_resolution is None:
                        raise ValueError("The cast assignment is absent from this run snapshot. Start a new generation run.")
                    if PROJECT_SPEECH_INPUT_KEY in recorded_resolution or PROJECT_SPEECH_BINDING_KEY in recorded_resolution:
                        validate_retained_cast_resolution(
                            self.session, session_id, binding, settings, recorded_resolution,
                            self._service_config_cache, _pending_proof=self._pending_proof,
                            _pending_revision_id=segment.plan_revision_id,
                        )
                    elif binding.get("voice_id"):
                        # Legacy snapshots still depend on the current source;
                        # retained v2 references prove their project-owned graph.
                        from .models import Voice

                        assert self.workspace_id is not None
                        require_current_voice_library_read(self.session, self.workspace_id)
                        if self.session.scalar(select(Voice.id).where(
                            Voice.id == binding["voice_id"], Voice.workspace_id == self.workspace_id,
                        )) is None:
                            raise ValueError("Managed cast voice is unavailable.")
                else:
                    version = 2
                    resolved = resolve_project_cast_binding(
                        self.session, session_id, binding, settings, slot_key,
                        self._service_config_cache,
                        _saved_references=saved_references,
                    )
                    if resolved is None:
                        resolved = resolve_binding(
                            self.session, binding, settings, self._service_config_cache,
                            self._registry, workspace_id=self.workspace_id,
                        )
                    resolutions = {cast_resolution_key(binding, slot_key, schema_version=version): resolved}
                settings = apply_resolved_binding(
                    binding, settings, resolutions, slot_key=slot_key, schema_version=version,
                )
            except (ValueError, AccessDenied, WorkspaceNotFound) as error:
                # Keep history inspectable when a managed voice is unavailable.
                # The generation boundary independently rejects the binding.
                binding_error = str(error)
        selected_voice = str(
            settings.get("voice") or settings.get("speaker") or ""
        ).strip()
        from .voice_library import resolve_audio_cpp_voice_reference

        settings_hash_key = _hash(settings)
        service_id = self._service_id_cache.get(settings_hash_key)
        if service_id is None:
            service_id = self._registry.service_id_for_settings(settings)
            self._service_id_cache[settings_hash_key] = service_id
        from .generation_speech_inputs import retained_generation_input

        retained = retained_generation_input(settings)
        references = []
        if root_error is not None:
            references = [{"unavailable": True}]
        elif retained is not None:
            references = [{"retained_content_hash": retained.pin["content_hash"]}] if retained.pin else []
        elif service_id != "audio_cpp":
            self._ensure_voice_references()
            references = self.voices.get(_voice_key(selected_voice), [])
            if segment.voice_id and not strict_single_voice:
                references = [*references, *self.voices.get(_voice_key(segment.voice_id), [])]
        if service_id == "audio_cpp" and root_error is None:
            if retained is None and "audio_cpp_voice_ref" not in settings and not settings.get("casting_enabled"):
                # Request-scoped: same session/settings snapshot within one
                # inspection, keyed on the complete effective settings.
                cached = self._voice_reference_cache.get(settings_hash_key, ...)
                if cached is ...:
                    try:
                        assert self.workspace_id is not None
                        require_current_voice_library_read(self.session, self.workspace_id)
                        reference = resolve_audio_cpp_voice_reference(
                            self.session, self.paths, settings,
                            self._service_config_cache,
                            workspace_id=self.workspace_id,
                        )
                    except (ValueError, AccessDenied, WorkspaceNotFound):
                        cached = [{"unavailable": True}]
                    else:
                        cached = []
                        if reference is not None:
                            content_hash, _path, artifact, sample = reference
                            cached = [audio_cpp_reference_identity(
                                sample.id, artifact.id, content_hash,
                                sample.transcript, sample.transcript_reviewed,
                            )]
                    self._voice_reference_cache[settings_hash_key] = cached
                references = cached
        identity = {
            "schema_version": IDENTITY_VERSION,
            "settings_hash": _hash(
                {
                    "tts": self._memoized_material_settings(settings),
                    "rvc": self.selected_rvc
                    if self.selected_rvc.get("enabled")
                    else {},
                }
            ),
            "voice_reference_hash": voice_reference_hash(references),
        }
        source_hash = source_voice_reference_hash(settings)
        if source_hash is not None:
            identity["source_voice_reference_hash"] = source_hash
        if binding_error:
            identity.update(performance_request_hash="unavailable", performance_error=binding_error)
        self.cache[key] = identity
        self.effective_settings_cache[key] = settings
        return self._with_performance(segment, identity, settings)


def take_reuse_reason(
    segment: GenerationSegment,
    take: AudioTake | None,
    artifact: Artifact | None,
    expected: dict[str, Any],
) -> str:
    if take is None or artifact is None or artifact.state == "deleted":
        return "audio_unavailable"
    if segment.status != "completed" or take.status != "completed":
        return "not_completed"
    if expected.get("performance_error"):
        return "audio_identity_unknown"
    actual = (artifact.metadata_json or {}).get(IDENTITY_KEY)
    if not isinstance(actual, dict) or actual.get("schema_version") != IDENTITY_VERSION:
        return "audio_identity_unknown"
    if actual.get("settings_hash") != expected["settings_hash"]:
        return "generation_settings_changed"
    candidates = ("voice_reference_hash", "source_voice_reference_hash", "legacy_voice_reference_hash")
    actual_hashes = {value for name in candidates
                     if type(value := actual.get(name)) is str and SHA256.fullmatch(value)}
    expected_hashes = {value for name in candidates
                       if type(value := expected.get(name)) is str and SHA256.fullmatch(value)}
    if not actual_hashes.intersection(expected_hashes):
        return "voice_reference_changed"
    if actual.get("performance_request_hash") != expected.get("performance_request_hash"):
        return "performance_changed"
    return "reusable"


def plan_audio_identities(
    session: Session, revision_id: str, snapshot: dict[str, Any], *, segment_ids: Collection[str] | None = None,
    _pending_proof: PendingSpeechProof | None = None,
) -> dict[str, dict[str, Any]]:
    query = select(GenerationSegment).where(
        GenerationSegment.plan_revision_id == revision_id,
        GenerationSegment.removed.is_(False),
    )
    if segment_ids is not None:
        query = query.where(GenerationSegment.id.in_(segment_ids))
    context = AudioIdentityContext(session, snapshot, _pending_proof=_pending_proof, _live_cast_material=False)
    return {
        segment.id: context.for_segment(segment)
        for segment in session.scalars(query)
    }
