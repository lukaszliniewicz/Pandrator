"""Bounded metadata topology copy and original-slot-preserving publication."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select
from sqlalchemy.orm import Session

from pandrator.web import project_speech_reference_capture as capture_module
from pandrator.web import project_speech_references as store
from pandrator.web import speech_inputs
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.models import (
    AppSetting,
    Artifact,
    ArtifactEdge,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    SessionRecord,
    Voice,
    VoiceSample,
    Workspace,
)
from pandrator.web.project_speech_reference_capture import (
    ProjectSpeechReferenceCapture,
    capture_project_speech_reference,
    prepare_project_speech_reference,
    publish_project_speech_reference,
)
from pandrator.web.project_speech_references import (
    clone_segment_speech_references,
    get_project_speech_reference,
    list_project_speech_references,
    put_project_speech_reference,
    segment_speech_reference_identity,
    speech_reference_selector,
    speech_reference_selector_hash,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.speech_reference_models import ProjectSpeechReference
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.speech_input_contracts import (
    SpeechInputError,
    SpeechInputOwner,
)
from tests.test_retained_speech_inputs import Harness
from tests.test_retained_speech_inputs import harness as harness


def allow(session: Session) -> None:
    assert not session.autoflush


def forbidden(*_args: Any, **_kwargs: Any):
    raise AssertionError("Metadata association crossed a graph/library/file boundary")


@dataclass
class Segments:
    h: Harness
    ids: list[str]
    foreign: str
    revision: str

    @property
    def session_id(self) -> str:
        assert self.h.owner.session_id is not None
        return self.h.owner.session_id


@pytest.fixture
def segments(harness: Harness) -> Segments:
    h = harness
    with h.database.immediate_session() as session:
        record = SessionRecord(name="Segment references", workflow_kind="audiobook")
        root = create_session_project(session, record, workspace_id=h.owner.workspace_id)
        h.owner = SpeechInputOwner(h.owner.workspace_id, root.id, record.id)
        plan = GenerationPlan(session_id=record.id)
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(
            plan_id=plan.id, revision_number=1, content_hash="synthetic"
        )
        session.add(revision)
        session.flush()
        rows = [
            GenerationSegment(plan_revision_id=revision.id, ordinal=n, text=f"Segment {n}")
            for n in range(12)
        ]
        session.add_all(rows)
        other = SessionRecord(name="Foreign segments", workflow_kind="audiobook")
        create_session_project(session, other, workspace_id=h.other.workspace_id)
        other_plan = GenerationPlan(session_id=other.id)
        session.add(other_plan)
        session.flush()
        other_revision = GenerationPlanRevision(
            plan_id=other_plan.id, revision_number=1, content_hash="other"
        )
        session.add(other_revision)
        session.flush()
        foreign = GenerationSegment(plan_revision_id=other_revision.id, ordinal=0, text="Private")
        session.add(foreign)
        session.flush()
        result = Segments(h, [row.id for row in rows], foreign.id, revision.id)
    return result


def slot(identifier: str) -> str:
    return "segment:" + identifier


def capture(s: Segments, index=0, *, settings=None, selector=None, **kwargs):
    with s.h.database.snapshot_session() as session:
        return capture_project_speech_reference(
            session,
            s.h.paths,
            s.session_id,
            slot(s.ids[index]),
            settings=s.h.settings if settings is None else settings,
            selector={"voice_id": s.h.voice_id} if selector is None else selector,
            authorization_guard=allow,
            source_authorization_guard=kwargs.pop("source_authorization_guard", allow),
            **kwargs,
        )


def prepare(s: Segments, captured: ProjectSpeechReferenceCapture):
    return prepare_project_speech_reference(
        s.h.database, s.h.paths, captured, before_io=lambda: None
    )


def publish(s: Segments, captured: ProjectSpeechReferenceCapture, prepared=None, **kwargs):
    with s.h.database.immediate_session() as session:
        return publish_project_speech_reference(
            session,
            s.h.paths,
            captured,
            prepared,
            settings=kwargs.pop("settings", captured.settings),
            authorization_guard=kwargs.pop("authorization_guard", allow),
            source_authorization_guard=kwargs.pop("source_authorization_guard", allow),
            **kwargs,
        )


def save(s: Segments, index=0, **kwargs):
    captured = capture(s, index, **kwargs)
    prepared = prepare(s, captured)
    return publish(s, captured, prepared)


def row(s: Segments, identifier: str):
    with s.h.database.snapshot_session() as session:
        return get_project_speech_reference(
            session, s.session_id, slot(identifier), authorization_guard=allow
        )


def clone(s: Segments, mapping):
    with s.h.database.immediate_session() as session:
        return clone_segment_speech_references(
            session, s.session_id, mapping, authorization_guard=allow
        )


def counts(s: Segments):
    with s.h.database.snapshot_session() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Artifact, ArtifactEdge)
        )


def test_split_metadata_copy_and_exact_noop_preserve_source_and_files_without_graph_io(
    segments, monkeypatch
):
    s = segments
    source = save(s)
    before = counts(s)
    statements = []
    event.listen(
        s.h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    monkeypatch.setattr(store, "check_speech_input", forbidden)
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    result = clone(s, {s.ids[0]: s.ids[1:3]})
    assert set(result) == set(s.ids[1:3])
    assert all(
        value.pin == source.pin and value.revision == 1 and value.owner == source.owner
        for value in result.values()
    )
    assert counts(s) == before and row(s, s.ids[0]) == source
    assert clone(s, {s.ids[0]: [s.ids[0]]}) == {s.ids[0]: source}
    assert not any(
        "artifacts.relative_path" in sql
        or "voices.metadata_json" in sql
        or "voice_samples.transcript" in sql
        for sql in statements
    )
    assert (
        sum("artifacts.mime_type" in sql and "artifacts.content_hash" in sql for sql in statements)
        == 2
    )
    result[s.ids[1]].binding["voice"] = "mutated"
    stored_target = row(s, s.ids[1])
    assert stored_target is not None and stored_target.binding == source.binding


def test_same_selection_merge_chooses_stable_source_and_ignores_label_revision_and_artifact_uuid(
    segments,
):
    s = segments
    first = save(s)
    with s.h.database.immediate_session() as session:
        second = put_project_speech_reference(
            session,
            s.session_id,
            slot(s.ids[1]),
            selector=first.selector,
            binding={**first.binding, "display_name": "Another label"},
            pin=first.pin,
            expected_revision=0,
            authorization_guard=allow,
        )
    assert segment_speech_reference_identity(first) == segment_speech_reference_identity(second)
    assert first.pin is not None
    changed_uuid = replace(first, pin={**first.pin, "artifact_id": str(uuid4())}, revision=20)
    assert segment_speech_reference_identity(first) == segment_speech_reference_identity(
        changed_uuid
    )
    result = clone(s, {s.ids[1]: [s.ids[2]], s.ids[0]: [s.ids[2]]})
    chosen = first if s.ids[0] < s.ids[1] else second
    assert result[s.ids[2]].binding == chosen.binding and result[s.ids[2]].pin == chosen.pin
    assert segment_speech_reference_identity(None) == ("inherit",)
    assert clone(s, {s.ids[3]: [s.ids[5]], s.ids[4]: [s.ids[5]]}) == {}


@pytest.mark.parametrize("difference", ["missing", "pin", "selector", "null"])
def test_mixed_or_different_choices_deny_all_targets_before_insert(segments, difference):
    s = segments
    first = save(s)
    if difference == "pin":
        with s.h.database.immediate_session() as session:
            session.get_one(VoiceSample, s.h.sample_id).transcript = "Different retained content"
        save(s, 1)
    elif difference in {"selector", "null"}:
        with s.h.database.immediate_session() as session:
            put_project_speech_reference(
                session,
                s.session_id,
                slot(s.ids[1]),
                selector={"voice": "changed"} if difference == "selector" else first.selector,
                binding=first.binding,
                pin=first.pin if difference == "selector" else None,
                expected_revision=0,
                authorization_guard=allow,
            )
    with pytest.raises(ValueError, match="These segments use different saved voice selections"):
        clone(s, {s.ids[0]: [s.ids[2], s.ids[3]], s.ids[1]: [s.ids[3]]})
    assert row(s, s.ids[2]) is None and row(s, s.ids[3]) is None


@pytest.mark.parametrize(
    "invalid",
    ["foreign_source", "foreign_target", "missing_source", "missing_target", "pending_target"],
)
def test_segment_header_ownership_denies_before_reference_bodies(segments, invalid):
    s = segments
    save(s)
    mapping = {s.ids[0]: [s.ids[1]]}
    if invalid == "foreign_source":
        mapping = {s.foreign: [s.ids[1]]}
    elif invalid == "foreign_target":
        mapping = {s.ids[0]: [s.foreign]}
    elif invalid == "missing_source":
        mapping = {str(uuid4()): [s.ids[1]]}
    elif invalid == "missing_target":
        mapping = {s.ids[0]: [str(uuid4())]}
    statements = []
    event.listen(
        s.h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    with s.h.database.immediate_session() as session:
        target = None
        if invalid == "pending_target":
            target = GenerationSegment(
                id=str(uuid4()), plan_revision_id=s.revision, ordinal=30, text="Unflushed"
            )
            session.add(target)
            mapping = {s.ids[0]: [target.id]}
        with pytest.raises(
            ValueError, match="The segment voice selection does not belong to this project"
        ):
            clone_segment_speech_references(
                session, s.session_id, mapping, authorization_guard=allow
            )
        if target is not None:
            session.expunge(target)
    assert not any("FROM project_speech_references" in sql for sql in statements)
    assert not any("generation_segments.text" in sql for sql in statements)


@pytest.mark.parametrize("mutation", ["foreign", "hash", "role", "mime", "kind", "missing"])
def test_envelope_header_mismatch_rejects_without_body_reads_or_inserts(segments, mutation):
    s = segments
    source = save(s)
    assert source.pin is not None
    with s.h.database.immediate_session() as session:
        header = session.get_one(Artifact, source.pin["artifact_id"])
        if mutation == "foreign":
            record = SessionRecord(name="Foreign envelope", workflow_kind="audiobook")
            root = create_session_project(session, record, workspace_id=s.h.other.workspace_id)
            other = Artifact(
                workspace_id=s.h.other.workspace_id,
                project_id=root.id,
                session_id=record.id,
                kind=header.kind,
                role=header.role,
                mime_type=header.mime_type,
                content_hash=header.content_hash,
                relative_path="artifacts/foreign-envelope.json",
            )
            session.add(other)
            session.flush()
            session.get_one(
                ProjectSpeechReference, (s.session_id, slot(s.ids[0]))
            ).input_artifact_id = other.id
        elif mutation == "missing":
            pass
        else:
            setattr(
                header,
                {"hash": "content_hash", "role": "role", "mime": "mime_type", "kind": "kind"}[
                    mutation
                ],
                "0" * 64 if mutation == "hash" else "wrong",
            )
    if mutation == "missing":
        # Model an already corrupt missing header in this disposable DB only.
        with sqlite3.connect(s.h.database.path) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DELETE FROM artifacts WHERE id=?", (source.pin["artifact_id"],))
    statements = []
    event.listen(
        s.h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    with pytest.raises(ValueError, match="saved segment speech input is unavailable"):
        clone(s, {s.ids[0]: [s.ids[1]]})
    assert row(s, s.ids[1]) is None
    assert not any("artifacts.relative_path" in sql for sql in statements)


def test_retired_header_copies_association_without_reviving_inference_or_ordinary_put(
    segments, monkeypatch
):
    s = segments
    source = save(s)
    assert source.pin is not None
    with s.h.database.immediate_session() as session:
        session.get_one(Artifact, source.pin["artifact_id"]).state = "deleted"
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    assert clone(s, {s.ids[0]: [s.ids[1]]})[s.ids[1]].pin == source.pin
    with s.h.database.immediate_session() as session:
        with pytest.raises(SpeechInputError):
            speech_inputs.check_speech_input(session, source.pin, owner=s.h.owner)
        with pytest.raises(SpeechInputError):
            put_project_speech_reference(
                session,
                s.session_id,
                slot(s.ids[2]),
                selector=source.selector,
                binding=source.binding,
                pin=source.pin,
                expected_revision=0,
                authorization_guard=allow,
            )
    assert row(s, s.ids[0]) == source


def test_guard_first_and_copy_does_not_flush_unrelated_pending_rows(segments):
    s = segments
    save(s)
    statements = []
    event.listen(
        s.h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    with s.h.database.immediate_session() as session:
        pending = AppSetting(key="unrelated-pending", value_json={})
        session.add(pending)

        def deny(unit):
            assert not unit.autoflush
            raise PermissionError("Denied before headers")

        statements.clear()
        with pytest.raises(PermissionError, match="Denied before headers"):
            clone_segment_speech_references(
                session, s.session_id, {s.ids[0]: [s.ids[1]]}, authorization_guard=deny
            )
        assert statements == []
        clone_segment_speech_references(
            session, s.session_id, {s.ids[0]: [s.ids[1]]}, authorization_guard=allow
        )
        assert pending in session.new and not any(
            "INSERT INTO app_settings" in sql for sql in statements
        )
        session.expunge(pending)


@pytest.mark.parametrize("inactive", ["workspace", "session"])
def test_inactive_owner_cannot_copy(segments, inactive):
    s = segments
    with s.h.database.immediate_session() as session:
        if inactive == "workspace":
            session.get_one(Workspace, s.h.owner.workspace_id).state = "archived"
        else:
            session.get_one(SessionRecord, s.session_id).trashed_at = session.get_one(
                Voice, s.h.voice_id
            ).created_at
    with pytest.raises(AccessDenied):
        clone(s, {s.ids[0]: [s.ids[1]]})


@pytest.mark.parametrize("invalid", ["string", "duplicate", "upper", "overlap", "map", "uuid"])
def test_malformed_mapping_and_target_conflicts(segments, invalid):
    s = segments
    mapping: Any = {s.ids[0]: [s.ids[1]]}
    if invalid == "string":
        mapping[s.ids[0]] = s.ids[1]
    elif invalid == "duplicate":
        mapping[s.ids[0]] = [s.ids[1], s.ids[1]]
    elif invalid == "upper":
        mapping = {s.ids[0].upper(): [s.ids[1]]}
    elif invalid == "overlap":
        mapping = {s.ids[0]: [s.ids[1]], s.ids[1]: [s.ids[2]]}
    elif invalid == "map":
        mapping = []
    else:
        mapping = {"bad": [s.ids[1]]}
    with pytest.raises(ValueError):
        clone(s, mapping)


def test_existing_target_conflict_and_caller_rollback_preserve_all_metadata(segments):
    s = segments
    source = save(s)
    clone(s, {s.ids[0]: [s.ids[1]]})
    with pytest.raises(RevisionConflict):
        clone(s, {s.ids[0]: [s.ids[2], s.ids[1]]})
    assert row(s, s.ids[2]) is None
    with pytest.raises(RuntimeError):
        with s.h.database.immediate_session() as session:
            clone_segment_speech_references(
                session, s.session_id, {s.ids[0]: [s.ids[2]]}, authorization_guard=allow
            )
            raise RuntimeError("Caller rollback")
    assert row(s, s.ids[2]) is None and row(s, s.ids[0]) == source


def test_scoped_list_empty_and_chunked_ordering_are_metadata_only(segments):
    s = segments
    selector = speech_reference_selector({"voice": "builtin"})
    slots = [slot(str(uuid4())) for _ in range(1003)]
    with s.h.database.immediate_session() as session:
        session.execute(
            insert(ProjectSpeechReference),
            [
                {
                    "session_id": s.session_id,
                    "slot_key": key,
                    "selector_json": selector,
                    "selector_hash": speech_reference_selector_hash(selector),
                    "binding_json": {
                        "adapter": "audio_cpp",
                        "model": "synthetic",
                        "voice": "builtin",
                        "display_name": "Builtin",
                    },
                    "input_artifact_id": None,
                    "input_content_hash": None,
                    "revision": 1,
                }
                for key in slots
            ],
        )
    queries = []
    event.listen(
        s.h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, parameters, _co, _e: queries.append((statement, parameters)),
    )
    with s.h.database.snapshot_session() as session:
        assert (
            list_project_speech_references(
                session, s.session_id, slot_keys=[], authorization_guard=allow
            )
            == []
        )
        assert not any("FROM project_speech_references" in sql for sql, _ in queries)
        rows = list_project_speech_references(
            session,
            s.session_id,
            slot_keys=[*reversed(slots), *slots[:3]],
            authorization_guard=allow,
        )
    reference_queries = [
        (sql, params) for sql, params in queries if "FROM project_speech_references" in sql
    ]
    assert len(reference_queries) == 3 and all(
        len(params) <= 501 for _, params in reference_queries
    )
    assert [value.slot_key for value in rows] == sorted(slots)


def test_pure_identity_rejects_mutated_selector_binding_and_pin(segments):
    s = segments
    source = save(s)
    for changed in (
        replace(source, selector_hash="0" * 64),
        replace(source, selector={"voice": "missing-defaults"}),
        replace(source, binding={**source.binding, "api_key": "not-allowed"}),
        replace(source, pin={"artifact_id": "invalid"}),
    ):
        with pytest.raises(ValueError):
            segment_speech_reference_identity(changed)


def test_refresh_publication_targets_copy_without_updating_original_slot_or_reading_bytes_in_writer(
    segments, monkeypatch
):
    s = segments
    source = save(s)
    copied = clone(s, {s.ids[0]: [s.ids[1]]})[s.ids[1]]
    with s.h.database.immediate_session() as session:
        session.get_one(VoiceSample, s.h.sample_id).transcript = "Refreshed copied segment voice"
    captured = capture(s, refresh=True)
    prepared = prepare(s, captured)
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    target = publish(
        s,
        captured,
        prepared,
        target_slot_key=slot(s.ids[1]),
        target_expected_revision=copied.revision,
    )
    assert target.pin != source.pin and target.revision == 2
    assert row(s, s.ids[0]) == source and row(s, s.ids[1]) == target


def test_fresh_target_publication_keeps_original_absent_and_reuse_target_is_revision_noop(
    segments, monkeypatch
):
    s = segments
    captured = capture(s)
    prepared = prepare(s, captured)
    target = publish(
        s, captured, prepared, target_slot_key=slot(s.ids[1]), target_expected_revision=0
    )
    assert row(s, s.ids[0]) is None and target.pin is not None
    source = save(s)
    copied = clone(s, {s.ids[0]: [s.ids[2]]})[s.ids[2]]
    reused = capture(s, source_authorization_guard=forbidden)
    monkeypatch.setattr(capture_module, "capture_speech_input", forbidden)
    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    assert (
        publish(
            s,
            reused,
            source_authorization_guard=forbidden,
            target_slot_key=slot(s.ids[2]),
            target_expected_revision=1,
        )
        == copied
    )
    fresh_target = publish(
        s,
        reused,
        source_authorization_guard=forbidden,
        target_slot_key=slot(s.ids[3]),
        target_expected_revision=0,
    )
    assert fresh_target.pin == source.pin and row(s, s.ids[0]) == source


@pytest.mark.parametrize(
    "change", ["source_row", "target_row", "settings", "source", "permission", "foreign", "missing"]
)
def test_target_publication_cas_and_current_source_authority_deny_atomically(segments, change):
    s = segments
    source = save(s)
    captured = capture(s, refresh=True)
    prepared = prepare(s, captured)
    kwargs = {"target_slot_key": slot(s.ids[1]), "target_expected_revision": 0}
    if change in {"source_row", "target_row"}:
        with s.h.database.immediate_session() as session:
            put_project_speech_reference(
                session,
                s.session_id,
                slot(s.ids[0] if change == "source_row" else s.ids[1]),
                selector=source.selector,
                binding=source.binding,
                pin=source.pin,
                expected_revision=1 if change == "source_row" else 0,
                authorization_guard=allow,
            )
    elif change == "settings":
        kwargs["settings"] = {**captured.settings, "speed": 2}
    elif change == "source":
        with s.h.database.immediate_session() as session:
            session.get_one(Voice, s.h.voice_id).revision += 1
    elif change == "permission":

        def deny(_unit):
            raise PermissionError("Revoked")

        kwargs["authorization_guard"] = deny
    elif change == "foreign":
        kwargs["target_slot_key"] = slot(s.foreign)
    else:
        kwargs["target_slot_key"] = slot(str(uuid4()))
    with pytest.raises((ValueError, PermissionError)):
        publish(s, captured, prepared, **kwargs)
    assert row(s, s.ids[1]) is None or change == "target_row"
    if prepared is not None:
        speech_inputs.cleanup_unpublished_speech_input(s.h.database, prepared)


@pytest.mark.parametrize(
    "invalid",
    ["one_slot", "one_revision", "bool", "negative", "target_kind", "source_kind", "upper"],
)
def test_target_extension_rejects_nonsegment_or_incomplete_contract(segments, invalid):
    s = segments
    captured = capture(s)
    prepared = prepare(s, captured)
    kwargs: dict[str, Any] = {"target_slot_key": slot(s.ids[1]), "target_expected_revision": 0}
    if invalid == "one_slot":
        del kwargs["target_expected_revision"]
    elif invalid == "one_revision":
        del kwargs["target_slot_key"]
    elif invalid == "bool":
        kwargs["target_expected_revision"] = False
    elif invalid == "negative":
        kwargs["target_expected_revision"] = -1
    elif invalid == "target_kind":
        kwargs["target_slot_key"] = "tts"
    elif invalid == "source_kind":
        captured = replace(captured, slot_key="cast:narrator")
    else:
        kwargs["target_slot_key"] = slot(s.ids[1].upper())
    with pytest.raises(ValueError):
        publish(s, captured, prepared, **kwargs)
    if prepared is not None:
        speech_inputs.cleanup_unpublished_speech_input(s.h.database, prepared)


@pytest.mark.parametrize("default_slot", ["tts", "cast:narrator", 'cast:["characters","c-alice"]'])
def test_default_publication_remains_compatible_and_adds_no_segment_header_queries(
    segments, default_slot
):
    s = segments
    with s.h.database.snapshot_session() as session:
        captured = capture_project_speech_reference(
            session,
            s.h.paths,
            s.session_id,
            default_slot,
            selector={"voice_id": s.h.voice_id},
            settings=s.h.settings,
            authorization_guard=allow,
            source_authorization_guard=allow,
        )
    prepared = prepare(s, captured)
    statements = []
    event.listen(
        s.h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    assert publish(s, captured, prepared).slot_key == default_slot
    assert not any("FROM generation_segments" in sql for sql in statements)


def test_revision_projection_selects_only_saved_unremoved_choices_in_requested_owned_revision(
    segments,
):
    s = segments
    saved = save(s)
    clone(s, {s.ids[0]: [s.ids[1], s.ids[2]]})
    with s.h.database.immediate_session() as session:
        session.get_one(GenerationSegment, s.ids[1]).removed = True
        foreign_revision = session.get_one(GenerationSegment, s.foreign).plan_revision_id
        foreign_session = session.get_one(
            GenerationPlan, session.get_one(GenerationPlanRevision, foreign_revision).plan_id
        ).session_id
        other_revision = GenerationPlanRevision(
            plan_id=session.get_one(GenerationPlanRevision, s.revision).plan_id,
            revision_number=2,
            content_hash="other-owned-revision",
        )
        session.add(other_revision)
        session.flush()
        other = GenerationSegment(
            plan_revision_id=other_revision.id, ordinal=0, text="Other owned revision"
        )
        session.add(other)
        session.flush()
        other_id = other.id
        put_project_speech_reference(
            session,
            s.session_id,
            slot(other.id),
            selector=saved.selector,
            binding=saved.binding,
            pin=saved.pin,
            expected_revision=0,
            authorization_guard=allow,
        )
        put_project_speech_reference(
            session,
            s.session_id,
            "tts",
            selector=saved.selector,
            binding=saved.binding,
            pin=saved.pin,
            expected_revision=0,
            authorization_guard=allow,
        )
        # Old generic metadata can contain a foreign logical locator. Both the
        # reference owner and segment-plan owner must constrain the projection.
        put_project_speech_reference(
            session,
            s.session_id,
            slot(s.foreign),
            selector=saved.selector,
            binding=saved.binding,
            pin=saved.pin,
            expected_revision=0,
            authorization_guard=allow,
        )
        put_project_speech_reference(
            session,
            foreign_session,
            slot(s.ids[3]),
            selector=saved.selector,
            binding={**saved.binding, "display_name": "Private foreign label"},
            pin=None,
            expected_revision=0,
            authorization_guard=allow,
        )
        other_revision_id = other_revision.id
    statements = []
    event.listen(
        s.h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    with s.h.database.snapshot_session() as session:
        rows = list_project_speech_references(
            session, s.session_id, segment_revision_id=s.revision, authorization_guard=allow
        )
        assert [value.slot_key for value in rows] == sorted([slot(s.ids[0]), slot(s.ids[2])])
        metadata_queries = [sql for sql in statements if "FROM project_speech_references" in sql]
        assert len(metadata_queries) == 1
        assert not any(
            "generation_segments.text" in sql or "artifacts.relative_path" in sql
            for sql in statements
        )
        assert [
            value.slot_key
            for value in list_project_speech_references(
                session,
                s.session_id,
                segment_revision_id=other_revision_id,
                authorization_guard=allow,
            )
        ] == [slot(other_id)]
        assert (
            list_project_speech_references(
                session,
                s.session_id,
                segment_revision_id=foreign_revision,
                authorization_guard=allow,
            )
            == []
        )
        assert (
            list_project_speech_references(
                session, s.session_id, segment_revision_id=str(uuid4()), authorization_guard=allow
            )
            == []
        )


@pytest.mark.parametrize("invalid", ["both", "uuid", "upper"])
def test_revision_projection_contract_is_guarded_and_canonical(segments, invalid):
    s = segments
    kwargs: dict[str, Any] = {"segment_revision_id": s.revision}
    if invalid == "both":
        kwargs["slot_keys"] = []
    elif invalid == "uuid":
        kwargs["segment_revision_id"] = "bad"
    else:
        kwargs["segment_revision_id"] = s.revision.upper()
    with s.h.database.snapshot_session() as session:
        with pytest.raises(ValueError):
            list_project_speech_references(
                session, s.session_id, authorization_guard=allow, **kwargs
            )

        def deny(unit):
            assert not unit.autoflush
            raise PermissionError("Revision projection denied")

        with pytest.raises(PermissionError, match="Revision projection denied"):
            list_project_speech_references(
                session, s.session_id, segment_revision_id=s.revision, authorization_guard=deny
            )
