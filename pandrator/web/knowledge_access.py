"""Current, session-authorized access to optional knowledge ledger rows."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from .access.contracts import AccessContext, AccessDenied, ProjectAction
from .access.session_policy import require_session_access
from .accounts.execution_actor import UserActor
from .knowledge import KnowledgeLedgerStore, _empty_payload
from .model_base import utcnow
from .models import KnowledgeLedger

_INVALID_SCOPE = "Invalid knowledge ledger scope."
_INVALID_LEDGER_ID = "Invalid knowledge ledger identifier."
_INVALID_REVISION = "Invalid knowledge ledger revision."
_REVISION_CONFLICT = "The knowledge ledger changed in another client."
_MAX_SESSION_ID_LENGTH = 36
_MAX_LEDGER_ID_LENGTH = 36
_MAX_KIND_LENGTH = 32
_MAX_LANGUAGE_LENGTH = 40


class KnowledgeLedgerNotFound(KeyError):
    """A stored ledger identifier is absent."""

    def __init__(self) -> None:
        super().__init__("Knowledge ledger not found.")


class KnowledgeLedgerRevisionConflict(ValueError):
    """A ledger was changed after the caller observed its revision."""

    def __init__(self) -> None:
        super().__init__(_REVISION_CONFLICT)


def _access_context(context: object) -> AccessContext:
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    if type(context.actor) is not UserActor:
        raise AccessDenied()
    return cast(AccessContext, context)


def _scope(
    session_id: object,
    kind: object,
    source_language: object,
    target_language: object,
) -> tuple[str, str, str, str]:
    if (
        type(session_id) is not str
        or not session_id
        or len(session_id) > _MAX_SESSION_ID_LENGTH
        or type(kind) is not str
        or len(kind) > _MAX_KIND_LENGTH
        or type(source_language) is not str
        or len(source_language) > _MAX_LANGUAGE_LENGTH
        or type(target_language) is not str
        or len(target_language) > _MAX_LANGUAGE_LENGTH
    ):
        raise ValueError(_INVALID_SCOPE)

    normalized_kind = kind.strip().lower()
    if normalized_kind not in KnowledgeLedgerStore.KINDS:
        raise ValueError(_INVALID_SCOPE)
    source = source_language.strip().lower() or "auto"
    target = target_language.strip().lower()
    return session_id, normalized_kind, source, target


def _ledger_id(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_LEDGER_ID_LENGTH:
        raise ValueError(_INVALID_LEDGER_ID)
    return value


def _expected_revision(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(_INVALID_REVISION)
    return value


def _serialize(record: KnowledgeLedger) -> dict[str, Any]:
    updated_at = record.updated_at
    if updated_at is not None and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return {
        "id": record.id,
        "session_id": record.session_id,
        "kind": record.kind,
        "source_language": record.source_language,
        "target_language": record.target_language,
        "payload": deepcopy(record.payload_json or {}),
        "revision": record.revision,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
    }


def _virtual_empty(
    session_id: str,
    kind: str,
    source_language: str,
    target_language: str,
) -> dict[str, Any]:
    return {
        "id": None,
        "session_id": session_id,
        "kind": kind,
        "source_language": source_language,
        "target_language": target_language,
        "payload": _empty_payload(kind),
        "revision": 0,
        "updated_at": None,
    }


def _authorize(
    session: Session,
    context: AccessContext,
    session_id: str,
    action: ProjectAction,
) -> None:
    _access_context(context)
    with session.no_autoflush:
        require_session_access(session, context, session_id, action)


def read_ledger(
    session: Session,
    context: AccessContext,
    session_id: str,
    kind: str,
    *,
    source_language: str = "auto",
    target_language: str = "",
) -> dict[str, Any]:
    normalized_session, normalized_kind, source, target = _scope(
        session_id, kind, source_language, target_language
    )
    _authorize(session, context, normalized_session, ProjectAction.READ)
    with session.no_autoflush:
        record = session.execute(
            select(KnowledgeLedger).where(
                KnowledgeLedger.session_id == normalized_session,
                KnowledgeLedger.kind == normalized_kind,
                KnowledgeLedger.source_language == source,
                KnowledgeLedger.target_language == target,
            )
        ).scalar_one_or_none()
    if record is None:
        return _virtual_empty(normalized_session, normalized_kind, source, target)
    return _serialize(record)


def ensure_ledger(
    session: Session,
    context: AccessContext,
    session_id: str,
    kind: str,
    *,
    source_language: str = "auto",
    target_language: str = "",
) -> tuple[dict[str, Any], bool]:
    normalized_session, normalized_kind, source, target = _scope(
        session_id, kind, source_language, target_language
    )
    _authorize(session, context, normalized_session, ProjectAction.EDIT)
    with session.no_autoflush:
        record = session.execute(
            select(KnowledgeLedger).where(
                KnowledgeLedger.session_id == normalized_session,
                KnowledgeLedger.kind == normalized_kind,
                KnowledgeLedger.source_language == source,
                KnowledgeLedger.target_language == target,
            )
        ).scalar_one_or_none()
        if record is not None:
            return _serialize(record), False

        record = KnowledgeLedger(
            session_id=normalized_session,
            kind=normalized_kind,
            source_language=source,
            target_language=target,
            payload_json=_empty_payload(normalized_kind),
        )
        session.add(record)
        session.flush([record])
    return _serialize(record), True


def replace_ledger(
    session: Session,
    context: AccessContext,
    ledger_id: str,
    *,
    expected_revision: int,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    validated_context = _access_context(context)
    identifier = _ledger_id(ledger_id)
    expected = _expected_revision(expected_revision)

    with session.no_autoflush:
        owner_session_id = session.scalar(
            select(KnowledgeLedger.session_id).where(KnowledgeLedger.id == identifier)
        )
    if owner_session_id is None:
        raise KnowledgeLedgerNotFound()

    _authorize(session, validated_context, owner_session_id, ProjectAction.EDIT)
    with session.no_autoflush:
        record = session.execute(
            select(KnowledgeLedger)
            .where(KnowledgeLedger.id == identifier)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if record is None:
            raise KnowledgeLedgerNotFound()
        if record.revision != expected:
            raise KnowledgeLedgerRevisionConflict()

        validated_payload = KnowledgeLedgerStore._validated_payload(
            record.kind,
            payload,
        )
        record.payload_json = validated_payload
        record.revision += 1
        record.updated_at = utcnow()
        session.flush([record])

    return {
        "id": record.id,
        "payload": deepcopy(record.payload_json),
        "revision": record.revision,
        "updated_at": record.updated_at.isoformat(),
    }


__all__ = [
    "KnowledgeLedgerNotFound",
    "KnowledgeLedgerRevisionConflict",
    "ensure_ledger",
    "read_ledger",
    "replace_ledger",
]
