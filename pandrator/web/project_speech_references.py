"""Caller-transaction store for saved project speech-reference metadata."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Table, delete, insert, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .access.contracts import AccessDenied
from .accounts.models import Workspace
from .generation_control_schemas import VoiceBinding
from .model_base import utcnow
from .models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    SessionRecord,
)
from .projects.models import Project
from .projects.speech_reference_models import ProjectSpeechReference
from .settings_policy import RevisionConflict
from .speech_input_contracts import (
    SpeechInputOwner,
    canonical_json,
    validate_speech_input_pin,
)
from .speech_inputs import check_speech_input

_CAST_BUCKETS = frozenset({"categories", "characters", "source_speakers"})
_BINDING_LIMITS = {
    "adapter": None,
    "model": 255,
    "voice": 512,
    "display_name": 255,
}
_BINDING_FIELDS = frozenset(_BINDING_LIMITS)
_REFERENCE_COLUMNS = (
    ProjectSpeechReference.session_id,
    ProjectSpeechReference.slot_key,
    ProjectSpeechReference.selector_json,
    ProjectSpeechReference.selector_hash,
    ProjectSpeechReference.binding_json,
    ProjectSpeechReference.input_artifact_id,
    ProjectSpeechReference.input_content_hash,
    ProjectSpeechReference.revision,
)

__all__ = [
    "StoredProjectSpeechReference",
    "clear_project_speech_reference",
    "clone_segment_speech_references",
    "get_project_speech_reference",
    "normalize_speech_reference_binding",
    "list_project_speech_references",
    "normalize_speech_reference_slot",
    "put_project_speech_reference",
    "segment_speech_reference_identity",
    "speech_reference_selector",
    "speech_reference_selector_hash",
]


@dataclass(frozen=True, slots=True)
class StoredProjectSpeechReference:
    """Validated metadata for a session-scoped saved speech selection."""

    owner: SpeechInputOwner
    slot_key: str
    selector: dict[str, Any]
    selector_hash: str
    binding: dict[str, str]
    pin: dict[str, Any] | None
    revision: int


def normalize_speech_reference_slot(value: object) -> str:
    """Validate and return one canonical session speech-reference slot."""
    if type(value) is not str or not value or len(value) > 512:
        raise ValueError("Invalid speech reference slot.")
    if value in {"tts", "cast:narrator"}:
        return value
    if value.startswith("cast:"):
        encoded = value[len("cast:") :]
        try:
            parsed = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ValueError("Invalid cast speech reference slot.") from error
        if type(parsed) is not list or len(parsed) != 2:
            raise ValueError("Invalid cast speech reference slot.")
        bucket, member = parsed
        if type(bucket) is not str or bucket not in _CAST_BUCKETS:
            raise ValueError("Invalid cast speech reference bucket.")
        if (
            type(member) is not str
            or not member
            or member != member.strip()
            or len(member) > 160
            or any(ord(character) < 32 or ord(character) == 127 for character in member)
        ):
            raise ValueError("Invalid cast speech reference member.")
        canonical = "cast:" + canonical_json([bucket, member])
        if value != canonical:
            raise ValueError("Cast speech reference slot is not canonical.")
        return canonical
    if value.startswith("segment:"):
        segment_id = value[len("segment:") :]
        try:
            canonical_id = str(UUID(segment_id))
        except (ValueError, AttributeError) as error:
            raise ValueError("Invalid segment speech reference slot.") from error
        if segment_id != canonical_id:
            raise ValueError("Segment speech reference slot is not canonical.")
        return value
    raise ValueError("Unsupported speech reference slot.")


def speech_reference_selector(value: object) -> dict[str, Any]:
    """Return the strict public VoiceBinding selection including default fields."""
    return VoiceBinding.model_validate(value).model_dump(mode="json")


def _selector_hash(selector: Mapping[str, Any]) -> str:
    try:
        encoded = canonical_json(dict(selector)).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("Invalid speech reference selector.") from error
    return hashlib.sha256(encoded).hexdigest()


def speech_reference_selector_hash(value: object) -> str:
    """Hash a canonical VoiceBinding using the generation binding-key format."""
    return _selector_hash(speech_reference_selector(value))


def normalize_speech_reference_binding(value: object) -> dict[str, str]:
    """Return the finite audio.cpp identity stored with a speech selection."""
    if type(value) is not dict or set(value) != _BINDING_FIELDS:
        raise ValueError("Invalid speech reference binding fields.")
    normalized: dict[str, str] = {}
    for field, limit in _BINDING_LIMITS.items():
        raw = value[field]
        if type(raw) is not str:
            raise ValueError("Speech reference binding values must be strings.")
        text = raw.strip()
        if not text or (limit is not None and len(text) > limit):
            raise ValueError("Invalid speech reference binding value.")
        normalized[field] = text
    if normalized["adapter"] != "audio_cpp":
        raise ValueError("Unsupported speech reference binding adapter.")
    return normalized


def _expected_revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected_revision must be a nonnegative integer.")
    return value


def _guarded_owner(
    session: Session,
    session_id: object,
    authorization_guard: Callable[[Session], None],
) -> SpeechInputOwner:
    if not callable(authorization_guard):
        raise TypeError("authorization_guard must be callable.")
    with session.no_autoflush:
        authorization_guard(session)
        if type(session_id) is not str or not session_id:
            row = None
        else:
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
    return SpeechInputOwner(
        workspace_id=row.workspace_id,
        project_id=row.project_id,
        session_id=row.id,
    )


def _reference_statement(session_id: str, slot_key: str):
    return select(*_REFERENCE_COLUMNS).where(
        ProjectSpeechReference.session_id == session_id,
        ProjectSpeechReference.slot_key == slot_key,
    )


def _stored_reference(
    row: Mapping[Any, Any], owner: SpeechInputOwner
) -> StoredProjectSpeechReference:
    slot_key = normalize_speech_reference_slot(row["slot_key"])
    raw_selector = row["selector_json"]
    if type(raw_selector) is not dict:
        raise ValueError("Stored speech reference selector is invalid.")
    selector = speech_reference_selector(raw_selector)
    if selector != raw_selector:
        raise ValueError("Stored speech reference selector is not canonical.")
    selector_hash = _selector_hash(selector)
    if row["selector_hash"] != selector_hash:
        raise ValueError("Stored speech reference selector hash disagrees.")

    raw_binding = row["binding_json"]
    binding = normalize_speech_reference_binding(raw_binding)
    if binding != raw_binding:
        raise ValueError("Stored speech reference binding is not canonical.")

    artifact_id = row["input_artifact_id"]
    content_hash = row["input_content_hash"]
    if artifact_id is None and content_hash is None:
        pin = None
    elif artifact_id is None or content_hash is None:
        raise ValueError("Stored speech reference pin is incomplete.")
    else:
        pin = validate_speech_input_pin(
            {"version": 1, "artifact_id": artifact_id, "content_hash": content_hash}
        )
    revision = row["revision"]
    if type(revision) is not int or revision < 1:
        raise ValueError("Stored speech reference revision is invalid.")
    return StoredProjectSpeechReference(
        owner=owner,
        slot_key=slot_key,
        selector=copy.deepcopy(selector),
        selector_hash=selector_hash,
        binding=copy.deepcopy(binding),
        pin=copy.deepcopy(pin),
        revision=revision,
    )


def _stored_from_values(
    owner: SpeechInputOwner,
    slot_key: str,
    selector: dict[str, Any],
    selector_hash: str,
    binding: dict[str, str],
    pin: dict[str, Any] | None,
    revision: int,
) -> StoredProjectSpeechReference:
    return StoredProjectSpeechReference(
        owner=owner,
        slot_key=slot_key,
        selector=copy.deepcopy(selector),
        selector_hash=selector_hash,
        binding=copy.deepcopy(binding),
        pin=copy.deepcopy(pin),
        revision=revision,
    )


def get_project_speech_reference(
    session: Session,
    session_id: str,
    slot_key: object,
    *,
    authorization_guard: Callable[[Session], None],
) -> StoredProjectSpeechReference | None:
    owner = _guarded_owner(session, session_id, authorization_guard)
    normalized_slot = normalize_speech_reference_slot(slot_key)
    with session.no_autoflush:
        row = session.execute(
            _reference_statement(session_id, normalized_slot)
        ).mappings().one_or_none()
    if row is None:
        return None
    return _stored_reference(row, owner)


def list_project_speech_references(
    session: Session,
    session_id: str,
    *,
    authorization_guard: Callable[[Session], None],
    slot_keys: Collection[str] | None = None,
    segment_revision_id: str | None = None,
) -> list[StoredProjectSpeechReference]:
    """Read ordered saved metadata after the caller's current root proof."""
    owner = _guarded_owner(session, session_id, authorization_guard)
    return _list_references(session, session_id, owner, slot_keys, segment_revision_id)


