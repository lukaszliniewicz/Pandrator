"""Process-local preparation of run-owned narrator references outside writers."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from . import models as m
from .access.contracts import ProjectAction
from .access.current_session_policy import require_current_session_access
from .access.voice_policy import require_current_voice_library_read
from .database import Database
from .generation_rendering import is_strict_single_voice
from .project_speech_reference_capture import _current_owner, capture_speech_selection
from .project_speech_references import speech_reference_selector_hash
from .speech_input_contracts import (
    BASE_VOICE_SETTING_KEYS,
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
    MissingSpeechSelection,
    PreparedSpeechInput,
    PreparedSpeechSelection,
    SpeechInputCapture,
    SpeechInputError,
    SpeechInputOwner,
    canonical_json,
    validate_speech_input_pin,
)
from .speech_inputs import (
    check_speech_input,
    cleanup_unpublished_speech_input,
    prepare_speech_input,
    publish_speech_input,
)

# Compatibility name for earlier internal callers.
PreparedNarratorSelection = PreparedSpeechSelection
MissingNarratorSelection = MissingSpeechSelection


@dataclass(slots=True)
class _PreparedMaterial:
    core: SpeechInputCapture
    prepared: PreparedSpeechInput | None


@dataclass(frozen=True, slots=True)
class _Selection:
    value: PreparedSpeechSelection
    settings_json: str
    material_key: str
    core: SpeechInputCapture


def _surface(snapshot: dict[str, Any]) -> str:
    keys = (*BASE_VOICE_SETTING_KEYS, "service", "tts_service", "voice_description",
            "voice_mode_version", "casting_enabled", "performance_enabled",
            "service_id", "preview_service_id", "preview_adapter")
    tts = snapshot.get("tts") or {}
    alternate = (snapshot.get("selected_segment_override") or {}).get("tts") or {}
    return canonical_json({
        "tts": {key: tts[key] for key in keys if key in tts},
        "selected_tts": {key: alternate[key] for key in keys if key in alternate},
        "cast": snapshot.get("generation_control_snapshot"),
    })


class GenerationSpeechPreparation:
    """Own lazy run choices and deduplicated stages, never saved associations."""

    def __init__(
        self, database: Database, paths: DataPaths, owner: SpeechInputOwner,
        revision_id: str, *, operation: str,
        selected_override: dict[str, Any], before_io: Callable[[], None] | None = None,
        reference_identity: Callable[[str, str, str, object, bool], dict[str, str]] | None = None,
        reference_item_hash: Callable[[Any], str] | None = None,
    ) -> None:
        self._database, self._paths, self._owner = database, paths, owner
        self._revision_id, self._operation = revision_id, operation
        selected_tts = selected_override.get("tts") or {}
        self._selected_voice = any(key in selected_tts for key in ("voice", "speaker", "voice_id"))
        self._before_io = before_io
        self._materials: dict[str, _PreparedMaterial] = {}
        self._selections: dict[str, _Selection] = {}
        self._reference_identity = reference_identity
        self._reference_item_hash = reference_item_hash
        self._cast_indexes: dict[str, str] = {}
        self._frozen_surface: str | None = None

    def _guard(self, session: Session, *, source: bool = False) -> None:
        sid = self._owner.session_id
        if sid is None:
            raise SpeechInputError("Run speech input requires a session owner.")
        require_current_session_access(session, sid, ProjectAction.RUN)
        if _current_owner(session, sid) != self._owner:
            raise SpeechInputError("Run speech input owner changed.")
        revision_owner = session.scalar(
            select(m.GenerationPlan.session_id)
            .join(m.GenerationPlanRevision, m.GenerationPlanRevision.plan_id == m.GenerationPlan.id)
            .where(m.GenerationPlanRevision.id == self._revision_id)
        )
        if revision_owner != sid:
            raise SpeechInputError("Run speech input revision changed.")
        if source:
            require_current_voice_library_read(session, self._owner.workspace_id)

    def _io(self) -> None:
        with self._database.snapshot_session() as session:
            self._guard(session, source=True)
        if self._before_io is not None:
            self._before_io()

    def missing_selection(
        self, session: Session, selector: dict[str, Any], settings: dict[str, Any],
    ) -> PreparedNarratorSelection | None:
        if "audio_cpp_voice_ref" in settings:
            return None
        strict_single = is_strict_single_voice(settings)
        if (self._operation == "rvc" or (self._selected_voice and not strict_single)
                or settings.get("casting_enabled")
                or (not strict_single and settings.get("performance_enabled"))):
            return None
        self._guard(session)
        if not strict_single:
            # This header-only probe occurs only after the binder's exact miss.
            assigned = session.scalar(
                select(m.GenerationSegment.id)
                .join(m.GenerationPlanRevision, m.GenerationPlanRevision.id == m.GenerationSegment.plan_revision_id)
                .join(m.GenerationPlan, m.GenerationPlan.id == m.GenerationPlanRevision.plan_id)
                .outerjoin(m.ProjectSpeechReference, (
                    (m.ProjectSpeechReference.session_id == self._owner.session_id)
                    & (m.ProjectSpeechReference.slot_key == "segment:" + m.GenerationSegment.id)
                ))
                .where(
                    m.GenerationPlan.session_id == self._owner.session_id,
                    m.GenerationSegment.plan_revision_id == self._revision_id,
                    m.GenerationSegment.removed.is_(False),
                    or_(m.GenerationSegment.voice_id.is_not(None),
                        m.GenerationSegment.voice.is_not(None),
                        m.ProjectSpeechReference.slot_key.is_not(None)),
                ).limit(1)
            )
            if assigned is not None:
                return None
        return self.missing_audible_selection(session, selector, settings)

    def _cast_reference_hashes(
        self, session: Session, core: SpeechInputCapture, prepared: PreparedSpeechInput,
    ) -> list[str]:
        if self._reference_item_hash is None:
            return []
        from .generation_voice_references import voice_reference_index

        state = json.loads(core.source_state_json or "{}")
        voice_id = state["voice"]["id"]
        if voice_id not in self._cast_indexes:
            self._io()
            index = voice_reference_index(session, self._paths,
                workspace_id=self._owner.workspace_id, _voice_ids={voice_id})
            self._cast_indexes[voice_id] = canonical_json(index.get(voice_id.casefold(), []))
        transcript_hash = self._reference_item_hash(
            state["sample"]["transcript"] if state["sample"]["transcript_reviewed"] else ""
        )
        matches = []
        for item in json.loads(self._cast_indexes[voice_id]):
            if item.get("id") != voice_id:
                continue
            if any(sample.get("id") == state["sample"]["id"]
                   and sample.get("artifact_id") == state["artifact"]["id"]
                   and sample.get("content_hash") == prepared.audio.content_hash
                   and sample.get("transcript_hash") == transcript_hash
                   and sample.get("state") == "current"
                   for sample in item.get("samples", [])):
                matches.append(self._reference_item_hash(item))
        return sorted(set(matches))

    def missing_audible_selection(
        self, session: Session, selector: dict[str, Any], settings: dict[str, Any],
    ) -> PreparedSpeechSelection | None:
        from .project_tts_settings import _retained_selection

        if (self._operation == "rvc" or selector.get("voice_description")
                or "audio_cpp_voice_ref" in settings or _retained_selection(settings) is None):
            return None
        self._guard(session, source=True)
        key = canonical_json({"selector": selector, "settings": settings})
        if key in self._selections:
            return self._selections[key].value
        if self._before_io is not None:
            self._before_io()
        core, binding = capture_speech_selection(session, self._paths, self._owner, selector, settings)
        material_key = canonical_json({
            "source": core.source_state_json, "envelope": core.envelope_json,
            "fingerprint": core.source_fingerprint, "binding": binding,
        })
        material = self._materials.get(material_key)
        if material is None:
            material = _PreparedMaterial(core, None)
            self._materials[material_key] = material
            if core.requires_files:
                material.prepared = prepare_speech_input(
                    self._database, self._paths, core, before_io=self._io,
                )
        prepared = material.prepared
        pin = None if prepared is None else validate_speech_input_pin({
            "version": 1, "artifact_id": prepared.snapshot.artifact_id,
            "content_hash": prepared.snapshot.content_hash,
        })
        source = None
        if prepared is not None and self._reference_identity is not None:
            state = json.loads(core.source_state_json or "{}")
            sample, artifact = state["sample"], state["artifact"]
            source = {
                "version": 2, "pin": pin, "binding": binding,
                "reference_identity": self._reference_identity(
                    sample["id"], artifact["id"], prepared.audio.content_hash,
                    sample["transcript"], sample["transcript_reviewed"],
                ),
            }
            if settings.get("casting_enabled"):
                hashes = self._cast_reference_hashes(session, core, prepared)
                if hashes:
                    source["cast_reference_hashes"] = hashes
        value = PreparedSpeechSelection(
            canonical_json(selector), canonical_json(binding), canonical_json(pin),
            canonical_json(source) if source is not None else None,
        )
        self._selections[key] = _Selection(value, canonical_json(settings), material_key, core)
        return value

    def seal(self, snapshot: dict[str, Any]) -> None:
        if self._selections:
            self._frozen_surface = _surface(snapshot)

    def pending_proof(
        self, session: Session, owner: SpeechInputOwner, revision_id: str | None,
        pin: dict[str, Any] | None, binding: dict[str, Any],
    ) -> bool:
        if owner != self._owner or revision_id != self._revision_id:
            return False
        self._guard(session)
        for item in self._selections.values():
            prepared = self._materials[item.material_key].prepared
            if (prepared is not None and prepared.snapshot.owned
                    and item.value.pin == pin and item.value.binding == binding
                    and pin == {"version": 1, "artifact_id": prepared.snapshot.artifact_id,
                                "content_hash": prepared.snapshot.content_hash}):
                return True
        return False

    def publish(
        self, session: Session, snapshot: dict[str, Any], *, session_id: str, revision_id: str,
    ) -> None:
        if not self._selections:
            return
        if session_id != self._owner.session_id or revision_id != self._revision_id:
            raise SpeechInputError("Prepared run speech input has a different owner or revision.")
        self._guard(session, source=True)
        if _surface(snapshot) != self._frozen_surface:
            raise SpeechInputError("Prepared run speech snapshot changed before publication.")
        # Every prepared descriptor must still occur on the sealed finite surface.
        occurrences = []
        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if PROJECT_SPEECH_BINDING_KEY in value:
                    occurrences.append(value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(json.loads(self._frozen_surface or "{}"))
        for item in self._selections.values():
            value = item.value
            if not any(
                occurrence.get(PROJECT_SPEECH_INPUT_KEY) == value.pin
                and occurrence.get(PROJECT_SPEECH_BINDING_KEY) == value.binding
                and occurrence.get(PROJECT_SPEECH_SELECTOR_KEY) == speech_reference_selector_hash(value.selector)
                and occurrence.get(PROJECT_SPEECH_SOURCE_KEY) == value.source
                for occurrence in occurrences
            ):
                raise SpeechInputError("Prepared run speech selection is absent or changed.")
            # Recapture metadata per selector, including explicit null aliases.
            current, binding = capture_speech_selection(
                session, self._paths, self._owner, value.selector, json.loads(item.settings_json),
            )
            if current != item.core or binding != value.binding:
                raise SpeechInputError("Selected run speech input changed before publication.")
        for material in self._materials.values():
            if material.core.requires_files:
                if material.prepared is None:
                    raise SpeechInputError("Prepared run speech files are missing.")
                published = publish_speech_input(session, self._paths, material.core, material.prepared)
                expected = validate_speech_input_pin({
                    "version": 1, "artifact_id": material.prepared.snapshot.artifact_id,
                    "content_hash": material.prepared.snapshot.content_hash,
                })
                if published != expected:
                    raise SpeechInputError("Published run speech input disagrees with its prepared pin.")
        session.flush()
        for item in self._selections.values():
            check_speech_input(session, item.value.pin, owner=self._owner)

    def cleanup(self) -> None:
        for material in self._materials.values():
            if material.prepared is not None:
                cleanup_unpublished_speech_input(self._database, material.prepared)
