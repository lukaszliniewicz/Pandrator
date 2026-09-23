"""Session-scoped character identities and provider-neutral voice casting."""

# Input validation deliberately raises ValueError: API callers translate it to 422.
# ruff: noqa: TRY004

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .generation_control_schemas import (
    CastSettings,
    CharacterEntry,
    GenerationControlsUpdateRequest,
    VoiceBinding,
)
from .settings_policy import RevisionConflict

GENERATION_CONTROLS_KIND = "generation_controls"
_CHARACTER_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,79}$")
_VOICE_CATEGORIES = {"male", "female", "androgynous", "unspecified"}
_PROPOSAL_IGNORED_FIELDS = {"id", "locked", "status", "origin"}


def _session_record(session: Session, session_id: str) -> m.SessionRecord:
    record = session.get(m.SessionRecord, session_id)
    if record is None:
        raise KeyError(session_id)
    return record


def _ledger(session: Session, session_id: str) -> m.KnowledgeLedger | None:
    return session.scalar(
        select(m.KnowledgeLedger).where(
            m.KnowledgeLedger.session_id == session_id,
            m.KnowledgeLedger.kind == GENERATION_CONTROLS_KIND,
            m.KnowledgeLedger.source_language == "auto",
            m.KnowledgeLedger.target_language == "",
        )
    )


def _payload(record: m.KnowledgeLedger | None) -> dict[str, Any]:
    if record is None or not isinstance(record.payload_json, Mapping):
        return {"characters": [], "cast": {}}
    payload = deepcopy(dict(record.payload_json))
    characters = payload.get("characters")
    cast = payload.get("cast")
    payload["characters"] = deepcopy(characters) if isinstance(characters, list) else []
    payload["cast"] = deepcopy(cast) if isinstance(cast, Mapping) else {}
    return payload