def _list_references(
    session: Session,
    session_id: str,
    owner: SpeechInputOwner,
    slot_keys: Collection[str] | None,
    segment_revision_id: str | None = None,
) -> list[StoredProjectSpeechReference]:
    if segment_revision_id is not None:
        if slot_keys is not None:
            raise ValueError("Specify speech reference slots or a segment revision, not both.")
        revision_id = _segment_id(segment_revision_id)
        with session.no_autoflush:
            rows = (
                session.execute(
                    select(*_REFERENCE_COLUMNS)
                    .join(
                        GenerationSegment,
                        ProjectSpeechReference.slot_key == "segment:" + GenerationSegment.id,
                    )
                    .join(
                        GenerationPlanRevision,
                        GenerationPlanRevision.id == GenerationSegment.plan_revision_id,
                    )
                    .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
                    .where(
                        ProjectSpeechReference.session_id == session_id,
                        GenerationPlan.session_id == session_id,
                        GenerationSegment.plan_revision_id == revision_id,
                        GenerationSegment.removed.is_(False),
                    )
                    .order_by(ProjectSpeechReference.slot_key)
                )
                .mappings()
                .all()
            )
        return [_stored_reference(row, owner) for row in rows]
    if slot_keys is not None:
        if isinstance(slot_keys, (str, bytes)) or not isinstance(slot_keys, Collection):
            raise ValueError("Speech reference slots must be a collection.")
        slots = sorted({normalize_speech_reference_slot(key) for key in slot_keys})
        groups = [slots[start : start + 500] for start in range(0, len(slots), 500)]
    else:
        groups = [None]
    result: list[StoredProjectSpeechReference] = []
    with session.no_autoflush:
        for group in groups:
            statement = select(*_REFERENCE_COLUMNS).where(
                ProjectSpeechReference.session_id == session_id
            )
            if group is not None:
                statement = statement.where(ProjectSpeechReference.slot_key.in_(group))
            rows = (
                session.execute(statement.order_by(ProjectSpeechReference.slot_key))
                .mappings()
                .all()
            )
            result.extend(_stored_reference(row, owner) for row in rows)
    return sorted(result, key=lambda row: row.slot_key)


