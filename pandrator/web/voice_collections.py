"""Session-scoped persistence helpers for reusable voice collections."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

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


def _collection_name_exists(
    session: Session,
    name: str,
    *,
    excluding_id: str | None = None,
) -> bool:
    normalized = name.strip().casefold()
    collections = session.scalars(select(VoiceCollection)).all()
    return any(
        collection.id != excluding_id
        and collection.name.strip().casefold() == normalized
        for collection in collections
    )


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _collection_payload(
    session: Session,
    collection: VoiceCollection,
) -> dict[str, Any]:
    members = session.scalars(
        select(VoiceCollectionMember)
        .where(VoiceCollectionMember.collection_id == collection.id)
        .order_by(VoiceCollectionMember.voice_key)
    ).all()
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
            for member in members
        ],
        "member_count": len(members),
    }


def _member_values(
    session: Session,
    reference: VoiceReference,
) -> tuple[str, str | None, dict[str, Any]]:
    key = canonical_voice_key(reference)
    if len(key) > 1024:
        raise ValueError("The canonical voice reference key is too long.")
    managed_voice_id = reference.voice_id if reference.kind == "managed" else None
    if managed_voice_id is not None and session.get(Voice, managed_voice_id) is None:
        raise ValueError(f"Managed voice {managed_voice_id!r} does not exist.")
    return (
        key,
        managed_voice_id,
        reference.model_dump(mode="json", exclude_none=True),
    )


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


def list_collections(session: Session) -> list[dict[str, Any]]:
    """Return all collections and their stable voice references."""

    collections = session.scalars(
        select(VoiceCollection).order_by(VoiceCollection.name, VoiceCollection.id)
    ).all()
    return [_collection_payload(session, collection) for collection in collections]


def create_collection(
    session: Session,
    payload: VoiceCollectionCreate | Mapping[str, Any],
) -> dict[str, Any]:
    """Create an empty collection inside the caller-owned transaction."""

    validated = _validate_create_payload(payload)
    if _collection_name_exists(session, validated.name):
        raise ValueError("A voice collection with this name already exists.")
    collection = VoiceCollection(
        name=validated.name,
        description=validated.description,
    )
    session.add(collection)
    session.flush()
    return _collection_payload(session, collection)


def update_collection(
    session: Session,
    collection_id: str,
    payload: VoiceCollectionUpdate | Mapping[str, Any],
) -> dict[str, Any]:
    """Apply a revision-checked collection update without committing it."""

    collection = session.get(VoiceCollection, collection_id)
    if collection is None:
        raise KeyError(collection_id)
    validated = _validate_update_payload(payload)
    if collection.revision != validated.expected_revision:
        raise RevisionConflict("The voice collection changed in another client.")

    changed = False
    if (
        "name" in validated.model_fields_set
        and validated.name is not None
        and validated.name != collection.name
    ):
        if _collection_name_exists(session, validated.name, excluding_id=collection.id):
            raise ValueError("A voice collection with this name already exists.")
        collection.name = validated.name
        changed = True

    if (
        "description" in validated.model_fields_set
        and validated.description != collection.description
    ):
        collection.description = validated.description
        changed = True

    additions = [
        _member_values(session, reference) for reference in validated.add_members
    ]
    removals = [
        _member_values(session, reference) for reference in validated.remove_members
    ]
    existing: dict[str, VoiceCollectionMember] = {
        member.voice_key: member
        for member in session.scalars(
            select(VoiceCollectionMember).where(
                VoiceCollectionMember.collection_id == collection.id
            )
        ).all()
    }

    for key, _managed_voice_id, _voice_ref_json in removals:
        member = existing.pop(key, None)
        if member is not None:
            session.delete(member)
            changed = True

    for key, managed_voice_id, voice_ref_json in additions:
        if key in existing:
            continue
        member = VoiceCollectionMember(
            collection_id=collection.id,
            voice_key=key,
            voice_ref_json=voice_ref_json,
            managed_voice_id=managed_voice_id,
        )
        session.add(member)
        existing[key] = member
        changed = True

    if changed:
        collection.revision += 1
        collection.updated_at = utcnow()
    session.flush()
    return _collection_payload(session, collection)


__all__ = ["create_collection", "list_collections", "update_collection"]
