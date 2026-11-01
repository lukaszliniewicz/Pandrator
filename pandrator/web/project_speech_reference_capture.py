"""Capture saved speech selections without owning caller transactions or cleanup."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.tts_service_identity import normalize_service_id
from pandrator.runtime import DataPaths

from .access.contracts import AccessDenied
from .accounts.models import Workspace
from .database import Database
from .generation_cast_runtime import resolve_binding
from .models import SessionRecord
from .project_speech_references import (
    StoredProjectSpeechReference,
    _require_segment_headers,
    get_project_speech_reference,
    normalize_speech_reference_slot,
    put_project_speech_reference,
    speech_reference_selector,
    speech_reference_selector_hash,
)
from .projects.models import Project
from .settings_policy import RevisionConflict
from .speech_input_contracts import (
    PreparedSpeechInput,
    SpeechInputCapture,
    SpeechInputError,
    SpeechInputOwner,
    canonical_json,
)
from .speech_inputs import (
    capture_speech_input,
    check_speech_input,
    prepare_speech_input,
    publish_speech_input,
    speech_input_adapter,
)

_INLINE_REFERENCE_KEYS = (
    "audio_cpp_voice_ref",
    "audio_cpp_voice_ref_hash",
    "audio_cpp_reference_text",
)


@dataclass(frozen=True, slots=True)
class ProjectSpeechReferenceCapture:
    """Immutable process-local metadata; settings are never stored in the binding."""

    owner: SpeechInputOwner
    slot_key: str
    selector_json: str
    selector_hash: str
    settings_json: str = field(repr=False)
    expected_revision: int
    binding_json: str
    core_capture: SpeechInputCapture | None = field(default=None, repr=False)
    reusable_json: str | None = None

    @property
    def selector(self) -> dict[str, Any]:
        return json.loads(self.selector_json)

    @property
    def settings(self) -> dict[str, Any]:
        return json.loads(self.settings_json)

    @property
    def binding(self) -> dict[str, str]:
        return json.loads(self.binding_json)

    @property
    def reusable(self) -> StoredProjectSpeechReference | None:
        if self.reusable_json is None:
            return None
        value = json.loads(self.reusable_json)
        return StoredProjectSpeechReference(owner=self.owner, **value)


def _guard(session: Session, guard: Callable[[Session], None]) -> None:
    if not callable(guard):
        raise TypeError("Speech reference authorization guard must be callable.")
    with session.no_autoflush:
        guard(session)


def _current_owner(session: Session, session_id: str) -> SpeechInputOwner:
    with session.no_autoflush:
        row = session.execute(
            select(SessionRecord.id, SessionRecord.project_id, Project.workspace_id)
            .select_from(SessionRecord)
            .join(Project, Project.id == SessionRecord.project_id)
            .join(Workspace, Workspace.id == Project.workspace_id)
            .where(
                SessionRecord.id == session_id,
                SessionRecord.trashed_at.is_(None),
                Workspace.state == "active",
            )
        ).one_or_none()
    if row is None:
        raise AccessDenied()
    return SpeechInputOwner(row.workspace_id, row.project_id, row.id)


def _stored_json(stored: StoredProjectSpeechReference) -> str:
    return canonical_json(
        {
            "slot_key": stored.slot_key,
            "selector": stored.selector,
            "selector_hash": stored.selector_hash,
            "binding": stored.binding,
            "pin": stored.pin,
            "revision": stored.revision,
        }
    )


def _text(value: object, limit: int) -> str:
    if type(value) is not str or not value.strip() or len(value.strip()) > limit:
        raise SpeechInputError("Invalid selected speech reference identity.")
    return value.strip()


def _selected_identity(settings: dict[str, Any]) -> tuple[str, str]:
    adapter, _registrations = speech_input_adapter(settings)
    if adapter != "audio_cpp":
        raise SpeechInputError("Saved speech references require the audio.cpp adapter.")
    return adapter, _text(settings.get("model"), 255)


def _validate_selector_identity(
    selector: dict[str, Any], settings: dict[str, Any], adapter: str, model: str
) -> None:
    services = {
        str(settings.get("service") or settings.get("tts_service") or adapter).strip().casefold(),
        adapter.casefold(),
    }
    _adapter, registrations = speech_input_adapter(settings)
    services.update(registrations)
    requested_service = normalize_service_id(selector["service"] or "")
    if requested_service and requested_service not in {
        normalize_service_id(name) for name in services
    }:
        raise SpeechInputError("Speech selector service disagrees with selected settings.")
    if selector["model"] and selector["model"] != model:
        raise SpeechInputError("Speech selector model disagrees with selected settings.")


def capture_speech_selection(
    session: Session,
    paths: DataPaths,
    owner: SpeechInputOwner,
    selector: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[SpeechInputCapture, dict[str, str]]:
    # Alias-only resolution preserves builtin/capability validation without managed-row reads.
    resolved = resolve_binding(session, selector, settings, workspace_id=owner.workspace_id)
    alias = _text(resolved["voice"], 512)
    selected = dict(settings)
    selected["voice"] = selected["speaker"] = alias
    for key in _INLINE_REFERENCE_KEYS:
        selected.pop(key, None)
    core = capture_speech_input(
        session, paths, selected, owner=owner, voice_id=selector["voice_id"]
    )
    adapter, model = _selected_identity(core.settings)
    display_name = (
        json.loads(core.envelope_json)["display_name"] if core.envelope_json is not None else alias
    )
    return core, {
        "adapter": adapter,
        "model": model,
        "voice": _text(core.settings.get("voice"), 512),
        "display_name": _text(display_name, 255),
    }


def capture_project_speech_reference(
    session: Session,
    paths: DataPaths,
    session_id: str,
    slot_key: object,
    *,
    selector: object,
    settings: dict[str, Any],
    authorization_guard: Callable[[Session], None],
    source_authorization_guard: Callable[[Session], None],
    refresh: bool = False,
) -> ProjectSpeechReferenceCapture:
    """Freeze a reusable saved pin or current source metadata, without byte reads."""
    if not callable(source_authorization_guard):
        raise TypeError("source_authorization_guard must be callable.")
    _guard(session, authorization_guard)
    slot = normalize_speech_reference_slot(slot_key)
    strict_selector = speech_reference_selector(selector)
    if strict_selector["voice_description"] or not (
        strict_selector["voice"] or strict_selector["voice_id"]
    ):
        raise SpeechInputError("Select a provider voice or managed audio.cpp reference.")
    settings_json = canonical_json(settings)
    selected_settings = json.loads(settings_json)
    adapter, model = _selected_identity(selected_settings)
    _validate_selector_identity(strict_selector, selected_settings, adapter, model)
    owner = _current_owner(session, session_id)
    stored = get_project_speech_reference(
        session, session_id, slot, authorization_guard=authorization_guard
    )
    selector_hash = speech_reference_selector_hash(strict_selector)
    reusable = (
        stored is not None
        and not refresh
        and stored.selector_hash == selector_hash
        and (stored.binding["adapter"], stored.binding["model"]) == (adapter, model)
    )
    if reusable:
        assert stored is not None
        if stored.pin is not None:
            check_speech_input(session, stored.pin, owner=owner)
        core = None
        binding = stored.binding
    else:
        _guard(session, source_authorization_guard)
        with session.no_autoflush:
            core, binding = capture_speech_selection(
                session, paths, owner, strict_selector, selected_settings
            )
        if (
            stored is not None
            and stored.pin is not None
            and not core.requires_files
            and all(strict_selector[name] == stored.selector[name]
                    for name in ("voice", "voice_id", "voice_description"))
        ):
            raise SpeechInputError(
                "The saved reference is no longer available in the library. "
                "Choose another reference or keep the previous project voice settings."
            )
    return ProjectSpeechReferenceCapture(
        owner=owner,
        slot_key=slot,
        selector_json=canonical_json(strict_selector),
        selector_hash=selector_hash,
        settings_json=settings_json,
        expected_revision=stored.revision if stored is not None else 0,
        binding_json=canonical_json(binding),
        core_capture=core,
        reusable_json=_stored_json(stored) if reusable and stored is not None else None,
    )


def prepare_project_speech_reference(
    database: Database,
    paths: DataPaths,
    capture: ProjectSpeechReferenceCapture,
    *,
    before_io: Callable[[], None],
) -> PreparedSpeechInput | None:
    """Prepare new retained files outside the caller's publication writer."""
    if not callable(before_io):
        raise TypeError("before_io must be callable.")
    core = capture.core_capture
    if core is None or not core.requires_files:
        return None
    return prepare_speech_input(database, paths, core, before_io=before_io)