def segment_speech_reference_identity(
    stored: StoredProjectSpeechReference | None,
) -> tuple[str, ...]:
    """Compare a logical saved choice and material, independent of row identity."""
    if stored is None:
        return ("inherit",)
    selector = speech_reference_selector(stored.selector)
    if selector != stored.selector or _selector_hash(selector) != stored.selector_hash:
        raise ValueError("Stored speech reference selector identity is invalid.")
    binding = normalize_speech_reference_binding(stored.binding)
    pin = validate_speech_input_pin(stored.pin)
    return (
        "saved",
        stored.selector_hash,
        binding["adapter"],
        binding["model"],
        binding["voice"],
        pin["content_hash"] if pin is not None else "<null>",
    )


def _segment_id(value: object) -> str:
    if type(value) is not str:
        raise ValueError("Segment IDs must be canonical UUIDs.")
    return normalize_speech_reference_slot("segment:" + value)[len("segment:") :]


def _require_segment_headers(
    session: Session, session_id: str, identifiers: Collection[str]
) -> None:
    identifiers = sorted({_segment_id(identifier) for identifier in identifiers})
    with session.no_autoflush:
        for start in range(0, len(identifiers), 500):
            group = identifiers[start : start + 500]
            found = set(
                session.scalars(
                    select(GenerationSegment.id)
                    .join(
                        GenerationPlanRevision,
                        GenerationPlanRevision.id == GenerationSegment.plan_revision_id,
                    )
                    .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
                    .where(GenerationSegment.id.in_(group), GenerationPlan.session_id == session_id)
                )
            )
            if found != set(group):
                raise ValueError("The segment voice selection does not belong to this project.")


