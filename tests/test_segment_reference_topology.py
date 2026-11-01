"""Saved segment voice selections follow validated generation topology lineage."""

from __future__ import annotations

import re
import uuid
from copy import deepcopy
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from pandrator.web import generation_topology, speech_inputs
from pandrator.web import models as m
from pandrator.web.project_speech_references import (
    get_project_speech_reference,
    put_project_speech_reference,
    speech_reference_selector,
)
from pandrator.web.projects.artifact_identity import resolve_artifact_ownership
from pandrator.web.projects.speech_reference_models import ProjectSpeechReference
from pandrator.web.speech_input_contracts import SpeechInputError, SpeechInputOwner
from tests.test_performance_plans import case as case

_MISMATCH = (
    "These segments use different saved voice selections. Keep their boundary or "
    "choose one voice before merging."
)


def allow(session: Session) -> None:
    assert not session.autoflush


def make_pin(case: dict[str, Any], digest: str) -> dict[str, Any]:
    session_id = case["session_id"]
    database = case["services"]["database"]
    with database.immediate_session() as session:
        ownership = resolve_artifact_ownership(session, session_id=session_id)
        audio = m.Artifact(
            workspace_id=ownership.workspace_id,
            project_id=ownership.project_id,
            session_id=session_id,
            kind="audio",
            role="speech_reference_audio",
            relative_path=f"topology/{uuid.uuid4().hex}.wav",
            mime_type="audio/wav",
            content_hash="c" * 64,
            state="current",
        )
        envelope = m.Artifact(
            workspace_id=ownership.workspace_id,
            project_id=ownership.project_id,
            session_id=session_id,
            kind="speech_voice_snapshot",
            role="speech_input",
            relative_path=f"topology/{uuid.uuid4().hex}.json",
            mime_type="application/json",
            content_hash=digest,
            state="current",
        )
        session.add_all([audio, envelope])
        session.flush()
        session.add(
            m.ArtifactEdge(
                parent_artifact_id=audio.id,
                child_artifact_id=envelope.id,
                relation="speech_reference",
            )
        )
        session.flush()
        return {
            "version": 1,
            "artifact_id": envelope.id,
            "content_hash": digest,
        }


def save_reference(
    case: dict[str, Any],
    segment_id: str,
    *,
    voice_id: str = "voice-a",
    pin: dict[str, Any] | None = None,
    display_name: str = "Voice A",
    expected_revision: int = 0,
):
    selector = speech_reference_selector({"voice_id": voice_id})
    binding = {
        "adapter": "audio_cpp",
        "model": "synthetic-model",
        "voice": voice_id,
        "display_name": display_name,
    }
    with case["services"]["database"].immediate_session() as session:
        return put_project_speech_reference(
            session,
            case["session_id"],
            "segment:" + segment_id,
            selector=selector,
            binding=binding,
            pin=pin,
            expected_revision=expected_revision,
            authorization_guard=allow,
        )


def saved_reference(case: dict[str, Any], segment_id: str):
    with case["services"]["database"].session() as session:
        return get_project_speech_reference(
            session,
            case["session_id"],
            "segment:" + segment_id,
            authorization_guard=allow,
        )


def revise(case: dict[str, Any], revision_id: str, **operation: Any) -> dict[str, Any]:
    return case["services"]["generation"].revise_topology(
        case["session_id"], revision_id, operation
    )


def _count(case: dict[str, Any], model: Any) -> int:
    with case["services"]["database"].session() as session:
        statement = select(func.count())
        if model is m.ArtifactEdge:
            statement = statement.select_from(model).join(
                m.Artifact, m.Artifact.id == model.parent_artifact_id
            ).where(m.Artifact.session_id == case["session_id"])
        elif model is m.GenerationPlanRevision:
            statement = statement.select_from(model).join(
                m.GenerationPlan, m.GenerationPlan.id == model.plan_id
            ).where(m.GenerationPlan.session_id == case["session_id"])
        else:
            statement = statement.select_from(model).where(
                model.session_id == case["session_id"]
            )
        value = session.scalar(statement)
        assert value is not None
        return value


