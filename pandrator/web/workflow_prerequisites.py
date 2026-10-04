"""Persisted workflow prerequisite reads with borrowed database dependencies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pandrator.runtime import DataPaths

from .artifact_selection import canonical_stage_key, selected_artifacts
from .database import Database
from .models import (
    Artifact,
    ArtifactEdge,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
)
from .source_resolution import resolve_primary_source
from .workflow_fingerprints import _stage_settings_fingerprint
from .workflow_inputs import workflow_transformations


class WorkflowPrerequisiteService:
    """Read prerequisite state using only a borrowed database and data paths."""

    def __init__(self, database: Database, paths: DataPaths) -> None:
        self.database = database
        self.paths = paths

    def _latest_stage_input(
        self, session_id: str, prerequisite_roles: tuple[str, ...]
    ) -> Artifact | None:
        with self.database.session() as session:
            candidates = list(
                session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.session_id == session_id,
                        Artifact.role.in_(prerequisite_roles),
                    )
                    .order_by(Artifact.created_at.desc())
                ).all()
            )
            selected = selected_artifacts(session, session_id, candidates)
            by_role: dict[str, Artifact] = {}
            for item in selected.values():
                if item.role in prerequisite_roles:
                    by_role.setdefault(item.role, item)
            primary_source = resolve_primary_source(session, session_id).artifact
            if primary_source and primary_source.role in prerequisite_roles:
                by_role.setdefault(primary_source.role, primary_source)
            for item in candidates:
                if item.state == "current" and item.role != "upload":
                    by_role.setdefault(item.role, item)
            result = next((by_role[role] for role in prerequisite_roles if role in by_role), None)
            if result is not None:
                session.expunge(result)
            return result

    def _persisted_translation_input(self, session_id: str, artifact_id: str) -> Artifact | None:
        """Return a safe persisted translation input, never a foreign artifact.

        Forked sessions may use a source attached from the source library, but
        no other cross-session artifact is a valid workflow input.  Invalid
        legacy settings deliberately fall back to the ordinary local
        prerequisite path instead of leaking an artifact across sessions.
        """

        if not artifact_id:
            return None
        with self.database.session() as session:
            candidate = session.get(Artifact, artifact_id)
            attached_ids = set(
                session.scalars(
                    select(Artifact.id)
                    .join(SourceAsset, SourceAsset.artifact_id == Artifact.id)
                    .join(
                        SessionSource,
                        SessionSource.source_asset_id == SourceAsset.id,
                    )
                    .where(SessionSource.session_id == session_id)
                ).all()
            )
            if (
                candidate is None
                or candidate.state == "deleted"
                or candidate.role
                not in {
                    "media_edit_subtitles",
                    "transcription",
                    "correction",
                    "upload",
                }
                or Path(candidate.relative_path).suffix.lower() != ".srt"
                or (candidate.session_id != session_id and candidate.id not in attached_ids)
            ):
                return None
            session.expunge(candidate)
            return candidate

    @staticmethod
    def _continuation_input_roles(
        definition_key: str,
        default_roles: tuple[str, ...],
        workflow_kind: str,
        input_choices: dict[str, Any],
        transformations: dict[str, Any],
    ) -> tuple[str, ...]:
        if definition_key == "translate":
            translation_parent = str(input_choices.get("translation") or "correction")
            return (
                ("correction",)
                if translation_parent == "correction"
                else (
                    ("media_edit_subtitles",)
                    if workflow_kind == "media_edit"
                    else ("transcription", "upload")
                )
            )
        if definition_key not in {"optimize_document", "generate_audio"}:
            return default_roles
        if definition_key == "generate_audio" and bool(
            transformations.get("llm_tts_document_optimization")
        ):
            return ("tts_optimized",)
        if workflow_kind == "audiobook":
            return ("prepared_text",)
        generation_parent = str(input_choices.get("generation") or "translation")
        if workflow_kind == "media_edit" and generation_parent in {
            "source",
            "media_edit",
        }:
            return ("media_edit_subtitles",)
        return {
            "translation": ("translation",),
            "correction": ("correction",),
            "source": ("transcription", "upload"),
        }.get(generation_parent, default_roles)

    @staticmethod
    def _continuation_required_stages(
        workflow_kind: str,
        target_key: str,
        is_srt_source: bool,
        input_choices: dict[str, Any],
        transformations: dict[str, Any],
    ) -> set[str]:
        required = {target_key}
        if workflow_kind == "audiobook" and target_key in {"generate_audio", "export"}:
            required.update({"clean_source", "prepare_text"})
        elif target_key in {"generate_audio", "export"}:
            if workflow_kind != "media_edit" and not is_srt_source:
                required.add("transcribe")
            translation_parent = str(input_choices.get("translation") or "correction")
            generation_parent = str(input_choices.get("generation") or "translation")
            translation_required = (
                bool(transformations.get("translation")) or generation_parent == "translation"
            )
            if (
                bool(transformations.get("correction"))
                or generation_parent == "correction"
                or (translation_required and translation_parent == "correction")
            ):
                required.add("correct")
            if translation_required:
                required.add("translate")
        if bool(transformations.get("llm_tts_document_optimization")) and target_key in {
            "generate_audio",
            "export",
        }:
            required.add("optimize_document")
        return required

    def _current_stage_fingerprint(
        self, stage_key: str, settings: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Fingerprint of the settings a stage would run with right now.

        LLM-backed stages are hydrated first so the effective (default) model
        is compared instead of the raw request shape.  ``None`` means the
        fingerprint cannot be computed (for example the provider is no longer
        configured), in which case the artifact must simply be reused.
        """
        backend = (
            str(settings.get("translation_backend") or settings.get("backend") or "llm")
            .strip()
            .lower()
        )
        if stage_key == "translate" and backend == "deepl":
            return _stage_settings_fingerprint(stage_key, settings)
        stage_alias = "correction" if stage_key == "correct" else "translation"
        try:
            hydrated = self._with_database_llm_settings(dict(settings), stage_alias)
        except ValueError:
            return None
        return _stage_settings_fingerprint(stage_key, hydrated)

    def _continuation_freshness(
        self,
        session_id: str,
        stage_key: str,
        settings: dict[str, Any],
        existing: Artifact | None,
        source: Artifact | None,
    ) -> dict[str, Any]:
        """Return the exact prerequisite-reuse decision and its UI reasons."""

        if existing is None:
            return {
                "settings_match": False,
                "source_match": False,
                "reasons": ["missing_artifact"],
                "changed_fields": [],
                "stored": None,
                "current": None,
            }
        expected_settings_hash = hashlib.sha256(
            json.dumps(
                settings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        metadata = existing.metadata_json if isinstance(existing.metadata_json, dict) else {}
        expected_hashes = {expected_settings_hash}
        raw_settings_match = bool(
            existing.settings_hash == expected_settings_hash
            or str(metadata.get("requested_settings_hash") or "") == expected_settings_hash
        )
        if not raw_settings_match and stage_key in {"correct", "translate"}:
            stage_alias = "correction" if stage_key == "correct" else "translation"
            try:
                hydrated = self._with_database_llm_settings(dict(settings), stage_alias)
            except ValueError:
                hydrated = None
            if hydrated is not None:
                expected_hashes.add(
                    hashlib.sha256(
                        json.dumps(
                            hydrated,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()
                )
        fallback = bool(
            existing.settings_hash in expected_hashes
            or str(metadata.get("requested_settings_hash") or "") == expected_settings_hash
        )
        stored = metadata.get("settings_fingerprint")
        current = None
        changed_fields: list[str] = []
        settings_match = fallback
        settings_reason = None
        if stage_key in {"correct", "translate"} and isinstance(stored, dict) and stored:
            current = self._current_stage_fingerprint(stage_key, settings)
            if current is None:
                # Keep the current artifact if the old provider configuration
                # cannot be reconstructed. This is the continuation behavior
                # that preflight must mirror.
                settings_match = True
            else:
                settings_match = stored == current
                if not settings_match:
                    settings_reason = "settings_changed"
                    changed_fields = sorted(
                        key
                        for key in set(stored) | set(current)
                        if stored.get(key) != current.get(key)
                    )
        elif not settings_match:
            # A legacy artifact can still be safely reused when its exact raw
            # hash matches. Without that evidence, continuation will rerun it.
            settings_reason = "settings_unverifiable"

        source_match = source is None
        if source is not None:
            source_match = str(metadata.get("source_artifact_id") or "") == source.id or (
                stage_key != "translate"
                and bool(source.content_hash)
                and str(metadata.get("source_content_hash") or "") == source.content_hash
            )
            if not source_match:
                with self.database.session() as session:
                    source_match = session.get(ArtifactEdge, (source.id, existing.id)) is not None
        reasons = [reason for reason in (settings_reason,) if reason]
        if not source_match:
            reasons.append("source_lineage_changed")
        return {
            "settings_match": settings_match,
            "source_match": source_match,
            "reasons": reasons,
            "changed_fields": changed_fields,
            "stored": stored if isinstance(stored, dict) else None,
            "current": current,
        }

    def settings_mismatches(
        self, session_id: str, target_stage: str = "generate_audio"
    ) -> list[dict[str, Any]]:
        """Report every prerequisite that continuation would rerun today.

        The response preserves the legacy ``stage`` and ``changed_fields``
        contract, while ``reasons`` distinguishes semantic changes from a
        legacy hash that cannot prove freshness and from broken source lineage.
        """
        from .settings_policy import adapt_runtime_settings
        from .workflows import AUDIOBOOK_STAGES, DUBBING_STAGES, MEDIA_EDIT_STAGES
        from .workspace_settings import WorkspaceSettingsService

        record = self._session_record(session_id)
        upload = self._latest_stage_input(session_id, ("upload",))
        filename = (
            str(
                (upload.metadata_json or {}).get("original_filename") or upload.relative_path
            ).lower()
            if upload
            else ""
        )
        definitions = (
            AUDIOBOOK_STAGES
            if record.workflow_kind == "audiobook"
            else MEDIA_EDIT_STAGES
            if record.workflow_kind == "media_edit"
            else DUBBING_STAGES
        )
        if record.workflow_kind != "audiobook" and filename.endswith(".srt"):
            definitions = tuple(
                definition for definition in definitions if definition.key != "transcribe"
            )
        target_index = next(
            (
                index
                for index, definition in enumerate(definitions)
                if definition.key == target_stage
            ),
            None,
        )
        if target_index is None:
            raise ValueError(f"Unknown continuation stage: {target_stage}")
        with self.database.session() as session:
            outcome = session.scalar(
                select(OutcomePlan).where(OutcomePlan.session_id == session_id)
            )
            outcome_value = dict(outcome.value_json or {}) if outcome else {}
            transformations = workflow_transformations(session, session_id, outcome, self.database)
            selected = selected_artifacts(session, session_id)
            translation_setting = session.get(
                SessionSetting,
                (session_id, "translation"),
            )
            persisted_translation_settings = (
                dict(translation_setting.value_json or {})
                if translation_setting is not None
                and isinstance(translation_setting.value_json, dict)
                else {}
            )
        raw_input_choices = outcome_value.get("inputs")
        input_choices = raw_input_choices if isinstance(raw_input_choices, dict) else {}
        required = self._continuation_required_stages(
            record.workflow_kind,
            target_stage,
            filename.endswith(".srt"),
            input_choices,
            transformations,
        )
        included = set(record.included_stages_json or [])
        runnable = [
            definition
            for index, definition in enumerate(definitions)
            if index < target_index
            and definition.executable
            and definition.job_kind
            and (definition.key in included or definition.key in required)
        ]
        section_map: dict[str, tuple[str, ...]] = {
            "clean_source": ("source_cleaning", "text"),
            "transcribe": ("stt", "subtitles"),
            "correct": ("correction", "subtitles"),
            "translate": ("translation", "subtitles"),
            "optimize_document": ("text",),
            "prepare_text": ("text", "audio"),
            "generate_audio": ("text", "tts", "audio", "rvc", "output"),
            "export": ("output", "audio", "subtitles"),
        }
        settings_service = WorkspaceSettingsService(self.database)
        sections = list(
            dict.fromkeys(
                section
                for definition in runnable
                for section in section_map.get(definition.key, ())
            )
        )
        resolved, _ = settings_service.resolve(session_id, sections)
        mismatches: list[dict[str, Any]] = []
        for definition in runnable:
            stage_settings: dict[str, Any] = {}
            for section in section_map.get(definition.key, ()):
                stage_settings.update(adapt_runtime_settings(section, resolved.get(section, {})))
            input_roles = self._continuation_input_roles(
                definition.key,
                definition.prerequisite_roles,
                record.workflow_kind,
                input_choices,
                transformations,
            )
            source = None
            if definition.key == "translate":
                for persisted_source_id in (
                    str(stage_settings.get("source_artifact_id") or ""),
                    str(persisted_translation_settings.get("source_artifact_id") or ""),
                ):
                    if source is None and persisted_source_id:
                        source = self._persisted_translation_input(
                            session_id,
                            persisted_source_id,
                        )
            if source is None:
                source = self._latest_stage_input(session_id, input_roles)
            existing = selected.get(canonical_stage_key(definition.key))
            if existing is None:
                continue
            freshness = self._continuation_freshness(
                session_id,
                definition.key,
                stage_settings,
                existing,
                source,
            )
            if freshness["settings_match"] and freshness["source_match"]:
                continue
            mismatches.append(
                {
                    "stage": definition.key,
                    "changed_fields": freshness["changed_fields"],
                    "reasons": freshness["reasons"],
                    "stored": freshness["stored"],
                    "current": freshness["current"],
                }
            )
        return mismatches

    def _session_record(self, session_id: str) -> SessionRecord:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise ValueError(f"Session not found: {session_id}")
            session.expunge(record)
            return record

    def _with_database_llm_settings(self, settings: dict[str, Any], stage: str) -> dict[str, Any]:
        from .provider_settings import build_llm_settings

        aliases = {
            "correction": ("correction_model", "correct_model"),
            "translation": ("translation_model", "translate_model"),
            "tts_optimization": ("tts_optimization_model", "llm_model"),
        }
        requested = str(settings.get("model_name") or "").strip()
        for key in aliases[stage]:
            requested = requested or str(settings.get(key) or "").strip()
        if requested == "default":
            requested = ""
        llm_settings, resolved_model = build_llm_settings(
            self.database,
            self.paths,
            requested_model=requested,
            request_timeout_seconds=int(settings.get("request_timeout_seconds") or 600),
        )
        hydrated = {
            **settings,
            "llm_provider_configs": llm_settings.provider_configs,
            "llm_default_model": llm_settings.default_model,
            "request_timeout_seconds": llm_settings.request_timeout_seconds,
        }
        hydrated[aliases[stage][0]] = requested or resolved_model
        return hydrated