def _require_envelope_headers(
    session: Session,
    owner: SpeechInputOwner,
    references: Collection[StoredProjectSpeechReference],
) -> None:
    pins = {canonical_json(row.pin): row.pin for row in references if row.pin is not None}
    identifiers = sorted({pin["artifact_id"] for pin in pins.values() if pin is not None})
    headers = {}
    with session.no_autoflush:
        for start in range(0, len(identifiers), 500):
            rows = session.execute(
                select(
                    Artifact.id,
                    Artifact.workspace_id,
                    Artifact.project_id,
                    Artifact.session_id,
                    Artifact.kind,
                    Artifact.role,
                    Artifact.mime_type,
                    Artifact.content_hash,
                ).where(Artifact.id.in_(identifiers[start : start + 500]))
            ).all()
            headers.update({row.id: row for row in rows})
    for pin in pins.values():
        assert pin is not None
        header = headers.get(pin["artifact_id"])
        if header is None or (
            header.workspace_id,
            header.project_id,
            header.session_id,
            header.kind,
            header.role,
            header.mime_type,
            header.content_hash,
        ) != (
            owner.workspace_id,
            owner.project_id,
            owner.session_id,
            "speech_voice_snapshot",
            "speech_input",
            "application/json",
            pin["content_hash"],
        ):
            raise ValueError("The saved segment speech input is unavailable in this project.")