def _active_revision(case: dict[str, Any]) -> str:
    with case["services"]["database"].session() as session:
        value = session.scalar(
            select(m.GenerationPlan.active_revision_id).where(
                m.GenerationPlan.session_id == case["session_id"]
            )
        )
        assert value is not None
        return value


def _segment_snapshot(case: dict[str, Any], revision_id: str) -> list[tuple[Any, ...]]:
    fields = (
        "id",
        "ordinal",
        "text",
        "optimized_text",
        "voice",
        "voice_id",
        "language",
        "speaker",
        "status",
        "removed",
        "source_segment_ids_json",
        "speech_plan_json",
        "speech_block_provenance_json",
    )
    with case["services"]["database"].session() as session:
        rows = session.scalars(
            select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == revision_id)
            .order_by(m.GenerationSegment.ordinal)
        )
        return [tuple(deepcopy(getattr(row, field)) for field in fields) for row in rows]


def test_split_copies_pinned_selection_and_unchanged_neighbor_without_file_io(
    case, monkeypatch
):
    source_ids = case["segment_ids"]
    first_pin = make_pin(case, "a" * 64)
    first = save_reference(case, source_ids[0], pin=first_pin)
    neighbor = save_reference(
        case,
        source_ids[1],
        voice_id="voice-neighbor",
        display_name="Neighbor",
    )
    artifact_count = _count(case, m.Artifact)
    edge_count = _count(case, m.ArtifactEdge)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Topology association copy must not read retained files")

    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    result = revise(
        case,
        case["revision_id"],
        action="split",
        segment_id=source_ids[0],
        cursor=5,
    )

    first_targets = result["lineage"][source_ids[0]]
    neighbor_target = result["lineage"][source_ids[1]][0]
    assert len(first_targets) == 2
    for target_id in first_targets:
        target = saved_reference(case, target_id)
        assert target is not None
        assert target.selector == first.selector
        assert target.selector_hash == first.selector_hash
        assert target.binding == first.binding
        assert target.pin == first_pin
    copied_neighbor = saved_reference(case, neighbor_target)
    assert copied_neighbor is not None
    assert copied_neighbor.selector == neighbor.selector
    assert copied_neighbor.binding == neighbor.binding
    assert copied_neighbor.pin is None
    assert saved_reference(case, source_ids[0]) == first
    assert saved_reference(case, source_ids[1]) == neighbor
    assert _count(case, m.Artifact) == artifact_count
    assert _count(case, m.ArtifactEdge) == edge_count


def test_historical_restore_uses_historical_selection_and_pin(case):
    source_id = case["segment_ids"][0]
    historical_pin = make_pin(case, "a" * 64)
    historical = save_reference(case, source_id, pin=historical_pin)
    split = revise(
        case,
        case["revision_id"],
        action="split",
        segment_id=source_id,
        cursor=5,
    )
    current_child = split["lineage"][source_id][0]
    newer_pin = make_pin(case, "b" * 64)
    newer = save_reference(
        case,
        current_child,
        voice_id="voice-current",
        pin=newer_pin,
        display_name="Current voice",
        expected_revision=1,
    )

    restored = revise(
        case,
        split["plan_revision_id"],
        action="restore",
        target_revision_id=case["revision_id"],
    )

    restored_id = restored["lineage"][source_id][0]
    restored_reference = saved_reference(case, restored_id)
    assert restored_reference is not None
    assert restored_reference.selector == historical.selector
    assert restored_reference.binding == historical.binding
    assert restored_reference.pin == historical_pin
    assert saved_reference(case, source_id) == historical
    assert saved_reference(case, current_child) == newer


