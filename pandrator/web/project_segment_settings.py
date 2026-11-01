"""Stage segment voice inputs outside the atomic editor/copy/receipt writer."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.speech_performance import resolve_capabilities
from pandrator.runtime import DataPaths

from .access.generation_resource_policy import require_generation_content_write
from .access.voice_policy import require_current_voice_library_read
from .auth import Principal
from .database import Database
from .idempotency import IdempotencyService
from .models import (
    ApiIdempotency,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    ProjectSpeechReference,
    Voice,
)
from .project_speech_reference_capture import (
    ProjectSpeechReferenceCapture,
    capture_project_speech_reference,
    prepare_project_speech_reference,
    publish_project_speech_reference,
)
from .project_speech_references import (
    StoredProjectSpeechReference,
    clear_project_speech_reference,
    get_project_speech_reference,
    list_project_speech_references,
    normalize_speech_reference_slot,
    put_project_speech_reference,
    speech_reference_selector,
    speech_reference_selector_hash,
)
from .projects.identity import session_project
from .schemas import GenerationSegmentUpdate
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
from .speech_plan_workspace import segment_edit_fork_source
from .workspace_settings import WorkspaceSettingsService


@dataclass(frozen=True, slots=True)
class ProjectSegmentSettingsWrite:
    value: dict[str, Any]
    status_code: int = 200
    replayed: bool = False


def _slot(identifier: str) -> str | None:
    try:
        return normalize_speech_reference_slot("segment:" + identifier)
    except ValueError:
        return None


def _rows(
    session: Session, session_id: str, identifiers: list[str]
) -> dict[str, GenerationSegment]:
    result = {}
    with session.no_autoflush:
        for start in range(0, len(identifiers), 500):
            result.update(
                {
                    row.id: row
                    for row in session.scalars(
                        select(GenerationSegment)
                        .join(
                            GenerationPlanRevision,
                            GenerationPlanRevision.id == GenerationSegment.plan_revision_id,
                        )
                        .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
                        .where(
                            GenerationSegment.id.in_(identifiers[start : start + 500]),
                            GenerationPlan.session_id == session_id,
                        )
                        .execution_options(populate_existing=True)
                    )
                }
            )
    if len(result) != len(identifiers):
        raise KeyError(next(identifier for identifier in identifiers if identifier not in result))
    return result


def _row_state(rows: Mapping[str, GenerationSegment]) -> str:
    return canonical_json(
        {
            identifier: {
                column.name: value.isoformat()
                if isinstance(value := getattr(row, column.name), datetime)
                else value
                for column in GenerationSegment.__table__.columns
            }
            for identifier, row in rows.items()
        }
    )


def _plan_state(session: Session, rows: Mapping[str, GenerationSegment]) -> str:
    revisions = {row.plan_revision_id for row in rows.values()}
    with session.no_autoflush:
        values = session.execute(
            select(GenerationPlan.id, GenerationPlan.active_revision_id)
            .join(GenerationPlanRevision, GenerationPlanRevision.plan_id == GenerationPlan.id)
            .where(GenerationPlanRevision.id.in_(revisions))
        ).all()
    return canonical_json({row.id: row.active_revision_id for row in values})


def _reference_value(stored: StoredProjectSpeechReference | None) -> object:
    if stored is None:
        return None
    return {
        "owner": [stored.owner.workspace_id, stored.owner.project_id, stored.owner.session_id],
        "slot": stored.slot_key,
        "selector": stored.selector,
        "hash": stored.selector_hash,
        "binding": stored.binding,
        "pin": stored.pin,
        "revision": stored.revision,
    }


def _references(
    session: Session, session_id: str, identifiers: list[str]
) -> dict[str, StoredProjectSpeechReference]:
    def guard(unit: Session) -> None:
        require_generation_content_write(unit, "session", session_id)

    slots = [key for identifier in identifiers if (key := _slot(identifier)) is not None]
    return {
        row.slot_key: row
        for row in list_project_speech_references(
            session,
            session_id,
            slot_keys=slots,
            authorization_guard=guard,
        )
    }


def _reference_state(values: Mapping[str, StoredProjectSpeechReference]) -> str:
    return canonical_json({key: _reference_value(value) for key, value in values.items()})


def _project_saved_selections(
    session: Session, session_id: str, results: list[dict[str, Any]],
) -> None:
    """Read public fields after the same writer's content and segment proof."""
    from .project_segment_speech import retained_voice_metadata_projection

    by_slot = {key: result for result in results if (key := _slot(result["id"])) is not None}
    slots = list(by_slot)
    with session.no_autoflush:
        for start in range(0, len(slots), 500):
            rows = session.execute(select(
                ProjectSpeechReference.slot_key,
                ProjectSpeechReference.selector_json,
                ProjectSpeechReference.binding_json,
            ).where(
                ProjectSpeechReference.session_id == session_id,
                ProjectSpeechReference.slot_key.in_(slots[start:start + 500]),
            ))
            for row in rows:
                by_slot[row.slot_key]["retained_voice"] = retained_voice_metadata_projection(
                    row.selector_json, row.binding_json,
                )