def clone_segment_speech_references(
    session: Session,
    session_id: str,
    source_to_targets: Mapping[str, Collection[str]],
    *,
    authorization_guard: Callable[[Session], None],
) -> dict[str, StoredProjectSpeechReference]:
    """Copy saved associations within one owner, including retired input headers."""
    owner = _guarded_owner(session, session_id, authorization_guard)
    if not isinstance(source_to_targets, Mapping):
        raise ValueError("Segment reference copy requires a source-to-target mapping.")
    target_sources: dict[str, list[str]] = {}
    sources: set[str] = set()
    for raw_source, raw_targets in source_to_targets.items():
        source = _segment_id(raw_source)
        if (
            source in sources
            or isinstance(raw_targets, (str, bytes))
            or not isinstance(raw_targets, Collection)
        ):
            raise ValueError("Invalid segment reference copy mapping.")
        sources.add(source)
        targets = [_segment_id(target) for target in raw_targets]
        if len(set(targets)) != len(targets):
            raise ValueError("Duplicate segment reference copy targets.")
        for target in targets:
            target_sources.setdefault(target, []).append(source)
    for target, incoming in target_sources.items():
        if target in sources and incoming != [target]:
            raise ValueError("A copied segment target overlaps another source.")
    _require_segment_headers(session, session_id, sources | target_sources.keys())
    source_rows = {
        row.slot_key[len("segment:") :]: row
        for row in _list_references(
            session, session_id, owner, ["segment:" + source for source in sources]
        )
    }
    target_slots = sorted("segment:" + target for target in target_sources)
    occupied: set[str] = set()
    with session.no_autoflush:
        for start in range(0, len(target_slots), 500):
            occupied.update(
                session.scalars(
                    select(ProjectSpeechReference.slot_key).where(
                        ProjectSpeechReference.session_id == session_id,
                        ProjectSpeechReference.slot_key.in_(target_slots[start : start + 500]),
                    )
                )
            )
    result: dict[str, StoredProjectSpeechReference] = {}
    to_insert: list[dict[str, Any]] = []
    for target, incoming in sorted(target_sources.items()):
        slot = "segment:" + target
        no_op = incoming == [target]
        if slot in occupied and not no_op:
            raise RevisionConflict("The target segment already has a saved speech reference.")
        choices = [source_rows.get(source) for source in sorted(incoming)]
        identities = {segment_speech_reference_identity(choice) for choice in choices}
        if len(identities) != 1:
            raise ValueError(
                "These segments use different saved voice selections. Keep their boundary or choose one voice before merging."
            )
        chosen = choices[0]
        if chosen is None:
            continue
        if no_op:
            result[target] = chosen
            continue
        pin = chosen.pin
        now = utcnow()
        to_insert.append(
            {
                "session_id": session_id,
                "slot_key": slot,
                "selector_json": copy.deepcopy(chosen.selector),
                "selector_hash": chosen.selector_hash,
                "binding_json": copy.deepcopy(chosen.binding),
                "input_artifact_id": pin["artifact_id"] if pin is not None else None,
                "input_content_hash": pin["content_hash"] if pin is not None else None,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
        )
        result[target] = _stored_from_values(
            owner,
            slot,
            chosen.selector,
            chosen.selector_hash,
            chosen.binding,
            chosen.pin,
            1,
        )
    _require_envelope_headers(session, owner, list(source_rows.values()))
    if to_insert:
        try:
            with session.no_autoflush:
                session.execute(insert(cast(Table, ProjectSpeechReference.__table__)), to_insert)
        except IntegrityError as error:
            raise RevisionConflict(
                "The target segment speech reference changed while copying."
            ) from error
    return result

def put_project_speech_reference(
    session: Session,
    session_id: str,
    slot_key: object,
    *,
    selector: object,
    binding: object,
    pin: object,
    expected_revision: int,
    authorization_guard: Callable[[Session], None],
) -> StoredProjectSpeechReference:
    owner = _guarded_owner(session, session_id, authorization_guard)
    normalized_slot = normalize_speech_reference_slot(slot_key)
    expected = _expected_revision(expected_revision)
    normalized_selector = speech_reference_selector(selector)
    selector_hash = _selector_hash(normalized_selector)
    normalized_binding = normalize_speech_reference_binding(binding)
    normalized_pin = validate_speech_input_pin(pin)

    with session.no_autoflush:
        current_revision = session.scalar(
            select(ProjectSpeechReference.revision).where(
                ProjectSpeechReference.session_id == session_id,
                ProjectSpeechReference.slot_key == normalized_slot,
            )
        )
    if current_revision is None:
        if expected != 0:
            raise RevisionConflict("The saved speech reference changed in another client.")
    elif expected == 0 or current_revision != expected:
        raise RevisionConflict("The saved speech reference changed in another client.")

    if normalized_pin is not None:
        check_speech_input(session, normalized_pin, owner=owner)

    artifact_id = normalized_pin["artifact_id"] if normalized_pin is not None else None
    content_hash = normalized_pin["content_hash"] if normalized_pin is not None else None
    if current_revision is None:
        now = utcnow()
        row = ProjectSpeechReference(
            session_id=session_id,
            slot_key=normalized_slot,
            selector_json=copy.deepcopy(normalized_selector),
            selector_hash=selector_hash,
            binding_json=copy.deepcopy(normalized_binding),
            input_artifact_id=artifact_id,
            input_content_hash=content_hash,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush([row])
        return _stored_from_values(
            owner,
            normalized_slot,
            normalized_selector,
            selector_hash,
            normalized_binding,
            normalized_pin,
            1,
        )

    statement = (
        update(ProjectSpeechReference)
        .where(
            ProjectSpeechReference.session_id == session_id,
            ProjectSpeechReference.slot_key == normalized_slot,
            ProjectSpeechReference.revision == expected,
        )
        .values(
            selector_json=copy.deepcopy(normalized_selector),
            selector_hash=selector_hash,
            binding_json=copy.deepcopy(normalized_binding),
            input_artifact_id=artifact_id,
            input_content_hash=content_hash,
            revision=expected + 1,
            updated_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    with session.no_autoflush:
        result = cast(CursorResult[Any], session.execute(statement))
        if result.rowcount != 1:
            raise RevisionConflict("The saved speech reference changed in another client.")
        updated = session.execute(
            _reference_statement(session_id, normalized_slot)
        ).mappings().one_or_none()
    if updated is None:
        raise RevisionConflict("The saved speech reference changed in another client.")
    return _stored_reference(updated, owner)


def clear_project_speech_reference(
    session: Session,
    session_id: str,
    slot_key: object,
    *,
    expected_revision: int,
    authorization_guard: Callable[[Session], None],
) -> bool:
    _guarded_owner(session, session_id, authorization_guard)
    normalized_slot = normalize_speech_reference_slot(slot_key)
    expected = _expected_revision(expected_revision)
    with session.no_autoflush:
        current_revision = session.scalar(
            select(ProjectSpeechReference.revision).where(
                ProjectSpeechReference.session_id == session_id,
                ProjectSpeechReference.slot_key == normalized_slot,
            )
        )
    if current_revision is None:
        if expected == 0:
            return False
        raise RevisionConflict("The saved speech reference changed in another client.")
    if current_revision != expected:
        raise RevisionConflict("The saved speech reference changed in another client.")
    statement = (
        delete(ProjectSpeechReference)
        .where(
            ProjectSpeechReference.session_id == session_id,
            ProjectSpeechReference.slot_key == normalized_slot,
            ProjectSpeechReference.revision == expected,
        )
        .execution_options(synchronize_session=False)
    )
    with session.no_autoflush:
        result = cast(CursorResult[Any], session.execute(statement))
    if result.rowcount != 1:
        raise RevisionConflict("The saved speech reference changed in another client.")
    return True
