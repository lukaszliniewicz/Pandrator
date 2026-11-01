"""Capture a bounded, authorized saved graph without writing or touching files."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Iterator, Mapping
from copy import deepcopy
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import MetaData, Table, and_, func, inspect, literal, or_, select
from sqlalchemy.orm import Session

from .. import models as m
from ..access.artifact_policy import visible_artifact_ids_statement
from ..access.contracts import AccessContext, AccessDenied, ProjectAction
from ..access.project_policy import require_project_access, require_project_creation
from ..generation_control_schemas import CastSettings, VoiceBinding
from ..generation_controls import iter_cast_voice_bindings
from ..knowledge import KnowledgeLedgerStore, KnowledgeValidationError
from ..project_cast_settings import _selected_settings
from ..recorded_speech_contracts import PROVENANCE_KEY
from ..settings_policy import BUILTIN_DEFAULTS, SETTING_SECTIONS, _secret_free
from ..speech_input_contracts import SpeechInputOwner
from .copy_audio import (
    audio_metadata,
    capture_audio_evidence,
    selected_take_headers,
    supported_take_header,
    validate_audio_copy_graph,
)
from .copy_connections import (
    bind_destination_connections,
    collect_requirements,
    connection_key,
    private_model_selector,
    validate_connection_graph,
    validate_connection_state,
)
from .copy_contracts import (
    COPY_STATE_VERSION,
    CopyFileSource,
    CopySettingsReader,
    ProjectCopyError,
    ProjectCopySnapshot,
)
from .copy_evidence import (
    ACTIVE_EVIDENCE_STATUSES,
    EVIDENCE_STATUSES,
    evidence_dependencies,
    evidence_reference_sessions,
    referenced_evidence_ids,
    sanitize_evidence,
    sanitize_evidence_artifact_metadata,
    validate_evidence_graph,
)
from .copy_media import (
    MAX_MEDIA_EDIT_PLANS,
    MAX_MEDIA_EDIT_REVISIONS,
    MAX_MEDIA_PAYLOAD_ROWS,
    freeze_revision,
    rebind_capture_metadata,
    validate_media_graph,
)
from .copy_media import (
    PLAN_FIELDS as MEDIA_PLAN_FIELDS,
)
from .copy_media import (
    REVISION_FIELDS as MEDIA_REVISION_FIELDS,
)
from .copy_media import (
    validate_revision as validate_media_revision,
)
from .copy_outputs import (
    SAVED_SPEECH_ROLE,
    SOUND_MIX_ROLE,
    freeze_saved_speech_output,
    validate_output_copy_graph,
)
from .copy_performance import PERFORMANCE_FAMILIES, validate_performance_copy_graph
from .copy_performance_capture import capture_current_performance
from .copy_sound import SOUND_FAMILIES, freeze_sound_artifact, validate_sound_copy_graph
from .copy_sound_capture import capture_current_sounds
from .copy_speech import (
    freeze_cast_reference,
    freeze_generation_controls,
    freeze_segment_reference,
    freeze_tts_reference,
    validate_speech_copy_graph,
)
from .copy_speech_plan import (
    PLAN_FIELDS as SPEECH_PLAN_FIELDS,
)
from .copy_speech_plan import (
    REVIEW_FIELDS as SPEECH_REVIEW_FIELDS,
)
from .copy_speech_plan import (
    REVISION_FIELDS as SPEECH_REVISION_FIELDS,
)
from .copy_speech_plan import (
    SEGMENT_FIELDS as SPEECH_SEGMENT_FIELDS,
)
from .copy_speech_plan import (
    SPEECH_FAMILIES,
    editorial_revision_settings,
    freeze_speech_plan_rows,
    validate_speech_plan_graph,
)
from .models import Project

MAX_SESSIONS = 101
MAX_ARTIFACTS = 10_000
MAX_EDITORIAL_ROWS = 250_000
MAX_KNOWLEDGE_LEDGER_ROWS = 1_000
MAX_SUBTITLE_EVIDENCE_ROWS = 1_000
MAX_EVIDENCE_CLOSURE_PASSES = 32
_CHUNK = 500
_SUPPORTED_KNOWLEDGE_KINDS = frozenset({"research", "glossary", "generation_controls"})
_MEDIA_PAIR_HEADER_CHUNK = 250
_TERMINAL = frozenset({"completed", "succeeded", "failed", "canceled", "cancelled", "superseded"})
_SUPPORTED = frozenset(
    {
        "projects",
        "translation_projects",
        "translation_project_branches",
        "session_settings",
        "outcome_plans",
        "session_stage_selections",
        "session_sources",
        "sources",
        "documents",
        "artifacts",
        "subtitle_evidence",
        "media_edit_plans",
        "media_edit_plan_revisions",
        "project_sound_plans",
        "performance_plans",
    }
)
_UNSUPPORTED = frozenset(
    {
        "generation_plans",
        "pronunciation_entries",
        "knowledge_ledgers",
    }
)
_OPERATIONAL = frozenset(
    {
        "jobs",
        "output_assemblies",
        "generation_runs",
        "workflow_runs",
        "workflow_execution_plans",
        "exports",
        "usage_events",
        "upload_sessions",
        "session_settings_history",
        "outcome_plan_history",
        "project_folder_memberships",
    }
)
_EDITORIAL = frozenset(
    {
        "agent_runs",
        "dispatch_runs",
        "media_edit_dispatch_runs",
        "source_cleaning_dispatch_runs",
        "speech_optimization_dispatch_runs",
    }
)
_MESSAGES = {
    "copy_source_unavailable": "Restore the source project before copying it.",
    "copy_limit_exceeded": "This project exceeds the current copy limits.",
    "copy_graph_invalid": "The saved project state is incomplete or inconsistent.",
}


def _error(code: str) -> ProjectCopyError:
    return ProjectCopyError(code, _MESSAGES[code])


def _chunks(values: Iterable[str]) -> Iterator[list[str]]:
    ordered = sorted(set(values))
    for start in range(0, len(ordered), _CHUNK):
        yield ordered[start : start + _CHUNK]


def _rows(session: Session, model: Any, clause: Any) -> list[dict[str, Any]]:
    table = model.__table__
    return [dict(row) for row in session.execute(select(table).where(clause)).mappings()]


def _content(row: Mapping[str, Any], *, omit: Iterable[str] = ()) -> dict[str, Any]:
    # SQL row mappings contain detached JSON values; serialization below breaks
    # their last possible connection with mutable ORM values held by callers.
    excluded = {"created_at", "updated_at", *omit}
    return {key: _secret_free(value) for key, value in row.items() if key not in excluded}


def _select_chunks(
    session: Session, model: Any, column: Any, ids: Iterable[str]
) -> list[dict[str, Any]]:
    return [row for chunk in _chunks(ids) for row in _rows(session, model, column.in_(chunk))]


def _nonempty(value: Any) -> bool:
    return value is not None and value != "" and value != {} and value != [] and value is not False


def _dependency(key: str, value: Any, ids: set[str]) -> bool:
    normalized = key.lower()
    if isinstance(value, str) and value.startswith("managed:"):
        return True
    if normalized in {"voice", "voice_ref"} and isinstance(value, str) and value:
        try:
            uuid.UUID(value)
        except ValueError:
            pass
        else:
            return True
    if normalized in {"voice_ref", "voice_reference"} and isinstance(value, dict):
        return value.get("kind") == "managed"
    if "voice" in normalized and normalized.endswith(("_id", "_ids")):
        return _nonempty(value)
    if ("source" in normalized or "sound" in normalized) and normalized.endswith(("_id", "_ids")):
        values = value if isinstance(value, list) else [value]
        return any(
            _nonempty(item) and (not isinstance(item, str) or item not in ids) for item in values
        )
    return False


def _settings(
    value: Any,
    defaults: Any,
    section: str,
    ids: set[str],
    blockers: set[tuple[str, str]],
    removed: dict[str, int],
    *,
    _retained_voice_id: str | None = None,
    _root: bool = True,
) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            default = defaults.get(key) if isinstance(defaults, dict) else None
            retained_identity = (
                _root
                and section == "tts"
                and key == "voice_id"
                and _retained_voice_id is not None
                and item == _retained_voice_id
            )
            if not retained_identity and _dependency(key, item, ids):
                blockers.add(("unsupported_state", "reusable_editorial_dependency"))
            if key not in _secret_free({key: item}):
                if _nonempty(item):
                    blockers.add(("destination_binding_required", section))
                    removed["secret"] = removed.get("secret", 0) + 1
            elif connection_key(key):
                if _nonempty(item) and item != default:
                    blockers.add(("destination_binding_required", section))
                    removed["connection"] = removed.get("connection", 0) + 1
            elif private_model_selector(item):
                blockers.add(("unsupported_state", "custom_settings"))
            else:
                result[key] = _settings(item, default, section, ids, blockers, removed, _root=False)
        return result
    if isinstance(value, list):
        if any(private_model_selector(item) for item in value):
            blockers.add(("unsupported_state", "custom_settings"))
        return [
            _settings(item, None, section, ids, blockers, removed, _root=False)
            for item in value
            if not private_model_selector(item)
        ]
    return value


def _artifact_closure(
    session: Session, context: AccessContext, seeds: set[str], *,
    sound_artifact_ids: set[str] | None = None, source_project_id: str | None = None,
):
    # Walk only ownership/dependency scalars first. Authorize the complete set
    # in fixed chunks before hydrating protected artifact/file metadata.
    retained_ids: set[str] = set()
    sound_artifact_ids = sound_artifact_ids or set()
    pending = set(seeds)
    edges: list[dict[str, Any]] = []
    while pending:
        if len(retained_ids) + len(pending) > MAX_ARTIFACTS:
            raise _error("copy_limit_exceeded")
        next_ids: set[str] = set()
        for chunk in _chunks(pending):
            pointers = {
                row.id: (row.state, row.role)
                for row in session.execute(
                    select(m.Artifact.id, m.Artifact.state, m.Artifact.role).where(m.Artifact.id.in_(chunk))
                )
            }
            if set(pointers) != set(chunk) or any(state == "deleted" for state, _role in pointers.values()):
                raise _error("copy_graph_invalid")
            retained_ids.update(chunk)
            traversable = [identifier for identifier, (_state, role) in pointers.items()
                           if role not in {"assembled_audio", SAVED_SPEECH_ROLE, SOUND_MIX_ROLE}
                           and identifier not in sound_artifact_ids]
            parents = (_rows(session, m.ArtifactEdge, m.ArtifactEdge.child_artifact_id.in_(traversable))
                       if traversable else [])
            edges.extend(parents)
            next_ids.update(row["parent_artifact_id"] for row in parents)
        pending = next_ids - retained_ids
    retained: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(retained_ids):
        visible = set(
            session.scalars(
                visible_artifact_ids_statement(
                    session, context, sound_dependency_ids=set(chunk) & sound_artifact_ids,
                    sound_dependency_project_id=source_project_id,
                ).where(m.Artifact.id.in_(chunk))
            )
        )
        if visible != set(chunk):
            raise AccessDenied()
        ordinary = set(chunk) - sound_artifact_ids
        for row in (_rows(session, m.Artifact, m.Artifact.id.in_(ordinary)) if ordinary else []):
            if row["role"] in {"assembled_audio", SAVED_SPEECH_ROLE, SOUND_MIX_ROLE}:
                try:
                    row = freeze_saved_speech_output(row, allow_sound_mix=True)
                except ValueError as error:
                    raise _error("copy_graph_invalid") from error
            retained[row["id"]] = row
        selected_sounds = set(chunk) & sound_artifact_ids
        if selected_sounds:
            fields = (
                "id", "workspace_id", "project_id", "session_id", "kind", "role",
                "relative_path", "mime_type", "size_bytes", "content_hash", "state",
            )
            columns = [getattr(m.Artifact, key) for key in fields]
            for row in session.execute(
                select(*columns).where(m.Artifact.id.in_(selected_sounds))
            ).mappings():
                try:
                    retained[row["id"]] = freeze_sound_artifact({key: row[key] for key in fields})
                except (ValueError, TypeError, KeyError) as error:
                    raise _error("copy_graph_invalid") from error
    return retained, edges


def _unsupported(session: Session, ids: set[str], blockers: set[tuple[str, str]],
                 copied_cast: Mapping[str, set[str]], uncaptured_plans: int) -> dict[str, int]:
    inspector = inspect(session.connection())
    counts: dict[str, int] = {}
    for name in sorted(inspector.get_table_names()):
        columns = {
            column
            for fk in inspector.get_foreign_keys(name)
            if fk["referred_table"] == "sessions"
            for column in fk["constrained_columns"]
        }
        if not columns or name in _SUPPORTED or name in _OPERATIONAL:
            continue
        table = Table(name, MetaData(), autoload_with=session.connection(), resolve_fks=False)
        count = uncaptured_plans if name == "generation_plans" else 0
        for chunk in (() if name == "generation_plans" else _chunks(ids)):
            clause = or_(*(table.c[column].in_(chunk) for column in columns))
            if name == "knowledge_ledgers":
                clause &= table.c.kind.not_in(_SUPPORTED_KNOWLEDGE_KINDS)
            if name == "project_speech_references":
                # Current segment choices have an adapter. Non-current segment
                # slots belong to excluded revision history and need no bodies.
                clause &= table.c.slot_key != "tts"
                clause &= ~table.c.slot_key.like("segment:%")
                if copied_cast:
                    clause &= ~or_(*(and_(table.c.session_id == sid, table.c.slot_key.in_(slots))
                                     for sid, slots in copied_cast.items() if slots))
            if name in _EDITORIAL:
                clause &= or_(
                    table.c.status.is_(None), func.lower(table.c.status).not_in(_TERMINAL)
                )
            count += session.scalar(select(func.count()).select_from(table).where(clause)) or 0
        if count:
            if name in _EDITORIAL:
                blockers.add(("unsupported_state", "editorial_operation"))
            else:
                # Deliberate classification, not a suffix-based automatic clone.
                blockers.add(("unsupported_state", name))
                counts[name] = count
    return counts


def _knowledge_ledgers(session: Session, ids: set[str]) -> list[dict[str, Any]]:
    count = sum(
        session.scalar(
            select(func.count())
            .select_from(m.KnowledgeLedger)
            .where(
                m.KnowledgeLedger.session_id.in_(chunk),
                m.KnowledgeLedger.kind.in_(_SUPPORTED_KNOWLEDGE_KINDS),
            )
        )
        or 0
        for chunk in _chunks(ids)
    )
    if count > MAX_KNOWLEDGE_LEDGER_ROWS:
        raise _error("copy_limit_exceeded")

    ledgers = [
        row
        for chunk in _chunks(ids)
        for row in _rows(
            session,
            m.KnowledgeLedger,
            and_(
                m.KnowledgeLedger.session_id.in_(chunk),
                m.KnowledgeLedger.kind.in_(_SUPPORTED_KNOWLEDGE_KINDS),
            ),
        )
    ]
    scopes: set[tuple[str, str, str, str]] = set()
    captured: list[dict[str, Any]] = []
    for row in ledgers:
        kind = row["kind"]
        source_language = row["source_language"]
        target_language = row["target_language"]
        if (
            type(row["id"]) is not str
            or not row["id"]
            or type(row["session_id"]) is not str
            or row["session_id"] not in ids
            or type(kind) is not str
            or kind not in _SUPPORTED_KNOWLEDGE_KINDS
            or type(source_language) is not str
            or not source_language
            or source_language != source_language.strip().lower()
            or len(source_language) > 40
            or type(target_language) is not str
            or target_language != target_language.strip().lower()
            or len(target_language) > 40
        ):
            raise _error("copy_graph_invalid")
        scope = (row["session_id"], kind, source_language, target_language)
        if scope in scopes:
            raise _error("copy_graph_invalid")
        scopes.add(scope)
        try:
            if kind == "generation_controls":
                row["payload_json"] = freeze_generation_controls(row)
            else:
                KnowledgeLedgerStore._validated_payload(kind, row["payload_json"])
        except (KnowledgeValidationError, TypeError, ValueError) as error:
            raise _error("copy_graph_invalid") from error
        captured.append(_content(row, omit=("revision",)))
    return captured


def _documents(
    session: Session,
    ids: set[str],
    artifacts: Mapping[str, dict[str, Any]],
    additional_revision_ids: set[str] | None = None,
    media_revision_ids: set[str] | None = None,
    reserved_editorial_rows: int = 0,
):
    document_count = sum(
        session.scalar(
            select(func.count()).select_from(m.Document).where(m.Document.session_id.in_(chunk))
        )
        or 0
        for chunk in _chunks(ids)
    )
    if document_count > MAX_EDITORIAL_ROWS:
        raise _error("copy_limit_exceeded")
    documents = _select_chunks(session, m.Document, m.Document.session_id, ids)
    by_document = {row["id"]: row for row in documents}
    revisions = {row["active_revision_id"] for row in documents if row["active_revision_id"]}
    revisions.update(additional_revision_ids or ())
    for row in artifacts.values():
        value = (row["metadata_json"] or {}).get("revision_id")
        if value:
            if not isinstance(value, str):
                raise _error("copy_graph_invalid")
            if value not in (media_revision_ids or set()):
                revisions.add(value)
    revision_rows = _select_chunks(session, m.DocumentRevision, m.DocumentRevision.id, revisions)
    by_revision = {row["id"]: row for row in revision_rows}
    if set(by_revision) != revisions:
        raise _error("copy_graph_invalid")
    for row in revision_rows:
        if row["document_id"] not in by_document:
            raise _error("copy_graph_invalid")
    if any(
        row["active_revision_id"]
        and by_revision[row["active_revision_id"]]["document_id"] != row["id"]
        for row in documents
    ):
        raise _error("copy_graph_invalid")
    segments, words = [], []
    total = reserved_editorial_rows + len(documents) + len(revision_rows)
    for model, target in [(m.Segment, segments), (m.TimedWord, words)]:
        for chunk in _chunks(revisions):
            count = (
                session.scalar(
                    select(func.count()).select_from(model).where(model.revision_id.in_(chunk))
                )
                or 0
            )
            total += count
            if total > MAX_EDITORIAL_ROWS:
                raise _error("copy_limit_exceeded")
            target.extend(_rows(session, model, model.revision_id.in_(chunk)))
    if total > MAX_EDITORIAL_ROWS:
        raise _error("copy_limit_exceeded")
    by_segment = {row["id"]: row for row in segments}
    if any(
        row["segment_id"] is not None
        and (
            row["segment_id"] not in by_segment
            or by_segment[row["segment_id"]]["revision_id"] != row["revision_id"]
        )
        for row in words
    ):
        raise _error("copy_graph_invalid")
    lineage = []
    for chunk in _chunks(by_segment):
        lineage.extend(
            row
            for row in _rows(
                session, m.SegmentLineage, m.SegmentLineage.child_segment_id.in_(chunk)
            )
            if row["parent_segment_id"] in by_segment
        )
    return {
        "documents": documents,
        "document_revisions": revision_rows,
        "segments": segments,
        "timed_words": words,
        "segment_lineage": lineage,
    }


def _media_plans(session: Session, session_ids: set[str]) -> list[dict[str, Any]]:
    count = sum(
        session.scalar(
            select(func.count())
            .select_from(m.MediaEditPlan)
            .where(m.MediaEditPlan.session_id.in_(chunk))
        )
        or 0
        for chunk in _chunks(session_ids)
    )
    if count > MAX_MEDIA_EDIT_PLANS:
        raise _error("copy_limit_exceeded")
    return [
        {key: row[key] for key in MEDIA_PLAN_FIELDS}
        for row in _select_chunks(session, m.MediaEditPlan, m.MediaEditPlan.session_id, session_ids)
    ]


def _media_selection(
    session: Session,
    session_ids: set[str],
    plans: list[dict[str, Any]],
    artifacts: Mapping[str, dict[str, Any]],
    translation_projects: list[dict[str, Any]],
    revision_row_cache: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Select media payloads only after scoped revision ownership headers."""
    if not plans:
        if any(
            row.get("source_media_edit_revision_id") is not None
            or row.get("source_media_edit_content_hash") is not None
            for row in translation_projects
        ):
            raise _error("copy_graph_invalid")
        for artifact in artifacts.values():
            metadata = artifact.get("metadata_json")
            if type(metadata) is not dict:
                raise _error("copy_graph_invalid")
            if metadata.get("media_edit_revision_id") is not None:
                raise _error("copy_graph_invalid")
        return [], set(), set()

    plan_ids = {row["id"] for row in plans}
    active_ids: set[str] = set()
    dedicated_ids: set[str] = set()
    generic_ids: set[str] = set()
    pair_refs: set[tuple[str, int]] = set()
    media_role_pair_refs: set[tuple[str, int]] = set()

    for plan in plans:
        active_id = plan["active_revision_id"]
        if active_id is not None:
            if type(active_id) is not str or not active_id:
                raise _error("copy_graph_invalid")
            active_ids.add(active_id)

    for artifact in artifacts.values():
        metadata = artifact.get("metadata_json")
        if type(metadata) is not dict:
            raise _error("copy_graph_invalid")
        role = artifact.get("role")
        media_role = type(role) is str and role.startswith("media_edit_")
        dedicated = metadata.get("media_edit_revision_id")
        if dedicated is not None:
            if type(dedicated) is not str or not dedicated:
                raise _error("copy_graph_invalid")
            dedicated_ids.add(dedicated)
        generic = metadata.get("revision_id")
        if generic is not None:
            if type(generic) is not str:
                raise _error("copy_graph_invalid")
            if generic:
                generic_ids.add(generic)
        plan_id = metadata.get("plan_id")
        number = metadata.get("revision", metadata.get("revision_number"))
        if plan_id is not None or number is not None:
            if media_role and (type(plan_id) is not str or not plan_id or type(number) is not int):
                raise _error("copy_graph_invalid")
            if plan_id is not None and number is not None:
                if type(plan_id) is not str or not plan_id or type(number) is not int:
                    raise _error("copy_graph_invalid")
                pair_refs.add((plan_id, number))
                if media_role:
                    media_role_pair_refs.add((plan_id, number))

    for translation in translation_projects:
        revision_id = translation.get("source_media_edit_revision_id")
        content_hash = translation.get("source_media_edit_content_hash")
        if (revision_id is None) != (content_hash is None):
            raise _error("copy_graph_invalid")
        if revision_id is not None:
            if type(revision_id) is not str or not revision_id:
                raise _error("copy_graph_invalid")
            dedicated_ids.add(revision_id)

    requested_ids = active_ids | dedicated_ids | generic_ids
    revision_table = m.MediaEditPlanRevision.__table__
    plan_table = m.MediaEditPlan.__table__
    join = revision_table.join(plan_table, revision_table.c.plan_id == plan_table.c.id)
    headers: dict[str, tuple[str, str | None, int]] = {}
    for chunk in _chunks(requested_ids):
        statement = (
            select(
                revision_table.c.id,
                revision_table.c.plan_id,
                revision_table.c.parent_revision_id,
                revision_table.c.revision_number,
            )
            .select_from(join)
            .where(
                revision_table.c.id.in_(chunk),
                plan_table.c.id.in_(plan_ids),
                plan_table.c.session_id.in_(session_ids),
            )
        )
        for row in session.execute(statement).mappings():
            headers[row["id"]] = (
                row["plan_id"],
                row["parent_revision_id"],
                row["revision_number"],
            )

    for start in range(0, len(pair_refs), _MEDIA_PAIR_HEADER_CHUNK):
        chunk_pairs = sorted(pair_refs)[start : start + _MEDIA_PAIR_HEADER_CHUNK]
        clauses = [
            and_(
                revision_table.c.plan_id == plan_id,
                revision_table.c.revision_number == number,
            )
            for plan_id, number in chunk_pairs
        ]
        statement = (
            select(
                revision_table.c.id,
                revision_table.c.plan_id,
                revision_table.c.parent_revision_id,
                revision_table.c.revision_number,
            )
            .select_from(join)
            .where(
                plan_table.c.id.in_(plan_ids),
                plan_table.c.session_id.in_(session_ids),
                or_(*clauses),
            )
        )
        for row in session.execute(statement).mappings():
            headers[row["id"]] = (
                row["plan_id"],
                row["parent_revision_id"],
                row["revision_number"],
            )

    if not active_ids <= headers.keys() or not dedicated_ids <= headers.keys():
        raise _error("copy_graph_invalid")
    pair_headers = {(plan_id, number) for plan_id, _parent, number in headers.values()}
    if any(pair not in pair_headers for pair in media_role_pair_refs):
        raise _error("copy_graph_invalid")

    selected_ids = set(active_ids) | set(dedicated_ids)
    selected_ids.update(generic_ids & headers.keys())
    selected_ids.update(
        revision_id
        for revision_id, (plan_id, _parent, number) in headers.items()
        if (plan_id, number) in pair_refs
    )
    if len(selected_ids) > MAX_MEDIA_EDIT_REVISIONS:
        raise _error("copy_limit_exceeded")

    uncached_ids = selected_ids - revision_row_cache.keys()
    if uncached_ids:
        fetched_rows: dict[str, dict[str, Any]] = {}
        with session.no_autoflush:
            for chunk in _chunks(uncached_ids):
                statement = (
                    select(revision_table)
                    .select_from(join)
                    .where(
                        revision_table.c.id.in_(chunk),
                        revision_table.c.plan_id.in_(plan_ids),
                        plan_table.c.session_id.in_(session_ids),
                    )
                )
                fetched_rows.update(
                    {
                        row["id"]: {key: row[key] for key in MEDIA_REVISION_FIELDS}
                        for row in session.execute(statement).mappings()
                    }
                )
        if set(fetched_rows) != uncached_ids:
            raise _error("copy_graph_invalid")
        revision_row_cache.update(fetched_rows)
    rows = [revision_row_cache[revision_id] for revision_id in selected_ids]
    if {row["id"] for row in rows} != selected_ids or any(
        (row["plan_id"], row["parent_revision_id"], row["revision_number"]) != headers[row["id"]]
        for row in rows
    ):
        raise _error("copy_graph_invalid")
    total_payload_rows = 0
    try:
        for row in rows:
            total_payload_rows += validate_media_revision(row, frozen=False)
    except (TypeError, ValueError, KeyError) as error:
        raise _error("copy_graph_invalid") from error
    if total_payload_rows > MAX_MEDIA_PAYLOAD_ROWS:
        raise _error("copy_limit_exceeded")
    dependency_ids = {
        artifact_id
        for row in rows
        for artifact_id in (
            row["source_media_artifact_id"],
            row["editorial_transcript_artifact_id"],
            row["timing_artifact_id"],
        )
        if artifact_id is not None
    }
    return rows, selected_ids, dependency_ids