def publish_project_speech_reference(
    session: Session,
    paths: DataPaths,
    capture: ProjectSpeechReferenceCapture,
    prepared: PreparedSpeechInput | None,
    *,
    settings: dict[str, Any],
    authorization_guard: Callable[[Session], None],
    source_authorization_guard: Callable[[Session], None],
    target_slot_key: str | None = None,
    target_expected_revision: int | None = None,
) -> StoredProjectSpeechReference:
    """Publish in the caller's writer; caller owns CAS and staged-file cleanup."""
    if not callable(source_authorization_guard):
        raise TypeError("source_authorization_guard must be callable.")
    _guard(session, authorization_guard)
    if canonical_json(settings) != capture.settings_json:
        raise RevisionConflict("Selected speech settings changed before publication.")
    session_id = capture.owner.session_id
    if session_id is None or _current_owner(session, session_id) != capture.owner:
        raise SpeechInputError("Selected speech reference owner changed.")
    publication_slot, publication_revision = capture.slot_key, capture.expected_revision
    target = None
    if target_slot_key is not None or target_expected_revision is not None:
        if target_slot_key is None or target_expected_revision is None:
            raise ValueError("Target slot and expected revision must be supplied together.")
        if type(target_expected_revision) is not int or target_expected_revision < 0:
            raise ValueError("Target expected revision must be a nonnegative integer.")
        source_slot = normalize_speech_reference_slot(capture.slot_key)
        target_slot = normalize_speech_reference_slot(target_slot_key)
        if not source_slot.startswith("segment:") or not target_slot.startswith("segment:"):
            raise ValueError("Speech reference retargeting requires two segment slots.")
        _require_segment_headers(session, session_id, [source_slot[8:], target_slot[8:]])
        target = get_project_speech_reference(
            session, session_id, target_slot, authorization_guard=authorization_guard
        )
        if (target.revision if target is not None else 0) != target_expected_revision:
            raise RevisionConflict(
                "The target segment speech reference changed before publication."
            )
        publication_slot, publication_revision = target_slot, target_expected_revision
    stored = get_project_speech_reference(
        session, session_id, capture.slot_key, authorization_guard=authorization_guard
    )
    revision = stored.revision if stored is not None else 0
    if revision != capture.expected_revision:
        raise RevisionConflict("The saved speech reference changed in another client.")
    reusable = capture.reusable
    if reusable is not None:
        if prepared is not None:
            raise SpeechInputError("A reused speech reference cannot publish prepared files.")
        if stored != reusable:
            raise RevisionConflict("The saved speech reference changed before publication.")
        if reusable.pin is not None:
            check_speech_input(session, reusable.pin, owner=capture.owner)
        if target_slot_key is None:
            return reusable
        if target is not None and (
            target.selector,
            target.selector_hash,
            target.binding,
            target.pin,
        ) == (reusable.selector, reusable.selector_hash, reusable.binding, reusable.pin):
            return target
        return put_project_speech_reference(
            session,
            session_id,
            publication_slot,
            selector=capture.selector,
            binding=capture.binding,
            pin=reusable.pin,
            expected_revision=publication_revision,
            authorization_guard=authorization_guard,
        )
    core = capture.core_capture
    if core is None:
        raise SpeechInputError("New speech reference capture is missing.")
    _guard(session, source_authorization_guard)
    if core.requires_files:
        if prepared is None:
            raise SpeechInputError("Selected speech reference requires prepared files.")
        pin = publish_speech_input(session, paths, core, prepared)
        # Core adds the new graph edge after its artifact flush. The store's
        # no-autoflush pin proof must observe that edge in this same transaction.
        session.flush()
    else:
        if prepared is not None:
            raise SpeechInputError("No-reference selection cannot publish prepared files.")
        with session.no_autoflush:
            current, binding = capture_speech_selection(
                session, paths, capture.owner, capture.selector, capture.settings
            )
        if current != core or current.requires_files or binding != capture.binding:
            raise SpeechInputError("Selected no-reference speech input changed before publication.")
        pin = None
    return put_project_speech_reference(
        session,
        session_id,
        publication_slot,
        selector=capture.selector,
        binding=capture.binding,
        pin=pin,
        expected_revision=publication_revision,
        authorization_guard=authorization_guard,
    )