def _public(
    session_id: str,
    record: m.KnowledgeLedger | None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state = _payload(record) if payload is None else deepcopy(dict(payload))
    return {
        "id": record.id if record is not None else None,
        "session_id": session_id,
        "revision": int(record.revision) if record is not None else 0,
        "characters": deepcopy(state.get("characters") or []),
        "cast": deepcopy(state.get("cast") or {}),
    }


def _model_dump(model: CharacterEntry | CastSettings) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _character_model(raw: object, *, assign_id: bool = False) -> CharacterEntry:
    if isinstance(raw, CharacterEntry):
        character = CharacterEntry.model_validate(raw.model_dump(mode="json"))
    elif isinstance(raw, Mapping):
        character = CharacterEntry.model_validate(dict(raw))
    else:
        raise ValueError("Each character must be an object.")
    if not character.id and assign_id:
        character.id = f"c-{uuid.uuid4().hex}"
    if not character.id:
        raise ValueError("Character IDs must be nonblank.")
    return character


def _characters(raw: object, *, assign_ids: bool = False) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("Characters must be a list.")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        character = _character_model(item, assign_id=assign_ids)
        if character.id in seen:
            raise ValueError(f"Duplicate character ID: {character.id}")
        seen.add(character.id)
        result.append(_model_dump(character))
    return result


def _cast_model(raw: object) -> CastSettings:
    if isinstance(raw, CastSettings):
        return CastSettings.model_validate(raw.model_dump(mode="json"))
    if raw is None:
        return CastSettings()
    if not isinstance(raw, Mapping):
        raise ValueError("Cast must be an object.")
    return CastSettings.model_validate(dict(raw))


def _validate_managed_voices(session: Session, cast: CastSettings) -> None:
    bindings: list[VoiceBinding] = []
    if cast.narrator is not None:
        bindings.append(cast.narrator)
    bindings.extend(cast.categories.values())
    bindings.extend(cast.characters.values())
    bindings.extend(cast.source_speakers.values())
    for binding in bindings:
        if binding.voice_id and session.get(m.Voice, binding.voice_id) is None:
            raise ValueError(f"Managed voice does not exist: {binding.voice_id}")


def _validate_cast_references(
    cast: CastSettings,
    characters: list[dict[str, Any]],
) -> None:
    character_ids = {str(item["id"]) for item in characters}
    unknown = sorted(set(cast.characters) - character_ids)
    if unknown:
        raise ValueError("Cast references unknown character IDs: " + ", ".join(unknown))


def _identity_key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _identity_values(character: Mapping[str, Any]) -> set[str]:
    values = {_identity_key(character.get("display_name"))}
    values.update(_identity_key(alias) for alias in character.get("aliases", []))
    return {value for value in values if value}


def _assert_locked_changes(
    existing: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    unlock_ids: set[str],
) -> None:
    candidate_by_id = {str(item["id"]): item for item in candidate}
    for old in existing:
        if not old.get("locked"):
            continue
        old_id = str(old["id"])
        current = candidate_by_id.get(old_id)
        if current != old and old_id not in unlock_ids:
            raise RevisionConflict(
                f"Locked character '{old_id}' requires an explicit unlock."
            )


def _validate_update(
    session: Session,
    payload: Mapping[str, Any],
    *,
    characters: object | None,
    cast: object | None,
    unlock_ids: list[str] | None,
) -> dict[str, Any]:
    current_characters = _characters(payload.get("characters", []))
    candidate_characters = (
        current_characters
        if characters is None
        else _characters(characters, assign_ids=True)
    )
    unlock_set = {str(item).strip() for item in (unlock_ids or [])}
    _assert_locked_changes(current_characters, candidate_characters, unlock_set)

    existing_cast_raw = payload.get("cast", {})
    candidate_cast_model = _cast_model(existing_cast_raw if cast is None else cast)
    _validate_cast_references(candidate_cast_model, candidate_characters)
    _validate_managed_voices(session, candidate_cast_model)

    if characters is not None and cast is None:
        old_ids = {str(item["id"]) for item in current_characters}
        new_ids = {str(item["id"]) for item in candidate_characters}
        deleted = old_ids - new_ids
        referenced = set(candidate_cast_model.characters) & deleted
        if referenced:
            raise ValueError(
                "Cannot delete characters still referenced by the existing cast: "
                + ", ".join(sorted(referenced))
            )

    candidate_cast = (
        deepcopy(dict(existing_cast_raw))
        if cast is None and isinstance(existing_cast_raw, Mapping)
        else _model_dump(candidate_cast_model)
    )
    return {
        "characters": candidate_characters,
        "cast": candidate_cast,
    }


def get_generation_controls(session: Session, session_id: str) -> dict[str, Any]:
    """Read session controls without creating a ledger row."""

    _session_record(session, session_id)
    record = _ledger(session, session_id)
    return _public(session_id, record)


def save_generation_controls(
    session: Session,
    session_id: str,
    *,
    expected_revision: int,
    characters: list[dict[str, Any]] | None = None,
    cast: dict[str, Any] | None = None,
    unlock_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Replace supplied control sections inside the caller's transaction."""

    _session_record(session, session_id)
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        raise ValueError("expected_revision must be a nonnegative integer.")
    if expected_revision < 0:
        raise ValueError("expected_revision must be a nonnegative integer.")

    record = _ledger(session, session_id)
    if record is not None and int(expected_revision) != int(record.revision):
        raise RevisionConflict(
            f"Expected revision {expected_revision}, found {record.revision}."
        )
    if record is None and expected_revision != 0:
        raise RevisionConflict(f"Expected revision {expected_revision}, found 0.")

    current_payload = _payload(record)
    candidate = _validate_update(
        session,
        current_payload,
        characters=characters,
        cast=cast,
        unlock_ids=unlock_ids,
    )

    if record is None:
        record = m.KnowledgeLedger(
            session_id=session_id,
            kind=GENERATION_CONTROLS_KIND,
            source_language="auto",
            target_language="",
            payload_json=candidate,
            revision=1,
        )
        session.add(record)
    else:
        record.payload_json = candidate
        record.revision += 1
        record.updated_at = m.utcnow()
    session.flush()
    return _public(session_id, record, candidate)


def _validate_proposal_id(raw_id: object) -> str:
    if not isinstance(raw_id, str):
        raise ValueError("Character proposals require a nonblank stable ID.")
    proposal_id = raw_id
    if not proposal_id or not _CHARACTER_ID_RE.fullmatch(proposal_id):
        raise ValueError(
            "Character proposal IDs must match [A-Za-z][A-Za-z0-9._:-]{0,79}."
        )
    return proposal_id


def _proposal_conflicts(
    raw: Mapping[str, Any],
    proposed: Mapping[str, Any],
    existing: Mapping[str, Any],
) -> bool:
    for field in proposed:
        if field in _PROPOSAL_IGNORED_FIELDS or field not in raw:
            continue
        if proposed[field] != existing.get(field):
            return True
    return False


def merge_character_proposals(
    session: Session,
    session_id: str,
    proposals: list[dict[str, Any]],
    *,
    origin: str,
) -> dict[str, Any]:
    """Merge model identity proposals without touching cast settings."""

    _session_record(session, session_id)
    if not isinstance(proposals, list):
        raise ValueError("Character proposals must be a list.")
    normalized_origin = origin.strip() if isinstance(origin, str) else ""
    if not normalized_origin or len(normalized_origin) > 160:
        raise ValueError("Proposal origin must be between 1 and 160 characters.")

    record = _ledger(session, session_id)
    payload = _payload(record)
    existing = _characters(payload.get("characters", []))
    existing_by_id = {str(item["id"]): item for item in existing}
    seen_ids: set[str] = set()
    new_entries: list[dict[str, Any]] = []

    for raw in proposals:
        if not isinstance(raw, Mapping):
            raise ValueError("Each character proposal must be an object.")
        proposal_id = _validate_proposal_id(raw.get("id"))
        if proposal_id in seen_ids:
            raise ValueError(f"Duplicate character proposal ID: {proposal_id}")
        seen_ids.add(proposal_id)
        candidate = _character_model({**dict(raw), "id": proposal_id})
        candidate_payload = _model_dump(candidate)
        current = existing_by_id.get(proposal_id)
        if current is not None:
            if _proposal_conflicts(raw, candidate_payload, current):
                raise RevisionConflict(
                    f"Character proposal '{proposal_id}' conflicts with the stored identity."
                )
            continue
        candidate_payload["status"] = "proposed"
        candidate_payload["locked"] = False
        candidate_payload["origin"] = normalized_origin
        new_entries.append(candidate_payload)

    if not new_entries:
        return _public(session_id, record, payload)

    existing_identities: dict[str, str] = {}
    for entry in existing:
        for identity in _identity_values(entry):
            existing_identities.setdefault(identity, str(entry["id"]))
    proposal_identities: dict[str, str] = {}
    for entry in new_entries:
        entry_id = str(entry["id"])
        for identity in _identity_values(entry):
            existing_owner = existing_identities.get(identity)
            if existing_owner is not None and existing_owner != entry_id:
                raise ValueError(
                    f"Character name or alias '{identity}' is ambiguous; reuse the existing ID."
                )
            proposal_owner = proposal_identities.get(identity)
            if proposal_owner is not None and proposal_owner != entry_id:
                raise ValueError(
                    f"Character name or alias '{identity}' is ambiguous; reuse the existing ID."
                )
            proposal_identities[identity] = entry_id

    all_entries = existing + new_entries

    payload["characters"] = all_entries
    if record is None:
        record = m.KnowledgeLedger(
            session_id=session_id,
            kind=GENERATION_CONTROLS_KIND,
            source_language="auto",
            target_language="",
            payload_json=payload,
            revision=1,
        )
        session.add(record)
    else:
        record.payload_json = payload
        record.revision += 1
        record.updated_at = m.utcnow()
    session.flush()
    return _public(session_id, record, payload)


def resolve_cast_voice(
    controls: Mapping[str, Any],
    *,
    speaker_id: str | None = None,
    voice_category: str = "unspecified",
    dialogue: bool = False,
    span_voice: str | None = None,
    source_speaker: str | None = None,
) -> dict[str, Any]:
    """Resolve one span to the most specific configured binding."""

    if not isinstance(controls, Mapping):
        raise ValueError("Generation controls must be an object.")
    category = str(voice_category or "").strip().lower()
    if category not in _VOICE_CATEGORIES:
        raise ValueError(f"Unsupported voice category: {voice_category}")
    characters = _characters(controls.get("characters", []))
    character_by_id = {str(item["id"]): item for item in characters}
    normalized_speaker = speaker_id.strip() if speaker_id is not None else None
    if normalized_speaker and normalized_speaker not in character_by_id:
        raise ValueError(f"Unknown character ID: {normalized_speaker}")
    if normalized_speaker and category == "unspecified":
        category = str(
            character_by_id[normalized_speaker].get("voice_category") or category
        )

    cast = _cast_model(controls.get("cast", {}))
    selected: VoiceBinding | None = None
    source = "inherited"
    span = span_voice.strip() if isinstance(span_voice, str) else span_voice
    if span:
        selected = VoiceBinding(voice=span)
        source = "span"
    elif normalized_speaker and normalized_speaker in cast.characters:
        selected = cast.characters[normalized_speaker]
        source = "character"
    else:
        normalized_source = (
            source_speaker.strip() if source_speaker is not None else None
        )
        if normalized_source and normalized_source in cast.source_speakers:
            selected = cast.source_speakers[normalized_source]
            source = "source_speaker"
        elif dialogue and category in cast.categories:
            selected = cast.categories[category]
            source = "category"
        elif cast.narrator is not None:
            selected = cast.narrator
            source = "narrator"

    return {
        "binding": deepcopy(selected.model_dump(mode="json")) if selected else None,
        "source": source,
        "fallback": dialogue and source in {"category", "narrator", "inherited"},
    }


__all__ = [
    "GENERATION_CONTROLS_KIND",
    "GenerationControlsUpdateRequest",
    "RevisionConflict",
    "get_generation_controls",
    "merge_character_proposals",
    "resolve_cast_voice",
    "save_generation_controls",
]