def test_equivalent_selected_merge_preserves_stable_source_metadata_and_pin(case):
    left_id, right_id = case["segment_ids"][:2]
    left_pin = make_pin(case, "d" * 64)
    right_pin = make_pin(case, "d" * 64)
    left = save_reference(case, left_id, pin=left_pin, display_name="Left label")
    right = save_reference(
        case,
        right_id,
        pin=right_pin,
        display_name="Right label",
    )
    artifact_count = _count(case, m.Artifact)
    expected = left if left_id < right_id else right

    result = revise(
        case,
        case["revision_id"],
        action="merge",
        left_segment_id=left_id,
        right_segment_id=right_id,
    )

    target_id = result["lineage"][left_id][0]
    target = saved_reference(case, target_id)
    assert target is not None
    assert target.selector == expected.selector
    assert target.binding == expected.binding
    assert target.pin == expected.pin
    assert saved_reference(case, left_id) == left
    assert saved_reference(case, right_id) == right
    assert _count(case, m.Artifact) == artifact_count


@pytest.mark.parametrize("difference", ["inherited", "selector", "material"])
def test_different_merge_selections_reject_before_activation_or_cancel(
    case, monkeypatch, difference
):
    left_id, right_id = case["segment_ids"][:2]
    left_pin = None
    right_pin = None
    if difference == "inherited":
        save_reference(case, left_id)
    elif difference == "selector":
        save_reference(case, left_id)
        save_reference(case, right_id, voice_id="voice-b", display_name="Voice B")
    else:
        left_pin = make_pin(case, "1" * 64)
        right_pin = make_pin(case, "2" * 64)
        save_reference(case, left_id, pin=left_pin)
        save_reference(case, right_id, pin=right_pin)

    left_before = saved_reference(case, left_id)
    right_before = saved_reference(case, right_id)
    source_rows_before = _segment_snapshot(case, case["revision_id"])
    before_revision_count = _count(case, m.GenerationPlanRevision)
    before_reference_count = _count(case, ProjectSpeechReference)
    before_active = _active_revision(case)
    cancellation_calls = []
    monkeypatch.setattr(
        generation_topology,
        "mark_output_assemblies_stale",
        lambda *args, **kwargs: cancellation_calls.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match=re.escape(_MISMATCH)):
        revise(
            case,
            before_active,
            action="merge",
            left_segment_id=left_id,
            right_segment_id=right_id,
        )

    assert cancellation_calls == []
    assert _active_revision(case) == before_active
    assert _count(case, m.GenerationPlanRevision) == before_revision_count
    assert _count(case, ProjectSpeechReference) == before_reference_count
    assert saved_reference(case, left_id) == left_before
    assert saved_reference(case, right_id) == right_before
    assert _segment_snapshot(case, case["revision_id"]) == source_rows_before


def test_selected_canonical_and_inherited_legacy_merge_rejects(case, monkeypatch):
    left_id, right_id = case["segment_ids"][:2]
    save_reference(case, left_id)
    legacy_right = "legacy-source-right"
    with case["services"]["database"].immediate_session() as session:
        row = session.get(m.GenerationSegment, right_id)
        assert row is not None
        row.id = legacy_right
    before_active = _active_revision(case)
    left_before = saved_reference(case, left_id)
    source_rows_before = _segment_snapshot(case, before_active)
    calls = []
    monkeypatch.setattr(
        generation_topology,
        "mark_output_assemblies_stale",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match=re.escape(_MISMATCH)):
        revise(
            case,
            before_active,
            action="merge",
            left_segment_id=left_id,
            right_segment_id=legacy_right,
        )

    assert calls == []
    assert _active_revision(case) == before_active
    assert saved_reference(case, left_id) == left_before
    assert _segment_snapshot(case, before_active) == source_rows_before


def test_resegmentation_checks_saved_identity_before_making_draft(case):
    selected = case["segment_ids"][:2]
    save_reference(case, selected[0])
    before_revisions = _count(case, m.GenerationPlanRevision)

    with pytest.raises(ValueError, match=re.escape(_MISMATCH)):
        revise(
            case,
            case["revision_id"],
            action="resegment",
            segment_ids=selected,
            boundaries=[11],
        )

    assert _active_revision(case) == case["revision_id"]
    assert _count(case, m.GenerationPlanRevision) == before_revisions