def _branch_source_sessions(
    groups: list[dict[str, Any]], branches: list[dict[str, Any]]
) -> dict[str, str]:
    if not groups and branches or len(groups) > 1:
        raise _error("copy_graph_invalid")
    if not groups:
        return {}
    return {row["session_id"]: groups[0]["source_session_id"] for row in branches}


def _evidence_headers(session: Session, evidence_ids: set[str], session_ids: set[str]):
    """Read only bounded ownership/status headers before any evidence bodies."""
    result: dict[str, tuple[str, str]] = {}
    for chunk in _chunks(evidence_ids):
        result.update(
            {
                row.id: (row.session_id, row.status)
                for row in session.execute(
                    select(
                        m.SubtitleEvidence.id,
                        m.SubtitleEvidence.session_id,
                        m.SubtitleEvidence.status,
                    ).where(
                        m.SubtitleEvidence.id.in_(chunk),
                        m.SubtitleEvidence.session_id.in_(session_ids),
                    )
                )
            }
        )
    return result


def _selected_evidence(
    session: Session,
    evidence_ids: set[str],
    session_ids: set[str],
    headers: Mapping[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    result = []
    for chunk in _chunks(evidence_ids):
        result.extend(
            _rows(
                session,
                m.SubtitleEvidence,
                and_(
                    m.SubtitleEvidence.id.in_(chunk),
                    m.SubtitleEvidence.session_id.in_(session_ids),
                ),
            )
        )
    if {row["id"] for row in result} != evidence_ids or any(
        (row["session_id"], row["status"]) != headers.get(row["id"]) for row in result
    ):
        raise _error("copy_graph_invalid")
    return result



def _speech_selection(session: Session, ids: set[str]):
    """Classify bounded current headers before loading any speech JSON body."""
    headers = [dict(row) for row in session.execute(select(
        m.GenerationPlan.id, m.GenerationPlan.session_id, m.GenerationPlan.active_revision_id,
    ).where(m.GenerationPlan.session_id.in_(ids)).limit(MAX_SESSIONS + 1)).mappings()]
    if len(headers) > MAX_SESSIONS:
        raise _error("copy_limit_exceeded")
    if not headers:
        return [], {}, 0, [], {}
    active_ids = [row["active_revision_id"] for row in headers if row["active_revision_id"] is not None]
    if len(active_ids) != len(set(active_ids)):
        raise _error("copy_graph_invalid")
    by_revision = {row["active_revision_id"]: row for row in headers if row["active_revision_id"] is not None}
    revision_headers = list(session.execute(select(
        m.GenerationPlanRevision.id, m.GenerationPlanRevision.plan_id,
        m.GenerationPlanRevision.source_revision_id,
        m.Document.session_id.label("source_session_id"),
    ).outerjoin(m.DocumentRevision, m.DocumentRevision.id == m.GenerationPlanRevision.source_revision_id)
      .outerjoin(m.Document, m.Document.id == m.DocumentRevision.document_id)
      .where(m.GenerationPlanRevision.id.in_(by_revision)))) if by_revision else []
    if {row.id for row in revision_headers} != set(by_revision) or any(
        row.plan_id != by_revision[row.id]["id"] or
        row.source_revision_id is not None and row.source_session_id != by_revision[row.id]["session_id"]
        for row in revision_headers
    ):
        raise _error("copy_graph_invalid")
    segment_headers = list(session.execute(select(
        m.GenerationSegment.id, m.GenerationSegment.plan_revision_id,
        m.GenerationSegment.voice, m.GenerationSegment.voice_id,
    ).where(m.GenerationSegment.plan_revision_id.in_(by_revision)).limit(MAX_EDITORIAL_ROWS + 1))) if by_revision else []
    if len(segment_headers) > MAX_EDITORIAL_ROWS:
        raise _error("copy_limit_exceeded")
    reference = m.ProjectSpeechReference
    reference_headers = list(session.execute(select(
        reference.session_id, reference.slot_key, reference.input_artifact_id,
    ).join(m.GenerationSegment, reference.slot_key == literal("segment:") + m.GenerationSegment.id)
      .join(m.GenerationPlanRevision, m.GenerationPlanRevision.id == m.GenerationSegment.plan_revision_id)
      .join(m.GenerationPlan, m.GenerationPlan.id == m.GenerationPlanRevision.plan_id)
      .where(m.GenerationSegment.plan_revision_id.in_(by_revision),
             m.GenerationPlan.session_id == reference.session_id,
             reference.session_id.in_(ids)).limit(MAX_EDITORIAL_ROWS + 1))) if by_revision else []
    if len(reference_headers) > MAX_EDITORIAL_ROWS:
        raise _error("copy_limit_exceeded")
    saved = {(row.session_id, row.slot_key): row.input_artifact_id for row in reference_headers}
    voiced = {row.plan_revision_id for row in segment_headers
              if (row.voice not in (None, "") or row.voice_id not in (None, ""))
              and (by_revision[row.plan_revision_id]["session_id"], f"segment:{row.id}") not in saved}
    try:
        take_headers = selected_take_headers(session, set(by_revision), MAX_EDITORIAL_ROWS)
    except OverflowError as error:
        raise _error("copy_limit_exceeded") from error
    except (ValueError, TypeError, KeyError) as error:
        raise _error("copy_graph_invalid") from error
    unsupported_audio = {row["_revision_id"] for row in take_headers if not supported_take_header(row)}
    selected = [row for row in headers if row["active_revision_id"] is not None
                and row["active_revision_id"] not in voiced | unsupported_audio]
    reserved = {row["active_revision_id"]: 2 for row in selected}
    for header in segment_headers:
        if header.plan_revision_id in reserved:
            reserved[header.plan_revision_id] += 1
    for take in take_headers:
        if take["_revision_id"] in reserved:
            reserved[take["_revision_id"]] += 1
    if sum(reserved.values()) > MAX_EDITORIAL_ROWS:
        raise _error("copy_limit_exceeded")
    selected_sessions = {row["session_id"] for row in selected}
    saved = {key: pin for key, pin in saved.items() if key[0] in selected_sessions}
    return selected, reserved, len(headers) - len(selected), take_headers, saved


def capture_current_project(
    session: Session,
    context: AccessContext,
    source_project_id: str,
    destination_workspace_id: str,
    *,
    name: str | None = None,
    settings_service: CopySettingsReader,
    destination_connections: Any = None,
) -> ProjectCopySnapshot:
    """Read one caller-owned snapshot. Its data never confers copy permission."""
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    with session.no_autoflush:
        access = require_project_access(session, context, source_project_id, ProjectAction.EDIT)
        require_project_creation(session, context, destination_workspace_id)
        project = _rows(session, Project, Project.id == source_project_id)[0]
        source_id = project["source_session_id"]
        session_count = (
            session.scalar(
                select(func.count())
                .select_from(m.SessionRecord)
                .where(m.SessionRecord.project_id == source_project_id)
            )
            or 0
        )
        if session_count > MAX_SESSIONS:
            raise _error("copy_limit_exceeded")
        sessions = _rows(session, m.SessionRecord, m.SessionRecord.project_id == source_project_id)
        active = [row for row in sessions if row["trashed_at"] is None]
        source = next((row for row in active if row["id"] == source_id), None)
        if source is None:
            raise _error("copy_source_unavailable")
        if len(active) > MAX_SESSIONS:
            raise _error("copy_limit_exceeded")
        ids = {row["id"] for row in active}
        blockers: set[tuple[str, str]] = set()
        if any(row["workflow_kind"] not in {"subtitles", "media_edit", "voiceover"} for row in active):
            blockers.add(("unsupported_state", "workflow_kind"))
        if any(row["trashed_at"] is not None and row["target_language"] for row in sessions):
            blockers.add(("unsupported_state", "trashed_language_versions"))
        groups = _rows(
            session, m.TranslationProject, m.TranslationProject.project_root_id == source_project_id
        )
        if len(groups) > 1 or any(row["source_session_id"] != source_id for row in groups):
            raise _error("copy_graph_invalid")
        branches = []
        if groups:
            all_branches = _rows(
                session,
                m.TranslationProjectBranch,
                m.TranslationProjectBranch.project_id == groups[0]["id"],
            )
            all_session_ids = {row["id"] for row in sessions}
            if any(row["session_id"] not in all_session_ids for row in all_branches):
                raise _error("copy_graph_invalid")
            branches = [row for row in all_branches if row["session_id"] in ids]
            if len(branches) != len(all_branches):
                blockers.add(("unsupported_state", "trashed_language_versions"))
        (speech_plans, speech_reservations, uncaptured_plans,
         active_take_headers, segment_headers) = _speech_selection(session, ids)
        media_plans = _media_plans(session, ids)
        chosen_name = (groups[0]["name"] if groups else source["name"]) if name is None else name
        if (
            not isinstance(chosen_name, str)
            or not chosen_name.strip()
            or len(chosen_name.strip()) > 255
        ):
            raise ValueError("Copy name must contain between 1 and 255 characters.")
        chosen_name = chosen_name.strip()
        selections = _select_chunks(
            session, m.SessionStageSelection, m.SessionStageSelection.session_id, ids
        )
        attachments = [
            row
            for row in _select_chunks(session, m.SessionSource, m.SessionSource.session_id, ids)
            if row["is_current"]
        ]
        sources = _select_chunks(session, m.SourceRecord, m.SourceRecord.session_id, ids)
        knowledge_ledgers = _knowledge_ledgers(session, ids)
        representations = {
            (sid, section): deepcopy(
                settings_service.get_in_session(session, sid, section)["effective"]
            )
            for sid in sorted(ids)
            for section in SETTING_SECTIONS
        }
        controls = [row for row in knowledge_ledgers if row["kind"] == "generation_controls"]
        copied_cast: dict[str, set[str]] = {}
        cast_bindings = {}
        cast_headers = {}
        omitted_controls = 0
        omitted_control_sessions: set[str] = set()
        if controls:
            headers = list(session.execute(select(
                m.ProjectSpeechReference.session_id,
                m.ProjectSpeechReference.slot_key,
                m.ProjectSpeechReference.input_artifact_id,
            ).where(m.ProjectSpeechReference.session_id.in_(ids)).limit(MAX_ARTIFACTS + 1)))
            if len(headers) > MAX_ARTIFACTS:
                raise _error("copy_limit_exceeded")
            header_map = {(row.session_id, row.slot_key): row.input_artifact_id for row in headers}
            tts_headers = {sid: pin for (sid, slot), pin in header_map.items()
                           if slot == "tts"}
            for ledger in controls:
                sid = ledger["session_id"]
                bindings_for_session = dict(iter_cast_voice_bindings(
                    CastSettings.model_validate(ledger["payload_json"]["cast"])))
                if any((sid, slot) not in header_map or
                       _selected_settings(representations[(sid, "tts")], binding) is None
                       for slot, binding in bindings_for_session.items()):
                    knowledge_ledgers.remove(ledger)
                    omitted_controls += 1
                    omitted_control_sessions.add(sid)
                    blockers.add(("unsupported_state", "knowledge_ledgers"))
                    continue
                if bindings_for_session:
                    copied_cast[sid] = set(bindings_for_session)
                for slot, binding in bindings_for_session.items():
                    cast_bindings[(sid, slot)] = binding
                    cast_headers[(sid, slot)] = header_map[(sid, slot)]
        else:
            tts_headers = {
                row.session_id: row.input_artifact_id
                for chunk in _chunks(ids)
                for row in session.execute(
                    select(
                        m.ProjectSpeechReference.session_id, m.ProjectSpeechReference.input_artifact_id
                    ).where(
                        m.ProjectSpeechReference.session_id.in_(chunk),
                        m.ProjectSpeechReference.slot_key == "tts",
                    )
                )
            }

        excluded_speech = [row for row in speech_plans if row["session_id"] in omitted_control_sessions]
        uncaptured_plans += len(excluded_speech)
        speech_plans = [row for row in speech_plans if row["session_id"] not in omitted_control_sessions]
        selected_speech_ids = {row["active_revision_id"] for row in speech_plans}
        selected_speech_sessions = {row["session_id"] for row in speech_plans}
        segment_headers = {key: pin for key, pin in segment_headers.items()
                           if key[0] in selected_speech_sessions}
        candidate_take_headers = [row for row in active_take_headers if row["_revision_id"] in selected_speech_ids]
        speech_reserved = sum(speech_reservations[revision_id] for revision_id in selected_speech_ids)
        speech_revisions = [
            {key: row[key] for key in SPEECH_REVISION_FIELDS}
            for row in _select_chunks(session, m.GenerationPlanRevision,
                                      m.GenerationPlanRevision.id, selected_speech_ids)
        ]
        if {row["id"] for row in speech_revisions} != selected_speech_ids:
            raise _error("copy_graph_invalid")
        sound_rows, sound_artifact_ids, uncaptured_sounds = capture_current_sounds(
            session, source_project_id=source_project_id, workspace_id=access.workspace_id,
            session_workflows={row["id"]: row["workflow_kind"] for row in active},
            speech_revisions={row["session_id"]: row["active_revision_id"] for row in speech_plans},
            max_rows=MAX_EDITORIAL_ROWS - speech_reserved, max_artifacts=MAX_ARTIFACTS,
        )
        sound_row_count = sum(len(values) for values in sound_rows.values())
        if COPY_STATE_VERSION < 14:
            uncaptured_sounds += len(sound_rows["project_sound_plans"])
        if uncaptured_sounds:
            blockers.add(("unsupported_state", "project_sound_plans"))
        asset_ids = {row["source_asset_id"] for row in attachments}
        assets = _select_chunks(session, m.SourceAsset, m.SourceAsset.id, asset_ids)
        if {row["id"] for row in assets} != asset_ids or any(
            row["workspace_id"] != access.workspace_id or row["state"] == "deleted"
            for row in assets
        ):
            raise _error("copy_graph_invalid")
        seeds = set(
            session.scalars(
                select(m.Artifact.id).where(
                    m.Artifact.session_id.in_(ids),
                    m.Artifact.state == "current",
                    m.Artifact.role.not_in(
                        ("subtitle_evidence_audio", "subtitle_evidence_transcript", "generation_take")
                    ),
                )
            )
        )
        seeds.update(row["artifact_id"] for row in selections if row["artifact_id"])
        seeds.update(row["checkpoint_artifact_id"] for row in groups)
        seeds.update(row["source_checkpoint_artifact_id"] for row in branches)
        seeds.update(row["artifact_id"] for row in [*assets, *sources] if row["artifact_id"])
        seeds.update(pin for pin in tts_headers.values() if pin is not None)
        seeds.update(pin for pin in cast_headers.values() if pin is not None)
        seeds.update(pin for pin in segment_headers.values() if pin is not None)
        selected_audio_roots = {row["artifact_id"] for row in candidate_take_headers}
        seeds.update(selected_audio_roots)
        seeds.update(sound_artifact_ids)
        for revision in speech_revisions:
            value = revision["settings_json"]
            if type(value) is not dict:
                raise _error("copy_graph_invalid")
            source_artifact = value.get("_source_artifact_id")
            if source_artifact is not None:
                if type(source_artifact) is not str or not source_artifact:
                    raise _error("copy_graph_invalid")
                seeds.add(source_artifact)
        if any(not row["artifact_id"] for row in [*assets, *sources]):
            blockers.add(("unsupported_state", "external_source"))
        additional_revision_ids: set[str] = {row["source_revision_id"] for row in speech_revisions
                                             if row["source_revision_id"] is not None}
        evidence_rows: dict[str, dict[str, Any]] = {}
        active_evidence_reference = False
        converged = False
        artifacts: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        document_rows: dict[str, list[dict[str, Any]]] = {}
        media_revision_rows: list[dict[str, Any]] = []
        media_revision_ids: set[str] = set()
        previous_media_revision_ids: set[str] = set()
        media_revision_row_cache: dict[str, dict[str, Any]] = {}
        branch_source_sessions = _branch_source_sessions(groups, branches)
        for _pass in range(MAX_EVIDENCE_CLOSURE_PASSES):
            artifacts, edges = _artifact_closure(
                session, context, seeds, sound_artifact_ids=sound_artifact_ids,
                source_project_id=source_project_id,
            )
            media_revision_rows, media_revision_ids, media_dependencies = _media_selection(
                session, ids, media_plans, artifacts, groups, media_revision_row_cache
            )
            added_media_revisions = media_revision_ids - previous_media_revision_ids
            previous_media_revision_ids = set(media_revision_ids)
            added_media_dependencies = media_dependencies - seeds
            seeds.update(media_dependencies)
            document_rows = _documents(
                session,
                ids,
                artifacts,
                additional_revision_ids=additional_revision_ids,
                media_revision_ids=media_revision_ids,
                reserved_editorial_rows=speech_reserved + sound_row_count,
            )
            try:
                documents_by_id = {row["id"]: row for row in document_rows["documents"]}
                revision_sessions = {
                    row["id"]: documents_by_id[row["document_id"]]["session_id"]
                    for row in document_rows["document_revisions"]
                }
                referenced = set(
                    referenced_evidence_ids(document_rows["segments"], artifacts.values())
                )
                reference_sessions = evidence_reference_sessions(
                    document_rows["segments"], artifacts.values(), revision_sessions
                )
            except ValueError as error:
                raise _error("copy_graph_invalid") from error
            if len(referenced) > MAX_SUBTITLE_EVIDENCE_ROWS:
                raise _error("copy_limit_exceeded")
            new_evidence_ids = referenced - evidence_rows.keys()
            if not new_evidence_ids:
                if added_media_revisions or added_media_dependencies:
                    continue
                converged = True
                break
            headers = _evidence_headers(session, new_evidence_ids, ids)
            if set(headers) != new_evidence_ids:
                raise _error("copy_graph_invalid")
            if any(reference_sessions.get(item) != {headers[item][0]} for item in new_evidence_ids):
                raise _error("copy_graph_invalid")
            if any(status in ACTIVE_EVIDENCE_STATUSES for _sid, status in headers.values()):
                blockers.add(("unsupported_state", "active_evidence"))
                active_evidence_reference = True
                converged = True
                break
            if any(status not in EVIDENCE_STATUSES for _sid, status in headers.values()):
                raise _error("copy_graph_invalid")
            selected = _selected_evidence(session, new_evidence_ids, ids, headers)
            try:
                captured = [sanitize_evidence(row) for row in selected]
                evidence_artifact_ids, revision_ids = evidence_dependencies(captured)
            except ValueError as error:
                raise _error("copy_graph_invalid") from error
            evidence_rows.update({row["id"]: row for row in captured})
            added_seeds = evidence_artifact_ids - seeds
            added_revisions = revision_ids - additional_revision_ids
            seeds.update(evidence_artifact_ids)
            additional_revision_ids.update(revision_ids)
            if added_seeds or added_revisions or added_media_revisions or added_media_dependencies:
                continue
            converged = True
            break
        if not converged:
            raise _error("copy_limit_exceeded")
        if active_evidence_reference:
            # Active rows are represented by a fixed blocker only; their bodies
            # and dependencies were never selected.
            pass
        else:
            try:
                # Recheck all reference IDs after the last bounded closure pass;
                # this also prevents an extra, unreferenced row being serialized.
                documents_by_id = {row["id"]: row for row in document_rows["documents"]}
                revision_sessions = {
                    row["id"]: documents_by_id[row["document_id"]]["session_id"]
                    for row in document_rows["document_revisions"]
                }
                referenced = set(
                    referenced_evidence_ids(document_rows["segments"], artifacts.values())
                )
                reference_sessions = evidence_reference_sessions(
                    document_rows["segments"], artifacts.values(), revision_sessions
                )
            except ValueError as error:
                raise _error("copy_graph_invalid") from error
            if referenced != set(evidence_rows) or any(
                reference_sessions.get(item) != {evidence_rows[item]["session_id"]}
                for item in referenced
            ):
                raise _error("copy_graph_invalid")
        try:
            retained_evidence_artifact_ids, _ = evidence_dependencies(evidence_rows.values())
        except ValueError as error:
            raise _error("copy_graph_invalid") from error
        sanitize_evidence_artifact_metadata(artifacts.values(), retained_evidence_artifact_ids)
        for row in selections:
            if (
                row["artifact_id"]
                and artifacts[row["artifact_id"]]["session_id"] != row["session_id"]
            ):
                raise _error("copy_graph_invalid")
        for row in groups:
            pinned = artifacts[row["checkpoint_artifact_id"]]
            if (
                pinned["session_id"] != source_id
                or pinned["content_hash"] != row["source_content_hash"]
            ):
                raise _error("copy_graph_invalid")
        for row in branches:
            pinned = artifacts[row["source_checkpoint_artifact_id"]]
            if (
                pinned["session_id"] != row["session_id"]
                or pinned["content_hash"] != row["source_content_hash"]
            ):
                raise _error("copy_graph_invalid")
        for row in artifacts.values():
            if row["session_id"] in ids and (
                row["project_id"] != source_project_id or row["workspace_id"] != access.workspace_id
            ):
                raise _error("copy_graph_invalid")
            path = row["relative_path"]
            if (
                not path
                or PurePosixPath(path).is_absolute()
                or PureWindowsPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                or ".." in PureWindowsPath(path).parts
            ):
                blockers.add(("unsupported_state", "external_source"))
            if not row["content_hash"]:
                blockers.add(("unsupported_state", "unhashed_file"))
        rows = {
            "projects": [_content(project, omit=("revision",))],
            "sessions": [
                _content(
                    row,
                    omit=(
                        "storage_key",
                        "legacy_name",
                        "legacy_path",
                        "status",
                        "revision",
                        "trashed_at",
                        "purge_after",
                    ),
                )
                for row in active
            ],
            "translation_projects": [_content(row, omit=("revision",)) for row in groups],
            "translation_project_branches": branches,
            "session_stage_selections": selections,
            "session_sources": attachments,
            "source_assets": assets,
            "sources": sources,
            "artifacts": list(artifacts.values()),
            "artifact_edges": edges,
            "outcome_plans": _select_chunks(session, m.OutcomePlan, m.OutcomePlan.session_id, ids),
            "knowledge_ledgers": knowledge_ledgers,
            "subtitle_evidence": list(evidence_rows.values()),
            "project_speech_references": [],
        }
        rows.update(document_rows)
        rows.update(sound_rows)
        source_media_rows = {
            **rows,
            "media_edit_plans": media_plans,
            "media_edit_plan_revisions": media_revision_rows,
            "translation_projects": groups,
            "translation_project_branches": branches,
            "artifacts": list(artifacts.values()),
        }
        try:
            validate_media_graph(
                source_media_rows,
                branch_source_sessions=branch_source_sessions,
                document_revision_ids={row["id"] for row in document_rows["document_revisions"]},
                source=True,
            )
        except (TypeError, ValueError, KeyError) as error:
            raise _error("copy_graph_invalid") from error
        rows["media_edit_plans"] = media_plans
        try:
            rows["media_edit_plan_revisions"] = [
                freeze_revision(row, media_revision_ids) for row in media_revision_rows
            ]
        except (TypeError, ValueError, KeyError) as error:
            raise _error("copy_graph_invalid") from error
        rows["artifacts"] = [_content(row) for row in artifacts.values()]
        rows["translation_project_branches"] = [_content(row) for row in branches]
        rebind_capture_metadata(rows)
        try:
            validate_media_graph(
                rows,
                branch_source_sessions=branch_source_sessions,
                document_revision_ids={row["id"] for row in document_rows["document_revisions"]},
            )
        except (TypeError, ValueError, KeyError) as error:
            raise _error("copy_graph_invalid") from error
        rows.update({key: [] for key in SPEECH_FAMILIES})
        rows["generation_plans"] = [{key: row[key] for key in SPEECH_PLAN_FIELDS} for row in speech_plans]
        rows["generation_plan_revisions"] = speech_revisions
        speech_revision_ids = {row["id"] for row in speech_revisions}
        editorial = 0
        if speech_revision_ids:
            rows["speech_plan_reviews"] = [
                {key: row[key] for key in SPEECH_REVIEW_FIELDS}
                for row in _select_chunks(session, m.SpeechPlanReview, m.SpeechPlanReview.revision_id,
                                          speech_revision_ids)
            ]
            editorial = sound_row_count + sum(len(document_rows[key]) for key in
                            ("documents", "document_revisions", "segments", "timed_words"))
            if editorial + speech_reserved + len(rows["speech_plan_reviews"]) > MAX_EDITORIAL_ROWS:
                raise _error("copy_limit_exceeded")
            rows["generation_segments"] = [
                {key: row[key] for key in SPEECH_SEGMENT_FIELDS}
                for row in _select_chunks(session, m.GenerationSegment, m.GenerationSegment.plan_revision_id,
                                          speech_revision_ids)
            ]
        rows["audio_takes"] = []
        copied_audio_ids: set[str] = set()
        if candidate_take_headers:
            try:
                copied_takes, recorded_evidence, unsupported_revisions = capture_audio_evidence(
                    session, candidate_take_headers, artifacts, rows["generation_segments"], speech_plans,
                    MAX_EDITORIAL_ROWS - editorial - speech_reserved + len(candidate_take_headers),
                    {sid: SpeechInputOwner(access.workspace_id, source_project_id, sid) for sid in ids},
                    allow_saved_selections=COPY_STATE_VERSION >= 13,
                )
            except OverflowError as error:
                raise _error("copy_limit_exceeded") from error
            except (ValueError, TypeError, KeyError) as error:
                raise _error("copy_graph_invalid") from error
            if unsupported_revisions:
                uncaptured_plans += sum(row["active_revision_id"] in unsupported_revisions for row in speech_plans)
                speech_plans = [row for row in speech_plans if row["active_revision_id"] not in unsupported_revisions]
                speech_revisions = [row for row in speech_revisions if row["id"] not in unsupported_revisions]
                rows["generation_plans"] = [row for row in rows["generation_plans"] if row["active_revision_id"] not in unsupported_revisions]
                rows["generation_plan_revisions"] = speech_revisions
                rows["generation_segments"] = [row for row in rows["generation_segments"] if row["plan_revision_id"] not in unsupported_revisions]
                rows["speech_plan_reviews"] = [row for row in rows["speech_plan_reviews"] if row["revision_id"] not in unsupported_revisions]
            retained_segments = {row["id"] for row in rows["generation_segments"]}
            rows["audio_takes"] = [row for row in copied_takes if row["generation_segment_id"] in retained_segments]
            copied_audio_ids = {row["artifact_id"] for row in rows["audio_takes"]}
            # Prune only in memory from the single authorized closure/body cache.
            roots = (seeds - selected_audio_roots) | copied_audio_ids
            parents = {}
            for edge in edges:
                parents.setdefault(edge["child_artifact_id"], set()).add(edge["parent_artifact_id"])
            reachable = set(roots)
            pending = list(roots)
            while pending:
                current = pending.pop()
                for parent in parents.get(current, set()) - reachable:
                    reachable.add(parent)
                    pending.append(parent)
            artifacts = {key: value for key, value in artifacts.items() if key in reachable}
            edges = [row for row in edges if row["parent_artifact_id"] in artifacts and row["child_artifact_id"] in artifacts]
            rows["artifacts"] = [row for row in rows["artifacts"] if row["id"] in artifacts]
            rows["artifact_edges"] = edges
            for artifact in rows["artifacts"]:
                if artifact["role"] != "generation_take":
                    continue
                raw_metadata = artifacts[artifact["id"]]["metadata_json"]
                metadata = {key: deepcopy(value) for key, value in raw_metadata.items()
                            if key not in {"generation_segment_id", "generation_task_run_id", "generation_run_id", PROVENANCE_KEY}}
                if artifact["id"] in copied_audio_ids:
                    value = recorded_evidence[artifact["id"]]
                    value["segments"] = {key: snapshot for key, snapshot in value["segments"].items() if key in retained_segments}
                    metadata = audio_metadata(raw_metadata, next(row["optimized_text"] or row["text"] for row in rows["generation_segments"]
                        if row["id"] in value["segments"]))
                    metadata[PROVENANCE_KEY] = value
                artifact["metadata_json"] = metadata
        for artifact in rows["artifacts"]:
            if artifact["role"] == "generation_take" and artifact["id"] not in copied_audio_ids:
                artifact["metadata_json"] = {key: value for key, value in artifact["metadata_json"].items()
                    if key not in {"generation_segment_id", "generation_task_run_id", "generation_run_id", PROVENANCE_KEY}}
        unsupported_audio_count = len(active_take_headers) - len(rows["audio_takes"])
        current_segment_slots = {f"segment:{row['id']}" for row in rows["generation_segments"]}
        segment_headers = {key: pin for key, pin in segment_headers.items()
                           if key[1] in current_segment_slots}
        raw_segment_references = [
            _content(row)
            for chunk in _chunks(key[1] for key in segment_headers)
            for row in _rows(session, m.ProjectSpeechReference, and_(
                m.ProjectSpeechReference.session_id.in_(ids),
                m.ProjectSpeechReference.slot_key.in_(chunk),
            ))
        ]
        if {(row["session_id"], row["slot_key"]) for row in raw_segment_references} != set(segment_headers):
            raise _error("copy_graph_invalid")
        for row in raw_segment_references:
            key = (row["session_id"], row["slot_key"])
            try:
                reference = freeze_segment_reference(
                    row, representations[(key[0], "tts")],
                    binding=VoiceBinding.model_validate(row["selector_json"]),
                    owner=SpeechInputOwner(access.workspace_id, source_project_id, key[0]),
                    artifacts=artifacts, edges=edges, allow_null_inputs=True,
                )
            except (TypeError, ValueError, KeyError) as error:
                raise _error("copy_graph_invalid") from error
            if reference["input_artifact_id"] != segment_headers[key]:
                raise _error("copy_graph_invalid")
            rows["project_speech_references"].append(reference)
        base_ids = {row["id"] for values in rows.values() for row in values if "id" in row}
        raw_settings = _select_chunks(session, m.SessionSetting, m.SessionSetting.session_id, ids)
        raw_tts_references = {
            row["session_id"]: _content(row)
            for chunk in _chunks(tts_headers)
            for row in _rows(
                session,
                m.ProjectSpeechReference,
                and_(
                    m.ProjectSpeechReference.session_id.in_(chunk),
                    m.ProjectSpeechReference.slot_key == "tts",
                ),
            )
        }
        if set(raw_tts_references) != set(tts_headers):
            raise _error("copy_graph_invalid")
        raw_cast_references = [
            _content(row)
            for chunk in _chunks(copied_cast)
            for row in _rows(session, m.ProjectSpeechReference, or_(*(
                and_(m.ProjectSpeechReference.session_id == sid,
                     m.ProjectSpeechReference.slot_key.in_(copied_cast[sid]))
                for sid in chunk if copied_cast[sid]
            )))
        ] if cast_headers else []
        if {(row["session_id"], row["slot_key"]) for row in raw_cast_references} != set(cast_headers):
            raise _error("copy_graph_invalid")
        for row in raw_cast_references:
            key = (row["session_id"], row["slot_key"])
            try:
                reference = freeze_cast_reference(
                    row, representations[(key[0], "tts")], binding=cast_bindings[key],
                    owner=SpeechInputOwner(access.workspace_id, source_project_id, key[0]),
                    artifacts=artifacts, edges=edges,
                    allow_null_inputs=True,
                )
            except (TypeError, ValueError, KeyError) as error:
                raise _error("copy_graph_invalid") from error
            if reference["input_artifact_id"] != cast_headers[key]:
                raise _error("copy_graph_invalid")
            rows["project_speech_references"].append(reference)
        connection_rows = {
            **rows,
            "session_settings": [
                {"session_id": sid, "section": section, "value_json": value}
                for (sid, section), value in representations.items()
            ],
        }
        bindings = collect_requirements(connection_rows, blockers)
        settings_rows, binding_fields = [], {}
        speech_settings = {}
        for revision in speech_revisions:
            removed_speech: dict[str, int] = {}
            speech_settings[revision["id"]] = _settings(
                editorial_revision_settings(revision["settings_json"]), {}, "speech_plan", base_ids, blockers, removed_speech,
            )
            if removed_speech:
                binding_fields[f"speech_plan:{revision['id']}"] = removed_speech
        try:
            rows.update(freeze_speech_plan_rows(rows, speech_settings, allow_segment_references=True))
            validate_speech_plan_graph(rows, allow_segment_references=True)
            validate_audio_copy_graph(rows, allow_saved_selections=COPY_STATE_VERSION >= 13)
            validate_output_copy_graph(rows, allow_sound_mix=True)
            validate_sound_copy_graph(rows, allow_sounds=True)
            reserved = sum(len(rows[key]) for key in (
                "documents", "document_revisions", "segments", "timed_words",
                *SPEECH_FAMILIES, "audio_takes", *SOUND_FAMILIES))
            performance, uncaptured_performance = capture_current_performance(
                session, rows=rows, max_rows=MAX_EDITORIAL_ROWS - reserved,
                allow_performance=COPY_STATE_VERSION >= 15,
            )
            rows["performance_plans"] = performance
            validate_performance_copy_graph(rows, allow_performance=COPY_STATE_VERSION >= 15)
        except (TypeError, ValueError, KeyError) as error:
            raise _error("copy_graph_invalid") from error
        for sid in sorted(ids):
            for section in SETTING_SECTIONS:
                effective_settings = representations[(sid, section)]
                removed: dict[str, int] = {}
                retained_voice_id = None
                if section == "tts" and sid in raw_tts_references:
                    try:
                        reference = freeze_tts_reference(
                            raw_tts_references[sid],
                            effective_settings,
                            owner=SpeechInputOwner(access.workspace_id, source_project_id, sid),
                            artifacts=artifacts,
                            edges=edges,
                        )
                    except (TypeError, ValueError, KeyError) as error:
                        raise _error("copy_graph_invalid") from error
                    if reference["input_artifact_id"] != tts_headers[sid]:
                        raise _error("copy_graph_invalid")
                    rows["project_speech_references"].append(reference)
                    retained_voice_id = reference["selector_json"]["voice_id"]
                effective = _settings(
                    effective_settings,
                    BUILTIN_DEFAULTS[section],
                    section,
                    base_ids,
                    blockers,
                    removed,
                    _retained_voice_id=retained_voice_id,
                )
                settings_rows.append(
                    {"session_id": sid, "section": section, "value_json": effective}
                )
                if removed:
                    binding_fields[f"{sid}:{section}"] = removed
        for row in raw_settings:
            if row["section"] == "multilingual_setup":
                removed = {}
                value = _settings(
                    row["value_json"], {}, "multilingual_setup", base_ids, blockers, removed
                )
                settings_rows.append(
                    {
                        "session_id": row["session_id"],
                        "section": "multilingual_setup",
                        "value_json": value,
                    }
                )
                if removed:
                    binding_fields[f"{row['session_id']}:multilingual_setup"] = removed
            elif row["section"] not in SETTING_SECTIONS and row["value_json"]:
                blockers.add(("unsupported_state", "custom_settings"))
        rows["session_settings"] = settings_rows
        options = bind_destination_connections(
            session,
            rows,
            destination_workspace_id,
            bindings,
            destination_connections,
            blockers,
        )
        validate_connection_graph(rows, bindings, ready=not blockers)
        if not blockers:
            try:
                validate_speech_copy_graph(rows, allow_null_inputs=True, allow_segment_references=True)
            except (TypeError, ValueError, KeyError) as error:
                raise _error("copy_graph_invalid") from error
        if not active_evidence_reference:
            try:
                validate_evidence_graph(rows)
            except (TypeError, ValueError) as error:
                raise _error("copy_graph_invalid") from error
        content_rows = {
            key: sorted(
                (_content(row) for row in values),
                key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False),
            )
            for key, values in rows.items()
            if (COPY_STATE_VERSION >= 14 or key not in SOUND_FAMILIES)
                and (COPY_STATE_VERSION >= 15 or key not in PERFORMANCE_FAMILIES)
        }
        state = {
            "schema_version": COPY_STATE_VERSION,
            "destination_connections": bindings,
            "rows": content_rows,
            "binding_fields": binding_fields,
            "unsupported_counts": _unsupported(session, ids, blockers, copied_cast, uncaptured_plans),
        }
        if unsupported_audio_count:
            state["unsupported_counts"]["audio_takes"] = unsupported_audio_count
            blockers.add(("unsupported_state", "audio_takes"))
        if uncaptured_sounds:
            state["unsupported_counts"]["project_sound_plans"] = uncaptured_sounds
        if uncaptured_performance:
            state["unsupported_counts"]["performance_plans"] = uncaptured_performance
            blockers.add(("unsupported_state", "performance_plans"))
        if omitted_controls:
            state["unsupported_counts"]["knowledge_ledgers"] = (
                state["unsupported_counts"].get("knowledge_ledgers", 0) + omitted_controls
            )
        validate_connection_state(state, content_rows, ready=not blockers)
        try:
            state_json = json.dumps(
                state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError, UnicodeError) as error:
            raise _error("copy_graph_invalid") from error
        active_jobs = (
            session.scalar(
                select(func.count())
                .select_from(m.Job)
                .where(
                    m.Job.project_id == source_project_id,
                    m.Job.status.in_(("queued", "running", "retrying", "cancel_requested")),
                )
            )
            or 0
        )
        files = tuple(
            CopyFileSource(
                row["id"],
                row["workspace_id"],
                row["project_id"],
                row["session_id"],
                row["relative_path"],
                row["content_hash"],
                row["size_bytes"],
            )
            for row in sorted(artifacts.values(), key=lambda row: row["id"])
        )
        return ProjectCopySnapshot(
            source_project_id,
            destination_workspace_id,
            chosen_name,
            state_json,
            hashlib.sha256(state_json.encode("utf-8")).hexdigest(),
            files,
            tuple(sorted(blockers)),
            active_jobs,
            json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
