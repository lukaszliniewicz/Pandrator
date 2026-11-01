"""Workspace-owned persistence helpers for reusable voice collections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Voice, VoiceCollection, VoiceCollectionMember, utcnow
from .settings_policy import RevisionConflict
from .voice_catalog_schemas import (
    VoiceCollectionCreate,
    VoiceCollectionUpdate,
    VoiceReference,
    canonical_voice_key,
)
from .voice_identity import voice_creation_workspace


def _collection_name_exists(
    session: Session,
    name: str,
    *,
    workspace_id: str,
    excluding_id: str | None = None,
) -> bool:
    normalized = name.strip().casefold()
    statement = select(VoiceCollection.name).where(VoiceCollection.workspace_id == workspace_id)
    if excluding_id is not None:
        statement = statement.where(VoiceCollection.id != excluding_id)
    with session.no_autoflush:
        names = session.scalars(statement).all()
    return any(existing.strip().casefold() == normalized for existing in names)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _collection_payload(
    collection: VoiceCollection,
    members: Sequence[VoiceCollectionMember],
) -> dict[str, Any]:
    ordered_members = sorted(members, key=lambda member: member.voice_key)
    return {
        "id": collection.id,
        "name": collection.name,
        "description": collection.description,
        "revision": collection.revision,
        "created_at": _timestamp(collection.created_at),
        "updated_at": _timestamp(collection.updated_at),
        "members": [
            {
                "key": member.voice_key,
                "reference": deepcopy(member.voice_ref_json),
            }
            for member in ordered_members
        ],
        "member_count": len(ordered_members),
    }


def _validate_stored_member(
    member: VoiceCollectionMember,
    linked_voice: Voice | None,
    collection: VoiceCollection,
    *,
    workspace_id: str,
) -> None:
    if member.collection_id != collection.id:
        raise ValueError("A stored voice collection member has an invalid collection link.")
    if member.workspace_id != workspace_id or member.workspace_id != collection.workspace_id:
        raise ValueError("A stored voice collection member has an invalid workspace link.")
    if not isinstance(member.voice_ref_json, Mapping):
        raise ValueError("A stored voice collection member has an invalid reference.")
    try:
        reference = VoiceReference.model_validate(member.voice_ref_json)
        canonical_key = canonical_voice_key(reference)
    except (TypeError, ValidationError, ValueError) as error:
        raise ValueError("A stored voice collection member has an invalid reference.") from error
    if len(canonical_key) > 1024 or member.voice_key != canonical_key:
        raise ValueError("A stored voice collection member has an invalid canonical key.")

    expected_managed_voice_id = reference.voice_id if reference.kind == "managed" else None
    if member.managed_voice_id != expected_managed_voice_id:
        raise ValueError("A stored voice collection member has an invalid managed voice link.")
    if reference.kind == "managed":
        if (
            linked_voice is None
            or linked_voice.id != expected_managed_voice_id
            or linked_voice.workspace_id != workspace_id
        ):
            raise ValueError("A stored managed voice reference is unavailable in this workspace.")
    elif linked_voice is not None:
        raise ValueError("A stored provider reference has an invalid managed voice link.")


def _load_members_by_collection(
    session: Session,
    collections: Sequence[VoiceCollection],
    *,
    workspace_id: str,
) -> dict[str, list[VoiceCollectionMember]]:
    collection_by_id = {collection.id: collection for collection in collections}
    if any(collection.workspace_id != workspace_id for collection in collections):
        raise ValueError("A stored voice collection has an invalid workspace link.")
    members_by_collection: dict[str, list[VoiceCollectionMember]] = {
        collection.id: [] for collection in collections
    }
    statement = (
        select(VoiceCollectionMember, Voice)
        .join(
            VoiceCollection,
            VoiceCollectionMember.collection_id == VoiceCollection.id,
        )
        .outerjoin(
            Voice,
            VoiceCollectionMember.managed_voice_id == Voice.id,
        )
        .where(
            VoiceCollection.workspace_id == workspace_id,
            VoiceCollection.id.in_(collection_by_id),
        )
    )
    statement = statement.order_by(
        VoiceCollectionMember.collection_id, VoiceCollectionMember.voice_key
    )
    with session.no_autoflush:
        rows = session.execute(statement).all()
    for member, linked_voice in rows:
        collection = collection_by_id.get(member.collection_id)
        if collection is None:
            raise ValueError("A stored voice collection member has an invalid collection link.")
        _validate_stored_member(
            member,
            linked_voice,
            collection,
            workspace_id=workspace_id,
        )
        members_by_collection[collection.id].append(member)
    return members_by_collection


def _validate_create_payload(
    payload: VoiceCollectionCreate | Mapping[str, Any],
) -> VoiceCollectionCreate:
    return (
        payload
        if isinstance(payload, VoiceCollectionCreate)
        else VoiceCollectionCreate.model_validate(payload)
    )


def _validate_update_payload(
    payload: VoiceCollectionUpdate | Mapping[str, Any],
) -> VoiceCollectionUpdate:
    return (
        payload
        if isinstance(payload, VoiceCollectionUpdate)
        else VoiceCollectionUpdate.model_validate(payload)
    )


def _member_values(
    reference: VoiceReference,
    *,
    workspace_id: str,
    voices_by_id: Mapping[str, Voice],
) -> tuple[str, str | None, dict[str, Any]]:
    key = canonical_voice_key(reference)
    if len(key) > 1024:
        raise ValueError("The canonical voice reference key is too long.")
    managed_voice_id = reference.voice_id if reference.kind == "managed" else None
    if managed_voice_id is not None:
        voice = voices_by_id.get(managed_voice_id)
        if voice is None or voice.workspace_id != workspace_id:
            raise ValueError(
                f"Managed voice {managed_voice_id!r} does not exist in this workspace."
            )
    return (
        key,
        managed_voice_id,
        reference.model_dump(mode="json", exclude_none=True),
    )


def _managed_voices(
    session: Session,
    references: Sequence[VoiceReference],
    *,
    workspace_id: str,
) -> dict[str, Voice]:
    managed_ids = {
        reference.voice_id
        for reference in references
        if reference.kind == "managed" and reference.voice_id is not None
    }
    if not managed_ids:
        return {}
    with session.no_autoflush:
        voices = session.scalars(
            select(Voice).where(Voice.id.in_(managed_ids), Voice.workspace_id == workspace_id)
        ).all()
    return {voice.id: voice for voice in voices}


def list_collections(
    session: Session,
    *,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the selected workspace's collections and stable voice references."""

    workspace = voice_creation_workspace(session, workspace_id=workspace_id)
    with session.no_autoflush:
        collections = session.scalars(
            select(VoiceCollection)
            .where(VoiceCollection.workspace_id == workspace.id)
            .order_by(VoiceCollection.name, VoiceCollection.id)
        ).all()
    members_by_collection = _load_members_by_collection(
        session,
        collections,
        workspace_id=workspace.id,
    )
    return [
        _collection_payload(collection, members_by_collection[collection.id])
        for collection in collections
    ]