def test_resegmentation_copies_same_saved_identity_to_descendants(case):
    selected = case["segment_ids"][:2]
    first = save_reference(case, selected[0])
    second = save_reference(case, selected[1], display_name="Equivalent label")
    result = revise(
        case,
        case["revision_id"],
        action="resegment",
        segment_ids=selected,
        boundaries=[11],
    )

    assert result["is_draft"] is True
    source_references = {selected[0]: first, selected[1]: second}
    targets = set().union(*(set(result["lineage"][source_id]) for source_id in selected))
    assert targets
    for target_id in targets:
        incoming = sorted(
            source_id
            for source_id in selected
            if target_id in result["lineage"][source_id]
        )
        target = saved_reference(case, target_id)
        source = source_references[incoming[0]]
        assert target is not None
        assert target.selector == source.selector
        assert target.selector_hash == source.selector_hash
        assert target.pin == source.pin
        assert target.binding == source.binding


def test_retired_reference_metadata_follows_split_but_material_stays_unusable(
    case, monkeypatch
):
    source_id = case["segment_ids"][0]
    pin = make_pin(case, "e" * 64)
    source = save_reference(case, source_id, pin=pin)
    with case["services"]["database"].immediate_session() as session:
        envelope = session.get(m.Artifact, pin["artifact_id"])
        assert envelope is not None
        envelope.state = "deleted"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Retained metadata copy must not inspect files")

    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    result = revise(
        case,
        case["revision_id"],
        action="split",
        segment_id=source_id,
        cursor=5,
    )
    for target_id in result["lineage"][source_id]:
        copied = saved_reference(case, target_id)
        assert copied is not None and copied.pin == pin
    assert saved_reference(case, source_id) == source
    with case["services"]["database"].session() as session:
        ownership = resolve_artifact_ownership(session, session_id=case["session_id"])
        owner = SpeechInputOwner(
            workspace_id=ownership.workspace_id,
            project_id=ownership.project_id,
            session_id=case["session_id"],
        )
        with pytest.raises(SpeechInputError):
            speech_inputs.check_speech_input(session, pin, owner=owner)


def test_canonical_unretained_topology_uses_only_header_probe(case):
    statements: list[str] = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(case["services"]["database"].engine, "before_cursor_execute", capture_sql)
    try:
        revise(
            case,
            case["revision_id"],
            action="split",
            segment_id=case["segment_ids"][0],
            cursor=5,
        )
    finally:
        event.remove(
            case["services"]["database"].engine, "before_cursor_execute", capture_sql
        )

    reference_selects = [
        sql.lower()
        for sql in statements
        if "from project_speech_references" in sql.lower()
    ]
    assert len(reference_selects) == 1
    assert "select project_speech_references.slot_key" in reference_selects[0]
    assert "selector_json" not in reference_selects[0]
    assert "binding_json" not in reference_selects[0]


def test_unretained_legacy_segment_ids_keep_topology_compatible_without_reference_query(
    case,
):
    legacy_ids = [f"legacy-source-{index}" for index in range(3)]
    with case["services"]["database"].immediate_session() as session:
        rows = list(
            session.scalars(
                select(m.GenerationSegment)
                .where(m.GenerationSegment.plan_revision_id == case["revision_id"])
                .order_by(m.GenerationSegment.ordinal)
            )
        )
        assert len(rows) == len(legacy_ids)
        for row, legacy_id in zip(rows, legacy_ids, strict=True):
            row.id = legacy_id

    statements: list[str] = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(case["services"]["database"].engine, "before_cursor_execute", capture_sql)
    try:
        result = revise(
            case,
            case["revision_id"],
            action="split",
            segment_id=legacy_ids[0],
            cursor=5,
        )
    finally:
        event.remove(
            case["services"]["database"].engine, "before_cursor_execute", capture_sql
        )
    assert len(result["lineage"][legacy_ids[0]]) == 2
    assert not any("project_speech_references" in sql.lower() for sql in statements)