def _fork_state(
    session: Session, rows: dict[str, GenerationSegment], updates: list[dict[str, Any]]
) -> str:
    source = segment_edit_fork_source(session, rows, updates)
    return canonical_json([source[0].id, source[1].id] if source is not None else None)


def _validate_updates(
    updates: list[dict[str, Any]], *, allow_empty: bool = False
) -> list[dict[str, Any]]:
    if not updates:
        raise ValueError("At least one generation segment update is required.")
    result, seen = [], set()
    for item in updates:
        identifier = str(item.get("id") or "").strip()
        if not identifier or identifier in seen:
            raise ValueError("Every segment update requires one distinct ID.")
        seen.add(identifier)
        revision = item.get("revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("A segment revision must be a nonnegative integer.")
        changes = GenerationSegmentUpdate.model_validate(item.get("changes") or {}).model_dump(
            exclude_unset=True
        )
        if not changes and not allow_empty:
            raise ValueError("Every generation segment update requires at least one change.")
        if any(
            value is None and name not in {"optimized_text", "voice_id", "voice", "language"}
            for name, value in changes.items()
        ):
            raise ValueError("This generation segment field cannot be null.")
        if "text" in changes:
            changes["text"] = str(changes["text"]).strip()
            if not changes["text"]:
                raise ValueError("Generation text cannot be blank; remove the segment instead.")
        if "voice_id" in changes and changes["voice_id"] is not None:
            identifier_value = changes["voice_id"]
            if not identifier_value or identifier_value != identifier_value.strip():
                raise ValueError(
                    "Managed voice IDs must be nonblank and have no surrounding whitespace."
                )
        result.append({"id": identifier, "revision": revision, "changes": changes})
    return result


@dataclass(frozen=True, slots=True)
class _Decision:
    original_id: str
    action: str
    capture: ProjectSpeechReferenceCapture | None = None


@dataclass(frozen=True, slots=True)
class PreparedSegmentSpeechWrite:
    """Frozen original-row correspondence; caller owns transaction and cleanup."""

    database: Database = field(repr=False)
    paths: DataPaths = field(repr=False)
    workspace_settings: WorkspaceSettingsService = field(repr=False)
    session_id: str
    updates_json: str = field(repr=False)
    rows_json: str = field(repr=False)
    plans_json: str = field(repr=False)
    fork_json: str = field(repr=False)
    tts_json: str = field(repr=False)
    references_json: str = field(repr=False)
    decisions: tuple[_Decision, ...]
    preparations: tuple[tuple[SpeechInputCapture, PreparedSpeechInput], ...] = field(
        default=(), repr=False
    )

    @property
    def updates(self) -> list[dict[str, Any]]:
        return json.loads(self.updates_json)

    @property
    def handled_ids(self) -> frozenset[str]:
        return frozenset(item.original_id for item in self.decisions if item.action != "untouched")

    def guard(self, session: Session) -> None:
        require_generation_content_write(session, "session", self.session_id)

    def source_guard(self, session: Session) -> None:
        self.guard(session)
        require_current_voice_library_read(
            session, session_project(session, self.session_id).workspace_id
        )

    def validate_updates(self, session: Session, updates: list[dict[str, Any]]) -> None:
        self.guard(session)
        if canonical_json(updates) != self.updates_json:
            raise ValueError("Prepared segment speech write does not match these updates.")
        decisions = {item.original_id: item for item in self.decisions}
        if len(decisions) != len(self.decisions) or set(decisions) != {
            item["id"] for item in updates
        }:
            raise ValueError("Prepared segment decisions do not match their source updates.")
        for update in updates:
            decision = decisions[update["id"]]
            touched = bool({"voice_id", "voice"}.intersection(update["changes"]))
            if (
                decision.action not in {"untouched", "capture", "clear", "keep", "legacy"}
                or (decision.action == "untouched") == touched
                or (decision.action == "capture") != (decision.capture is not None)
            ):
                raise ValueError("Prepared segment decision does not match its physical update.")
        rows = _rows(session, self.session_id, [item["id"] for item in updates])
        if _row_state(rows) != self.rows_json or _plan_state(session, rows) != self.plans_json:
            raise RevisionConflict(
                "The segment rows or active plan changed while preparing speech inputs."
            )
        if _fork_state(session, rows, updates) != self.fork_json:
            raise RevisionConflict(
                "The speech-plan edit-copy decision changed while preparing inputs."
            )
        settings, _digest = self.workspace_settings.resolve_in_session(
            session, self.session_id, ["tts"]
        )
        if canonical_json(settings["tts"]) != self.tts_json:
            raise RevisionConflict(
                "Project speech settings changed while preparing segment inputs."
            )
        if (
            _reference_state(_references(session, self.session_id, list(rows)))
            != self.references_json
        ):
            raise RevisionConflict("Saved segment selections changed while preparing inputs.")

    def publish_for_targets(
        self,
        session: Session,
        targets_by_original_id: Mapping[str, GenerationSegment],
    ) -> dict[str, StoredProjectSpeechReference]:
        self.guard(session)
        if set(targets_by_original_id) != {item["id"] for item in self.updates}:
            raise ValueError("Prepared segment speech targets do not match their original sources.")
        expected = json.loads(self.references_json)
        prepared = dict(self.preparations)
        published: dict[SpeechInputCapture, StoredProjectSpeechReference] = {}
        result = {}
        for decision in self.decisions:
            original = decision.original_id
            target = targets_by_original_id[original]
            target_slot, source_slot = _slot(target.id), _slot(original)
            if target_slot is None:
                continue
            current = get_project_speech_reference(
                session, self.session_id, target_slot, authorization_guard=self.guard
            )
            old = expected.get(source_slot)
            if target.id == original:
                matches = _reference_value(current) == old
            elif old is None:
                matches = current is None
            else:
                actual = _reference_value(current)
                matches = isinstance(actual, dict) and actual == {
                    **old,
                    "slot": target_slot,
                    "revision": 1,
                }
            if not matches:
                raise RevisionConflict("The copied target speech selection changed before editing.")
            if decision.action in {"clear", "legacy"}:
                if current is not None:
                    clear_project_speech_reference(
                        session,
                        self.session_id,
                        target_slot,
                        expected_revision=current.revision,
                        authorization_guard=self.guard,
                    )
                continue
            captured = decision.capture
            if captured is not None:
                original_current = get_project_speech_reference(
                    session, self.session_id, captured.slot_key, authorization_guard=self.guard
                )
                if _reference_value(original_current) != expected.get(captured.slot_key):
                    raise RevisionConflict(
                        "The original segment selection changed before publication."
                    )
                core = captured.core_capture
                if core is not None and core.requires_files and core in published:
                    self.source_guard(session)
                    current = put_project_speech_reference(
                        session,
                        self.session_id,
                        target_slot,
                        selector=captured.selector,
                        binding=captured.binding,
                        pin=published[core].pin,
                        expected_revision=current.revision if current is not None else 0,
                        authorization_guard=self.guard,
                    )
                else:
                    kwargs = (
                        {}
                        if target.id == original
                        else {
                            "target_slot_key": target_slot,
                            "target_expected_revision": current.revision
                            if current is not None
                            else 0,
                        }
                    )
                    current = publish_project_speech_reference(
                        session,
                        self.paths,
                        captured,
                        prepared.get(core) if core is not None else None,
                        settings=captured.settings,
                        authorization_guard=self.guard,
                        source_authorization_guard=self.source_guard,
                        **kwargs,
                    )
                    if core is not None and core.requires_files:
                        published[core] = current
            if current is not None:
                result[original] = current
        return result


def _prepare_write(
    session: Session,
    database: Database,
    paths: DataPaths,
    workspace_settings: WorkspaceSettingsService,
    session_id: str,
    updates: list[dict[str, Any]],
    *,
    allow_empty: bool = False,
) -> PreparedSegmentSpeechWrite:
    updates = _validate_updates(updates, allow_empty=allow_empty)
    rows = _rows(session, session_id, [item["id"] for item in updates])
    for item in updates:
        if rows[item["id"]].revision != item["revision"]:
            raise RevisionConflict(
                "One or more generation segments changed in another client; no replacements were applied."
            )
    fork = _fork_state(session, rows, updates)
    resolved, _digest = workspace_settings.resolve_in_session(session, session_id, ["tts"])
    tts = resolved["tts"]
    references = _references(session, session_id, list(rows))
    reference_state = _reference_state(references)
    adapter, _aliases = speech_input_adapter(tts)
    capabilities = resolve_capabilities(tts) if adapter == "audio_cpp" else {}
    model = str(capabilities.get("model") or tts.get("model") or "").strip()
    active = (
        adapter == "audio_cpp"
        and bool(model)
        and capabilities.get("voice_mode") not in {"prebuilt", "design"}
    )
    decisions, managed_ids = [], set()

    def guard(unit: Session) -> None:
        require_generation_content_write(unit, "session", session_id)

    def source_guard(unit: Session) -> None:
        guard(unit)
        require_current_voice_library_read(unit, session_project(unit, session_id).workspace_id)

    for item in updates:
        identifier, changes = item["id"], item["changes"]
        physical = rows[identifier]
        if not {"voice_id", "voice"}.intersection(changes):
            decisions.append(_Decision(identifier, "untouched"))
            continue
        voice_id = changes.get("voice_id", physical.voice_id)
        voice = str(changes.get("voice", physical.voice) or "").strip()
        selection = (
            speech_reference_selector({"voice_id": voice_id} if voice_id else {"voice": voice})
            if voice_id or voice
            else None
        )
        source_slot = _slot(identifier)
        old = references.get(source_slot) if source_slot is not None else None
        exact = (
            old is not None
            and selection is not None
            and old.selector_hash == speech_reference_selector_hash(selection)
        )
        captured = None
        if selection is None:
            action = "clear"
        elif active and source_slot is not None:
            selected = deepcopy(tts)
            selected.update(
                model=model,
                xtts_model=model,
                voice=voice if not voice_id else "",
                speaker=voice if not voice_id else "",
            )
            for key in (
                "audio_cpp_voice_ref",
                "audio_cpp_voice_ref_hash",
                "audio_cpp_reference_text",
                PROJECT_SPEECH_INPUT_KEY,
                PROJECT_SPEECH_BINDING_KEY,
                PROJECT_SPEECH_SELECTOR_KEY,
            ):
                selected.pop(key, None)
            captured = capture_project_speech_reference(
                session,
                paths,
                session_id,
                source_slot,
                selector=selection,
                settings=selected,
                authorization_guard=guard,
                source_authorization_guard=source_guard,
            )
            action = "capture"
        elif exact:
            action = "keep"
        else:
            action = "clear" if source_slot is not None else "legacy"
            if voice_id:
                managed_ids.add(voice_id)
        reused = captured is not None and captured.reusable is not None
        if (
            voice_id
            and physical.voice_id is None
            and exact
            and (reused or action == "keep")
            and "voice_id" in changes
        ):
            changes["voice_id"] = None
        decisions.append(_Decision(identifier, action, captured))
    if managed_ids:
        source_guard(session)
        workspace_id = session_project(session, session_id).workspace_id
        found = set()
        identifiers = sorted(managed_ids)
        with session.no_autoflush:
            for start in range(0, len(identifiers), 500):
                found.update(
                    session.scalars(
                        select(Voice.id).where(
                            Voice.id.in_(identifiers[start : start + 500]),
                            Voice.workspace_id == workspace_id,
                        )
                    )
                )
        if found != managed_ids:
            raise ValueError("Managed voice is unavailable in this project.")
    return PreparedSegmentSpeechWrite(
        database,
        paths,
        workspace_settings,
        session_id,
        canonical_json(updates),
        _row_state(rows),
        _plan_state(session, rows),
        fork,
        canonical_json(tts),
        reference_state,
        tuple(decisions),
    )


def _write(
    database: Database,
    paths: DataPaths,
    workspace_settings: WorkspaceSettingsService,
    generation: Any,
    *,
    session_id: str | None,
    segment_id: str | None,
    updates: list[dict[str, Any]],
    payload: dict[str, Any],
    idempotency: IdempotencyService | None,
    principal: Principal | None,
    idempotency_key: str | None,
) -> ProjectSegmentSettingsWrite:
    if idempotency_key is not None and (idempotency is None or principal is None):
        raise TypeError("A retry key requires its idempotency service and principal.")
    single = segment_id is not None
    operation = "updateGenerationSegment" if single else "updateGenerationSegments"
    kind = "generation_segment" if single else "generation_segment_batch"

    def guard(session: Session) -> str:
        identifier = segment_id if single else session_id
        assert identifier is not None
        return require_generation_content_write(
            session, "segment" if single else "session", identifier
        )

    def replay(session: Session) -> ProjectSegmentSettingsWrite | None:
        if idempotency_key is None:
            return None
        assert idempotency is not None and principal is not None
        key = idempotency.validate_key(idempotency_key)
        with session.no_autoflush:
            header = session.execute(
                select(ApiIdempotency.resource_kind, ApiIdempotency.resource_id).where(
                    ApiIdempotency.principal_subject == principal.subject,
                    ApiIdempotency.operation_id == operation,
                    ApiIdempotency.idempotency_key == key,
                )
            ).one_or_none()
            if header is not None and (
                header.resource_kind is not None or header.resource_id is not None
            ):
                if header.resource_kind != kind or type(header.resource_id) is not str:
                    raise KeyError("Generation retry resource is unavailable.")
                require_generation_content_write(
                    session, "segment" if single else "session", header.resource_id
                )
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
        return ProjectSegmentSettingsWrite(value, status, True)

    preparations: dict[SpeechInputCapture, PreparedSpeechInput] = {}
    adopted = False

    def finish(
        session: Session, proved_session: str, plan: PreparedSegmentSpeechWrite | None,
        mutations: list[dict[str, Any]],
    ) -> ProjectSegmentSettingsWrite:
        # Both callers prove current access and replay inside this writer first.
        # The core independently validates a prepared packet before copying.
        reservation = None
        if idempotency_key is not None:
            assert idempotency is not None and principal is not None
            reservation = idempotency.begin(
                session,
                principal=principal,
                operation_id=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if reservation.response is not None:
                value, status = reservation.response
                return ProjectSegmentSettingsWrite(value, status, True)
        if single:
            item = mutations[0]
            value = generation.update_segment_in_session(
                session, item["id"], item["revision"], item["changes"], _speech_write=plan
            )
        else:
            value = generation.update_segments_in_session(
                session, proved_session, mutations, _speech_write=plan
            )
        if plan is None:
            _project_saved_selections(session, proved_session, [value] if single else value["items"])
        if reservation is not None:
            assert idempotency is not None
            idempotency.complete(
                session,
                reservation,
                response=value,
                status_code=200,
                resource_kind=kind,
                resource_id=segment_id if single else proved_session,
            )
        return ProjectSegmentSettingsWrite(value)

    try:
        with database.immediate_session() as session:
            proved_session = guard(session)
            previous = replay(session)
            if previous is not None:
                return previous
            selection_changes = [dict(item.get("changes") or {}) for item in updates]
            requires_selection = any(
                {"voice", "voice_id"}.intersection(changes)
                and not (
                    "voice" in changes and changes["voice"] is None
                    and "voice_id" in changes and changes["voice_id"] is None
                )
                for changes in selection_changes
            )
            if not requires_selection:
                mutations = _validate_updates(updates, allow_empty=single)
                return finish(session, proved_session, None, mutations)
            plan = _prepare_write(
                session,
                database,
                paths,
                workspace_settings,
                proved_session,
                updates,
                allow_empty=single,
            )
            if not any(
                d.capture is not None
                and d.capture.core_capture is not None
                and d.capture.core_capture.requires_files
                for d in plan.decisions
            ):
                return finish(session, proved_session, plan, plan.updates)

        def before_io() -> None:
            with database.snapshot_session() as session:
                plan.source_guard(session)
                plan.validate_updates(session, plan.updates)

        for decision in plan.decisions:
            captured = decision.capture
            core = captured.core_capture if captured is not None else None
            if (
                captured is not None
                and core is not None
                and core.requires_files
                and core not in preparations
            ):
                prepared = prepare_project_speech_reference(
                    database, paths, captured, before_io=before_io
                )
                assert prepared is not None
                preparations[core] = prepared
        plan = replace(plan, preparations=tuple(preparations.items()))
        with database.immediate_session() as session:
            proved_session = guard(session)
            previous = replay(session)
            if previous is not None:
                return previous
            result = finish(session, proved_session, plan, plan.updates)
        adopted = not result.replayed
        return result
    finally:
        if not adopted:
            for prepared in preparations.values():
                cleanup_unpublished_speech_input(database, prepared)


def write_project_segment_settings(
    database: Database,
    paths: DataPaths,
    workspace_settings: WorkspaceSettingsService,
    generation: Any,
    *,
    segment_id: str,
    expected_revision: int,
    changes: dict[str, Any],
    idempotency: IdempotencyService | None,
    principal: Principal | None,
    idempotency_key: str | None = None,
) -> ProjectSegmentSettingsWrite:
    return _write(
        database,
        paths,
        workspace_settings,
        generation,
        session_id=None,
        segment_id=segment_id,
        updates=[{"id": segment_id, "revision": expected_revision, "changes": changes}],
        payload={
            "segment_id": segment_id,
            "expected_revision": expected_revision,
            "changes": changes,
        },
        idempotency=idempotency,
        principal=principal,
        idempotency_key=idempotency_key,
    )


def write_project_segments_settings(
    database: Database,
    paths: DataPaths,
    workspace_settings: WorkspaceSettingsService,
    generation: Any,
    *,
    session_id: str,
    updates: list[dict[str, Any]],
    idempotency: IdempotencyService | None,
    principal: Principal | None,
    idempotency_key: str | None = None,
) -> ProjectSegmentSettingsWrite:
    return _write(
        database,
        paths,
        workspace_settings,
        generation,
        session_id=session_id,
        segment_id=None,
        updates=updates,
        payload={"session_id": session_id, "updates": updates},
        idempotency=idempotency,
        principal=principal,
        idempotency_key=idempotency_key,
    )