def create_collection(
    session: Session,
    payload: VoiceCollectionCreate | Mapping[str, Any],
    *,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Create an empty collection inside the caller-owned transaction."""

    workspace = voice_creation_workspace(session, workspace_id=workspace_id)
    validated = _validate_create_payload(payload)
    if _collection_name_exists(
        session,
        validated.name,
        workspace_id=workspace.id,
    ):
        raise ValueError("A voice collection with this name already exists.")
    collection = VoiceCollection(
        workspace_id=workspace.id,
        name=validated.name,
        description=validated.description,
    )
    session.add(collection)
    session.flush()
    return _collection_payload(collection, [])


def update_collection(
    session: Session,
    collection_id: str,
    payload: VoiceCollectionUpdate | Mapping[str, Any],
    *,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Apply a revision-checked collection update without committing it."""

    workspace = voice_creation_workspace(session, workspace_id=workspace_id)
    with session.no_autoflush:
        collection = session.scalars(
            select(VoiceCollection).where(
                VoiceCollection.id == collection_id,
                VoiceCollection.workspace_id == workspace.id,
            )
        ).one_or_none()
    if collection is None:
        raise KeyError(collection_id)
    if collection.workspace_id != workspace.id:
        raise KeyError(collection_id)

    validated = _validate_update_payload(payload)
    if collection.revision != validated.expected_revision:
        raise RevisionConflict("The voice collection changed in another client.")

    candidate_name: str | None = None
    if (
        "name" in validated.model_fields_set
        and validated.name is not None
        and validated.name != collection.name
    ):
        if _collection_name_exists(
            session,
            validated.name,
            workspace_id=workspace.id,
            excluding_id=collection.id,
        ):
            raise ValueError("A voice collection with this name already exists.")
        candidate_name = validated.name

    with session.no_autoflush:
        members_by_collection = _load_members_by_collection(
            session,
            [collection],
            workspace_id=workspace.id,
        )
    existing = {member.voice_key: member for member in members_by_collection[collection.id]}

    requested_references = [*validated.add_members, *validated.remove_members]
    voices_by_id = _managed_voices(session, requested_references, workspace_id=workspace.id)
    additions = [
        _member_values(reference, workspace_id=workspace.id, voices_by_id=voices_by_id)
        for reference in validated.add_members
    ]
    removals = [
        _member_values(reference, workspace_id=workspace.id, voices_by_id=voices_by_id)
        for reference in validated.remove_members
    ]

    changed = candidate_name is not None
    description_changed = (
        "description" in validated.model_fields_set
        and validated.description != collection.description
    )
    changed = changed or description_changed

    staged_members = dict(existing)
    removed_members: list[VoiceCollectionMember] = []
    for key, _managed_voice_id, _voice_ref_json in removals:
        member = staged_members.pop(key, None)
        if member is not None:
            removed_members.append(member)
            changed = True

    added_members: list[VoiceCollectionMember] = []
    for key, managed_voice_id, voice_ref_json in additions:
        if key in staged_members:
            continue
        member = VoiceCollectionMember(
            collection_id=collection.id,
            workspace_id=workspace.id,
            voice_key=key,
            voice_ref_json=voice_ref_json,
            managed_voice_id=managed_voice_id,
        )
        staged_members[key] = member
        added_members.append(member)
        changed = True

    if candidate_name is not None:
        collection.name = candidate_name
    if description_changed:
        collection.description = validated.description
    for member in removed_members:
        session.delete(member)
    session.add_all(added_members)
    if changed:
        collection.revision += 1
        collection.updated_at = utcnow()
    session.flush()
    return _collection_payload(collection, list(staged_members.values()))


__all__ = ["create_collection", "list_collections", "update_collection"]
