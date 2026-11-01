"""Save a project voice and its retained local input in one guarded publication."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.speech_performance import resolve_capabilities
from pandrator.runtime import DataPaths

from .access.contracts import AccessContext, ProjectAction
from .access.current_session_policy import require_current_session_access
from .access.session_policy import SessionNotFound
from .access.voice_policy import require_current_voice_library_read
from .access.workspace_policy import require_workspace_access
from .accounts.execution_actor import UserActor, current_execution_actor
from .auth import Principal
from .database import Database
from .idempotency import IdempotencyService
from .models import ApiIdempotency
from .project_speech_reference_capture import (
    ProjectSpeechReferenceCapture,
    capture_project_speech_reference,
    prepare_project_speech_reference,
    publish_project_speech_reference,
)
from .project_speech_references import (
    clear_project_speech_reference,
    get_project_speech_reference,
    normalize_speech_reference_binding,
    speech_reference_selector_hash,
)
from .projects.identity import session_project
from .speech_input_contracts import (
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
    SHA256,
    MissingSpeechSelection,
    PreparedSpeechInput,
    PreparedSpeechSelection,
    SpeechInputError,
    SpeechInputOwner,
    retained_speech_selection,
    validate_speech_input_pin,
)
from .speech_inputs import (
    check_speech_input,
    cleanup_unpublished_speech_input,
    speech_input_adapter,
)
from .workspace_settings import WorkspaceSettingsService


@dataclass(frozen=True, slots=True)
class ProjectTtsSettingsWrite:
    value: dict[str, Any]
    status_code: int = 200
    replayed: bool = False


def _retained_selection(settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    adapter = speech_input_adapter(settings)[0]
    return retained_speech_selection(
        settings, adapter, resolve_capabilities(settings) if adapter == "audio_cpp" else {},
    )


def bind_project_tts_reference(
    session: Session, session_id: str, snapshot: dict[str, Any], *,
    _missing_selection: MissingSpeechSelection | None = None,
    _resolve_missing_managed: bool = False,
) -> None:
    """Freeze an already saved narrator reference without consulting its library."""
    def guard(current: Session) -> None:
        require_current_session_access(current, session_id)

    raw = snapshot.get("tts")
    if not isinstance(raw, dict):
        return
    settings = deepcopy(raw)
    marker_present = PROJECT_SPEECH_INPUT_KEY in settings or PROJECT_SPEECH_BINDING_KEY in settings
    saved_binding = None
    freshly_prepared = False
    pin = None
    if marker_present:
        if PROJECT_SPEECH_INPUT_KEY not in settings or PROJECT_SPEECH_BINDING_KEY not in settings:
            raise SpeechInputError("Saved speech input metadata is incomplete.")
        saved_binding = normalize_speech_reference_binding(settings[PROJECT_SPEECH_BINDING_KEY])
        pin = validate_speech_input_pin(settings[PROJECT_SPEECH_INPUT_KEY])
        carried_selector = settings.get(PROJECT_SPEECH_SELECTOR_KEY)
        if type(carried_selector) is not str or SHA256.fullmatch(carried_selector) is None:
            raise SpeechInputError("Saved speech selection identity is incomplete. Start a new run.")
    selection = _retained_selection(settings)
    if selection is None:
        settings.pop(PROJECT_SPEECH_SOURCE_KEY, None)
        settings.pop(PROJECT_SPEECH_INPUT_KEY, None)
        settings.pop(PROJECT_SPEECH_BINDING_KEY, None)
        settings.pop(PROJECT_SPEECH_SELECTOR_KEY, None)
        snapshot["tts"] = settings
        return
    selector, selected = selection
    selector_hash = speech_reference_selector_hash(selector)
    guard(session)
    if saved_binding is not None and (
        (saved_binding["model"], saved_binding["voice"]) != (selected["model"], selected["voice"])
        or settings[PROJECT_SPEECH_SELECTOR_KEY] != selector_hash
    ):
        # A new alternate model/voice must not inherit its source run's input.
        saved_binding = None
        pin = None
        settings.pop(PROJECT_SPEECH_SOURCE_KEY, None)
    if saved_binding is None:
        # Source compatibility belongs only to an unchanged carried pin/binding.
        settings.pop(PROJECT_SPEECH_SOURCE_KEY, None)
        stored = get_project_speech_reference(session, session_id, "tts", authorization_guard=guard)
        if stored is None or stored.selector_hash != selector_hash:
            fresh = _missing_selection(session, selector, selected) if _missing_selection else None
            if fresh is not None:
                if not isinstance(fresh, PreparedSpeechSelection):
                    raise SpeechInputError("Invalid prepared narrator selection.")
                saved_binding = normalize_speech_reference_binding(fresh.binding)
                pin = validate_speech_input_pin(fresh.pin)
                from .speech_input_contracts import canonical_json

                if (fresh.selector_json != canonical_json(selector)
                        or saved_binding["adapter"] != "audio_cpp"
                        or saved_binding["model"] != selected["model"]
                        or (not selector["voice_id"] and saved_binding["voice"] != selector["voice"])):
                    raise SpeechInputError("Prepared narrator disagrees with the selected speech settings.")
                if fresh.source is not None:
                    settings[PROJECT_SPEECH_SOURCE_KEY] = fresh.source
                freshly_prepared = True
            elif selector["voice_id"]:
                if _resolve_missing_managed:
                    from .generation_cast_runtime import resolve_binding

                    root = session_project(session, session_id)
                    require_current_voice_library_read(session, root.workspace_id)
                    resolved = resolve_binding(
                        session, selector, selected, workspace_id=root.workspace_id,
                    )
                    settings.update(model=selected["model"], xtts_model=selected["model"],
                        voice=resolved["voice"], speaker=resolved["voice"])
                    for key in (PROJECT_SPEECH_INPUT_KEY, PROJECT_SPEECH_BINDING_KEY,
                                PROJECT_SPEECH_SELECTOR_KEY):
                        settings.pop(key, None)
                    snapshot["tts"] = settings
                    return
                raise SpeechInputError(
                    "Save this managed voice as the project voice before starting a new generation run."
                )
            else:
                settings.pop(PROJECT_SPEECH_INPUT_KEY, None)
                settings.pop(PROJECT_SPEECH_BINDING_KEY, None)
                settings.pop(PROJECT_SPEECH_SELECTOR_KEY, None)
                snapshot["tts"] = settings
                return
        else:
            saved_binding, pin = stored.binding, stored.pin
        if saved_binding["model"] != selected["model"] or (
            not selector["voice_id"] and saved_binding["voice"] != selector["voice"]
        ):
            raise SpeechInputError("Saved speech binding disagrees with the project selection.")
    if not freshly_prepared:
        root = session_project(session, session_id)
        check_speech_input(
            session, pin,
            owner=SpeechInputOwner(root.workspace_id, root.id, session_id),
        )
    for key in ("audio_cpp_voice_ref", "audio_cpp_voice_ref_hash", "audio_cpp_reference_text"):
        settings.pop(key, None)
    settings.update(
        model=saved_binding["model"], xtts_model=saved_binding["model"],
        voice=saved_binding["voice"], speaker=saved_binding["voice"],
    )
    settings[PROJECT_SPEECH_INPUT_KEY] = pin
    settings[PROJECT_SPEECH_BINDING_KEY] = saved_binding
    settings[PROJECT_SPEECH_SELECTOR_KEY] = selector_hash
    snapshot["tts"] = settings


def write_project_tts_settings(
    database: Database,
    paths: DataPaths,
    workspace_settings: WorkspaceSettingsService,
    *,
    session_id: str,
    expected_revision: int,
    value: dict[str, Any],
    patch: bool = False,
    idempotency: IdempotencyService | None = None,
    principal: Principal | None = None,
    idempotency_key: str | None = None,
) -> ProjectTtsSettingsWrite:
    """Stage selected bytes outside the settings writer; replay before library use."""
    if idempotency_key is not None and (idempotency is None or principal is None):
        raise TypeError("A retry key requires its idempotency service and principal.")
    operation = "patchSessionSettings" if patch else "putSessionSettings"
    payload = {
        "session_id": session_id,
        "section": "tts",
        "expected_revision": expected_revision,
        "value": value,
    }

    def guard(session: Session) -> None:
        require_current_session_access(session, session_id, ProjectAction.EDIT)

    def source_guard(session: Session) -> None:
        guard(session)
        actor = current_execution_actor.get()
        if type(actor) is UserActor:
            root = session_project(session, session_id)
            require_workspace_access(session, AccessContext(actor), root.workspace_id)

    def replay(session: Session) -> ProjectTtsSettingsWrite | None:
        if idempotency_key is None:
            return None
        assert idempotency is not None and principal is not None
        with session.no_autoflush:
            header = session.execute(select(
                ApiIdempotency.resource_kind, ApiIdempotency.resource_id,
            ).where(
                ApiIdempotency.principal_subject == principal.subject,
                ApiIdempotency.operation_id == operation,
                ApiIdempotency.idempotency_key == idempotency_key,
            )).one_or_none()
            if header is not None and (header.resource_kind is not None or header.resource_id is not None):
                if header.resource_kind != "session_settings" or type(header.resource_id) is not str:
                    raise SessionNotFound()
                original_session, separator, original_section = header.resource_id.rpartition(":")
                if not separator or not original_session or not original_section:
                    raise SessionNotFound()
                if original_session != session_id:
                    require_current_session_access(session, original_session, ProjectAction.EDIT)
        reservation = idempotency.replay_if_present(
            session, principal=principal, operation_id=operation,
            idempotency_key=idempotency_key, payload=payload,
        )
        if reservation is None or reservation.response is None:
            return None
        body, status = reservation.response
        return ProjectTtsSettingsWrite(body, status, True)

    def finish(
        session: Session,
        capture: ProjectSpeechReferenceCapture | None = None,
        prepared: PreparedSpeechInput | None = None,
    ) -> ProjectTtsSettingsWrite:
        guard(session)
        reservation = None
        if idempotency_key is not None:
            assert idempotency is not None and principal is not None
            reservation = idempotency.begin(
                session, principal=principal, operation_id=operation,
                idempotency_key=idempotency_key, payload=payload,
            )
            if reservation.response is not None:
                body, status = reservation.response
                return ProjectTtsSettingsWrite(body, status, True)
        mutate = workspace_settings.patch if patch else workspace_settings.update
        result = mutate(session_id, "tts", expected_revision, value, db_session=session)
        if capture is not None:
            current, _digest = workspace_settings.resolve_in_session(session, session_id, ["tts"])
            selection = _retained_selection(current["tts"])
            if selection is None:
                from .settings_policy import RevisionConflict

                raise RevisionConflict("The selected project voice changed while retaining its input.")
            publish_project_speech_reference(
                session, paths, capture, prepared, settings=selection[1],
                authorization_guard=guard, source_authorization_guard=source_guard,
            )
        else:
            previous = get_project_speech_reference(
                session, session_id, "tts", authorization_guard=guard
            )
            if previous is not None:
                clear_project_speech_reference(
                    session, session_id, "tts", expected_revision=previous.revision,
                    authorization_guard=guard,
                )
        if reservation is not None:
            assert idempotency is not None
            idempotency.complete(
                session, reservation, response=result, status_code=200,
                resource_kind="session_settings", resource_id=f"{session_id}:tts",
            )
        return ProjectTtsSettingsWrite(result)

    prepared = None
    adopted = False
    try:
        with database.immediate_session() as session:
            guard(session)
            previous_response = replay(session)
            if previous_response is not None:
                return previous_response
            _candidate, resolved = workspace_settings.tts_update_candidate_in_session(
                session, session_id, expected_revision, value, patch=patch
            )
            selection = _retained_selection(resolved)
            if selection is None:
                return finish(session)
            capture = capture_project_speech_reference(
                session, paths, session_id, "tts", selector=selection[0], settings=selection[1],
                authorization_guard=guard, source_authorization_guard=source_guard,
            )

        def before_io() -> None:
            with database.snapshot_session() as session:
                source_guard(session)
                # Settings CAS is repeated in the final writer. Avoid copying
                # more bytes after an already observable settings change.
                workspace_settings.tts_update_candidate_in_session(
                    session, session_id, expected_revision, value, patch=patch
                )

        prepared = prepare_project_speech_reference(database, paths, capture, before_io=before_io)
        with database.immediate_session() as session:
            guard(session)
            previous_response = replay(session)
            if previous_response is not None:
                return previous_response
            result = finish(session, capture, prepared)
        adopted = True
        return result
    finally:
        if prepared is not None and not adopted:
            cleanup_unpublished_speech_input(database, prepared)
