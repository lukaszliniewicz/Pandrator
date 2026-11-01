"""Atomically save active cast choices and their independently retained inputs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.speech_performance import resolve_capabilities
from pandrator.logic.tts_service_identity import normalize_service_id
from pandrator.runtime import DataPaths

from .access.contracts import ProjectAction
from .access.current_session_policy import require_current_session_access
from .access.session_policy import SessionNotFound
from .access.voice_policy import require_current_voice_library_read
from .auth import Principal
from .database import Database
from .generation_control_schemas import CastSettings, VoiceBinding
from .generation_controls import (
    GENERATION_CONTROLS_KIND,
    iter_cast_voice_bindings,
    prepare_generation_controls_update,
    save_generation_controls,
)
from .idempotency import IdempotencyService
from .models import ApiIdempotency, KnowledgeLedger
from .project_speech_reference_capture import (
    ProjectSpeechReferenceCapture,
    capture_project_speech_reference,
    prepare_project_speech_reference,
    publish_project_speech_reference,
)
from .project_speech_references import (
    StoredProjectSpeechReference,
    clear_project_speech_reference,
    list_project_speech_references,
    put_project_speech_reference,
    speech_reference_selector_hash,
)
from .projects.identity import session_project
from .settings_policy import RevisionConflict
from .speech_input_contracts import (
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    PreparedSpeechInput,
    SpeechInputCapture,
    canonical_json,
)
from .speech_inputs import cleanup_unpublished_speech_input, speech_input_adapter
from .workspace_settings import WorkspaceSettingsService


@dataclass(frozen=True, slots=True)
class ProjectCastSettingsWrite:
    value: dict[str, Any]
    status_code: int = 200
    replayed: bool = False


def _selected_settings(tts: dict[str, Any], binding: VoiceBinding) -> dict[str, Any] | None:
    adapter, aliases = speech_input_adapter(tts)
    if adapter != "audio_cpp" or binding.voice_description:
        return None
    capability = resolve_capabilities(tts)
    if capability.get("voice_mode") in {"prebuilt", "design"}:
        return None
    model = str(capability.get("model") or tts.get("model") or "").strip()
    services = {
        *aliases,
        normalize_service_id(tts.get("service") or tts.get("tts_service")),
        adapter,
    }
    if (
        not model
        or (binding.model and binding.model != model)
        or (binding.service and normalize_service_id(binding.service) not in services)
    ):
        return None
    selected = deepcopy(tts)
    selected.update(model=model, xtts_model=model, voice=binding.voice, speaker=binding.voice)
    for key in (
        "audio_cpp_voice_ref",
        "audio_cpp_voice_ref_hash",
        "audio_cpp_reference_text",
        PROJECT_SPEECH_INPUT_KEY,
        PROJECT_SPEECH_BINDING_KEY,
        PROJECT_SPEECH_SELECTOR_KEY,
    ):
        selected.pop(key, None)
    return selected


def write_project_cast_settings(
    database: Database,
    paths: DataPaths,
    workspace_settings: WorkspaceSettingsService,
    *,
    session_id: str,
    expected_revision: int,
    characters: list[dict[str, Any]] | None = None,
    cast: dict[str, Any] | None = None,
    unlock_ids: list[str] | None = None,
    idempotency: IdempotencyService,
    principal: Principal,
    idempotency_key: str,
) -> ProjectCastSettingsWrite:
    """Stage unique inputs outside the short controls/reference/receipt writer."""
    key = idempotency.validate_key(idempotency_key)
    operation = "updateGenerationControls"
    payload = {"session_id": session_id, "expected_revision": expected_revision}
    payload.update(
        {
            name: value
            for name, value in {
                "characters": characters,
                "cast": cast,
                "unlock_ids": unlock_ids,
            }.items()
            if value is not None
        }
    )

    def guard(session: Session) -> None:
        require_current_session_access(session, session_id, ProjectAction.EDIT)

    def source_guard(session: Session) -> None:
        guard(session)
        require_current_voice_library_read(
            session, session_project(session, session_id).workspace_id
        )

    def references(session: Session) -> dict[str, StoredProjectSpeechReference]:
        return {
            row.slot_key: row
            for row in list_project_speech_references(
                session, session_id, authorization_guard=guard
            )
            if row.slot_key.startswith("cast:")
        }

    def replay(session: Session) -> ProjectCastSettingsWrite | None:
        with session.no_autoflush:
            header = session.execute(
                select(
                    ApiIdempotency.resource_kind,
                    ApiIdempotency.resource_id,
                ).where(
                    ApiIdempotency.principal_subject == principal.subject,
                    ApiIdempotency.operation_id == operation,
                    ApiIdempotency.idempotency_key == key,
                )
            ).one_or_none()
            if header is not None and (
                header.resource_kind is not None or header.resource_id is not None
            ):
                if (
                    header.resource_kind != "generation_controls"
                    or type(header.resource_id) is not str
                ):
                    raise SessionNotFound()
                recorded_session = session.scalar(
                    select(KnowledgeLedger.session_id).where(
                        KnowledgeLedger.id == header.resource_id,
                        KnowledgeLedger.kind == GENERATION_CONTROLS_KIND,
                    )
                )
                if recorded_session is None:
                    raise SessionNotFound()
                require_current_session_access(session, recorded_session, ProjectAction.EDIT)
        reservation = idempotency.replay_if_present(
            session,
            principal=principal,
            operation_id=operation,
            idempotency_key=key,
            payload=payload,
        )
        if reservation is None or reservation.response is None:
            return None
        value, status = reservation.response
        return ProjectCastSettingsWrite(value, status, True)

    captures: dict[str, ProjectSpeechReferenceCapture] = {}
    preparations: dict[SpeechInputCapture, PreparedSpeechInput] = {}
    adopted = False

    def current_candidate(session: Session) -> dict[str, Any]:
        return prepare_generation_controls_update(
            session,
            session_id,
            expected_revision=expected_revision,
            characters=candidate["characters"] if characters is not None else None,
            cast=candidate["cast"] if cast is not None else None,
            unlock_ids=unlock_ids,
        )

    def check_tts(session: Session) -> None:
        resolved, _digest = workspace_settings.resolve_in_session(session, session_id, ["tts"])
        if canonical_json(resolved["tts"]) != tts_json:
            raise RevisionConflict("Project speech settings changed while saving cast inputs.")

    def finish(session: Session) -> ProjectCastSettingsWrite:
        guard(session)
        previous = replay(session)
        if previous is not None:
            return previous
        current_candidate(session)
        check_tts(session)
        if references(session) != previous_references:
            raise RevisionConflict("Saved cast references changed while saving the cast.")
        reservation = idempotency.begin(
            session,
            principal=principal,
            operation_id=operation,
            idempotency_key=key,
            payload=payload,
        )
        if reservation.response is not None:
            value, status = reservation.response
            return ProjectCastSettingsWrite(value, status, True)
        proofs: dict[str, StoredProjectSpeechReference] = {}
        published: dict[SpeechInputCapture, StoredProjectSpeechReference] = {}
        for slot, captured in captures.items():
            core = captured.core_capture
            if core is not None and core.requires_files and core in published:
                first = published[core]
                stored = put_project_speech_reference(
                    session,
                    session_id,
                    slot,
                    selector=captured.selector,
                    binding=captured.binding,
                    pin=first.pin,
                    expected_revision=captured.expected_revision,
                    authorization_guard=guard,
                )
            else:
                stored = publish_project_speech_reference(
                    session,
                    paths,
                    captured,
                    preparations.get(core) if core is not None else None,
                    settings=captured.settings,
                    authorization_guard=guard,
                    source_authorization_guard=source_guard,
                )
                if core is not None and core.requires_files:
                    published[core] = stored
            proofs[slot] = stored
        for slot, old in previous_references.items():
            binding = bindings.get(slot)
            if (
                binding is None
                or speech_reference_selector_hash(binding.model_dump(mode="json"))
                != old.selector_hash
            ):
                if slot not in captures:
                    clear_project_speech_reference(
                        session,
                        session_id,
                        slot,
                        expected_revision=old.revision,
                        authorization_guard=guard,
                    )
            elif cast is None:
                proofs[slot] = old
        value = save_generation_controls(
            session,
            session_id,
            expected_revision=expected_revision,
            characters=candidate["characters"] if characters is not None else None,
            cast=candidate["cast"] if cast is not None else None,
            unlock_ids=unlock_ids,
            _retained_references=proofs,
        )
        idempotency.complete(
            session,
            reservation,
            response=value,
            status_code=200,
            resource_kind="generation_controls",
            resource_id=value["id"],
        )
        return ProjectCastSettingsWrite(value)

    try:
        with database.immediate_session() as session:
            guard(session)
            previous = replay(session)
            if previous is not None:
                return previous
            candidate = prepare_generation_controls_update(
                session,
                session_id,
                expected_revision=expected_revision,
                characters=characters,
                cast=cast,
                unlock_ids=unlock_ids,
            )
            resolved, _digest = workspace_settings.resolve_in_session(session, session_id, ["tts"])
            tts_json = canonical_json(resolved["tts"])
            previous_references = references(session)
            bindings = dict(
                iter_cast_voice_bindings(CastSettings.model_validate(candidate["cast"]))
            )
            if cast is not None:
                for slot, binding in bindings.items():
                    selected = _selected_settings(resolved["tts"], binding)
                    if selected is not None:
                        captures[slot] = capture_project_speech_reference(
                            session,
                            paths,
                            session_id,
                            slot,
                            selector=binding.model_dump(mode="json"),
                            settings=selected,
                            authorization_guard=guard,
                            source_authorization_guard=source_guard,
                        )
            if not any(
                c.core_capture is not None and c.core_capture.requires_files
                for c in captures.values()
            ):
                return finish(session)

        def before_io() -> None:
            with database.snapshot_session() as session:
                source_guard(session)
                current_candidate(session)
                check_tts(session)

        for captured in captures.values():
            core = captured.core_capture
            if core is not None and core.requires_files and core not in preparations:
                prepared = prepare_project_speech_reference(
                    database, paths, captured, before_io=before_io
                )
                assert prepared is not None
                preparations[core] = prepared
        with database.immediate_session() as session:
            result = finish(session)
        adopted = not result.replayed
        return result
    finally:
        if not adopted:
            for prepared in preparations.values():
                cleanup_unpublished_speech_input(database, prepared)
