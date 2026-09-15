"""Focused run-history/undo coverage for the optional passage-regroup pass.

Covers the metadata owned by ``generation_run_history`` and ``repair_batches``
only: regroup markers (``regroup_parent_run_id``) and reason
(``passage_regroup``) group exactly like the legacy early-repair metadata,
rejected/failed candidates never become the final result, and grouped undo
restores the whole pass from the source-root snapshot.  No live TTS is used;
database tests run against a disposable app database.
"""

from __future__ import annotations

import wave
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

import pytest
from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_run_history import build_generation_run_history
from pandrator.web.models import (
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
)
from pandrator.web.repair_batches import (
    GUARD_KEY,
    REGROUP_REASON,
    REPAIR_REASON,
    UNDO_REGROUP_REASON,
    UNDO_REPAIR_REASON,
    capture_repair_base,
    record_accepted_repair,
    repair_state_hash,
)


# ---------------------------------------------------------------------------
# Pure history projection helpers (no database).
# ---------------------------------------------------------------------------

def _run(
    run_id: str,
    *,
    session_id: str = "session-1",
    sequence_number: int = 1,
    source_generation_run_id: str | None = None,
    output_generation_run_id: str | None = None,
    operation: str = "generate",
    status: str = "completed",
    marker: str | None = None,
    marker_key: str = "regroup_parent_run_id",
):
    snapshot = {marker_key: marker} if marker else {}
    return SimpleNamespace(
        id=run_id,
        session_id=session_id,
        plan_revision_id=f"plan-{run_id}",
        source_generation_run_id=source_generation_run_id,
        output_generation_run_id=output_generation_run_id,
        operation=operation,
        status=status,
        sequence_number=sequence_number,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        settings_snapshot_json=snapshot,
    )


def _revision(run, *, repair_status: str = "applied", source: str | None = None,
              reason: str = REGROUP_REASON):
    return SimpleNamespace(
        id=run.plan_revision_id,
        operation_json={
            "reason": reason,
            "source_generation_run_id": source or run.source_generation_run_id,
            "repair_status": repair_status,
        },
    )


class RegroupHistoryProjectionTests(unittest.TestCase):
    def test_accepted_group_chain_picks_final_and_root_stays_inspectable(self):
        root = _run("root")
        first = _run("group-0", sequence_number=2,
                     source_generation_run_id=root.id, marker=root.id)
        second = _run("group-1", sequence_number=3,
                      source_generation_run_id=first.id, marker=root.id)
        revisions = {
            first.plan_revision_id: _revision(first, source=root.id),
            second.plan_revision_id: _revision(second, source=root.id),
        }
        histories = build_generation_run_history([root, first, second], revisions)

        history = histories[root.id]
        self.assertEqual(
            ("group-0", "group-1"),
            tuple(child.id for child in history.repair_children),
        )
        self.assertEqual("group-1", history.result.id)
        self.assertEqual(("group-0", "group-1"),
                         tuple(child.id for child in history.applied_children))
        # The original first-pass run stays inspectable and every accepted
        # group child resolves to the same logical entry.
        self.assertEqual("root", history.root.id)
        self.assertEqual("root", histories["group-0"].root.id)
        self.assertEqual("group-1", histories["group-1"].result.id)
        self.assertTrue(history.is_repair_child("group-0"))
        self.assertTrue(history.is_repair_child("group-1"))
        self.assertFalse(history.is_repair_child("root"))

    def test_mixed_early_and_regroup_markers_group_under_one_root(self):
        root = _run("root")
        early = _run("early-0", sequence_number=2,
                     source_generation_run_id=root.id, marker=root.id,
                     marker_key="early_repair_parent_run_id")
        regroup = _run("regroup-0", sequence_number=3,
                       source_generation_run_id=early.id, marker=root.id)
        revisions = {
            early.plan_revision_id: _revision(
                early, source=root.id, reason=REPAIR_REASON),
            regroup.plan_revision_id: _revision(regroup, source=root.id),
        }
        histories = build_generation_run_history([root, early, regroup], revisions)

        history = histories[root.id]
        self.assertEqual(
            ("early-0", "regroup-0"),
            tuple(child.id for child in history.repair_children),
        )
        self.assertEqual("regroup-0", history.result.id)

    def test_rejected_only_regroup_keeps_root_as_result(self):
        root = _run("root")
        rejected = _run("group-0", sequence_number=2,
                        source_generation_run_id=root.id, marker=root.id)
        revisions = {
            rejected.plan_revision_id: _revision(
                rejected, repair_status="not_applied", source=root.id),
        }
        histories = build_generation_run_history([root, rejected], revisions)

        history = histories[root.id]
        self.assertEqual(("group-0",),
                         tuple(child.id for child in history.repair_children))
        self.assertEqual("root", history.result.id)
        self.assertEqual((), history.applied_children)

    def test_rejected_after_accepted_retains_last_accepted(self):
        root = _run("root")
        accepted = _run("group-0", sequence_number=2,
                        source_generation_run_id=root.id, marker=root.id)
        rejected = _run("group-1", sequence_number=3,
                        source_generation_run_id=accepted.id, marker=root.id)
        revisions = {
            accepted.plan_revision_id: _revision(accepted, source=root.id),
            rejected.plan_revision_id: _revision(
                rejected, repair_status="not_applied", source=root.id),
        }
        histories = build_generation_run_history(
            [root, accepted, rejected], revisions)

        history = histories[root.id]
        self.assertEqual(("group-0", "group-1"),
                         tuple(child.id for child in history.repair_children))
        self.assertEqual("group-0", history.result.id)
        self.assertEqual(("group-0",),
                         tuple(child.id for child in history.applied_children))

    def test_failed_regroup_child_never_promoted_as_final(self):
        root = _run("root")
        accepted = _run("group-0", sequence_number=2,
                        source_generation_run_id=root.id, marker=root.id)
        failed = _run("group-1", sequence_number=3,
                      source_generation_run_id=accepted.id, marker=root.id,
                      status="failed")
        revisions = {
            accepted.plan_revision_id: _revision(accepted, source=root.id),
            failed.plan_revision_id: _revision(failed, source=root.id),
        }
        histories = build_generation_run_history(
            [root, accepted, failed], revisions)

        history = histories[root.id]
        self.assertEqual(("group-0", "group-1"),
                         tuple(child.id for child in history.repair_children))
        self.assertEqual("group-0", history.result.id)

    def test_marked_run_cannot_anchor_a_mixed_child_chain(self):
        root = _run("root")
        child = _run("group-0", sequence_number=2,
                     source_generation_run_id=root.id, marker=root.id)
        impostor = _run("impostor", sequence_number=3,
                        source_generation_run_id=child.id, marker=child.id)
        revisions = {
            child.plan_revision_id: _revision(child, source=root.id),
            impostor.plan_revision_id: _revision(impostor, source=child.id),
        }
        histories = build_generation_run_history(
            [root, child, impostor], revisions)

        self.assertEqual(("group-0",),
                         tuple(c.id for c in histories[root.id].repair_children))
        self.assertFalse(histories[impostor.id].is_repair_child(impostor.id))
        self.assertEqual("impostor", histories[impostor.id].result.id)

    def test_conflicting_markers_stay_standalone(self):
        root = _run("root")
        conflicted = _run("conflicted", sequence_number=2,
                          source_generation_run_id=root.id, marker=root.id)
        conflicted.settings_snapshot_json = {
            "early_repair_parent_run_id": root.id,
            "regroup_parent_run_id": "other-root",
        }
        revisions = {
            conflicted.plan_revision_id: _revision(conflicted, source=root.id),
        }
        histories = build_generation_run_history([root, conflicted], revisions)

        self.assertEqual((), histories[root.id].repair_children)
        self.assertFalse(
            histories[conflicted.id].is_repair_child(conflicted.id))


# ---------------------------------------------------------------------------
# Grouped batch history + undo against a disposable database.
# ---------------------------------------------------------------------------

@pytest.fixture
def regroup_workspace(tmp_path):
    tokens = BootstrapTokenStore()
    token = tokens.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=tokens)
    client = app.test_client()
    auth = client.post('/api/v1/auth/bootstrap', json={'token': token}).get_json()
    headers = {'X-CSRF-Token': auth['csrf_token'], 'Idempotency-Key': 'regroup-undo-test'}
    services = app.extensions['pandrator']
    sid = client.post('/api/v1/sessions', json={'name': 'Regroup batch fixture', 'workflow_kind': 'voiceover'}, headers=headers).get_json()['id']
    generation, database = services['generation'], services['database']
    plan = generation.create_plan(sid, source_revision_id=None, settings={}, segments=[
        {'text': 'Alpha passage one.', 'source_segment_ids': [1]},
        {'text': 'Beta passage two.', 'source_segment_ids': [2]},
        {'text': 'Gamma passage three.', 'source_segment_ids': [3]},
    ])
    base_id = plan['active_revision_id']
    audio_path = services['paths'].uploads / 'regroup-fixture.wav'
    with wave.open(str(audio_path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b'\x00\x00' * 160)
    artifact = services['artifacts'].register(audio_path, kind='audio', role='generation_take', session_id=sid)
    with database.session() as session:
        rows = list(session.scalars(select(GenerationSegment).where(GenerationSegment.plan_revision_id == base_id).order_by(GenerationSegment.ordinal)))
        for row in rows:
            row.status = 'completed'
            session.add(AudioTake(generation_segment_id=row.id, artifact_id=artifact.id, status='completed', is_active=True))
        run = GenerationRun(session_id=sid, plan_revision_id=base_id, sequence_number=1,
                            operation='generate', status='completed', settings_snapshot_json={}, settings_hash='regroup-fixture')
        session.add(run)
        session.flush()
        run_id = run.id
    context = dict(app=app, client=client, headers=headers, services=services,
                   database=database, generation=generation, sid=sid, base_id=base_id,
                   run_id=run_id, artifact_id=artifact.id,
                   url=f'/api/v1/sessions/{sid}/generation-plan')
    yield context
    database.dispose()


def _top_up_takes(session, revision_id, artifact_id):
    segments = list(session.scalars(select(GenerationSegment).where(
        GenerationSegment.plan_revision_id == revision_id,
        GenerationSegment.removed.is_(False)).order_by(GenerationSegment.ordinal)))
    for segment in segments:
        segment.status = 'completed'
        active = session.scalar(select(AudioTake).where(
            AudioTake.generation_segment_id == segment.id,
            AudioTake.is_active.is_(True)))
        if active is None:
            session.add(AudioTake(generation_segment_id=segment.id,
                                  artifact_id=artifact_id,
                                  status='completed', is_active=True))
    session.flush()
    return [segment.id for segment in segments]


def apply_regroup(w, left_id, right_id, *, status="applied", reason_text=None,
                  group_index=0, snapshot=None):
    """Stage one regroup merge exactly like the regroup pass metadata does."""
    from pandrator.web.voiceover_repair import _record_repair_outcome

    with w['database'].immediate_session() as session:
        plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == w['sid']))
        expected = plan.active_revision_id
        if snapshot is None:
            snapshot = capture_repair_base(session, w['base_id'])
        operation = {'action': 'merge', 'left_segment_id': left_id,
                     'right_segment_id': right_id, 'reason': REGROUP_REASON,
                     'source_generation_run_id': w['run_id'],
                     GUARD_KEY: dict(snapshot),
                     'source_block_ordinal': group_index,
                     'repair_status': 'pending'}
        result = w['generation'].revise_topology_in_session(
            session, w['sid'], expected, operation)
        revision_id = result['plan_revision_id']
        _top_up_takes(session, revision_id, w['artifact_id'])
        session.flush()
        _record_repair_outcome(session, revision_id, status, reason_text)
        if status == "applied":
            record_accepted_repair(session, revision_id)
    return revision_id


def regroup_batch(w):
    response = w['client'].get(f"{w['url']}/repair-batches/{w['run_id']}")
    assert response.status_code == 200, response.get_json()
    return response.get_json()['repair_batch']


def regroup_undo(w, summary=None, **overrides):
    summary = summary or regroup_batch(w)
    body = {key: summary[key] for key in ('expected_revision_id', 'expected_state_hash')}
    body.update(overrides)
    return w['client'].post(f"{w['url']}/repair-batches/{w['run_id']}/undo", json=body, headers=w['headers'])


def _segment_texts(w, revision_id):
    with w['database'].session() as session:
        rows = list(session.scalars(select(GenerationSegment).where(
            GenerationSegment.plan_revision_id == revision_id,
            GenerationSegment.removed.is_(False)).order_by(GenerationSegment.ordinal)))
        return [row.text for row in rows]


def _active_segment_ids(w, revision_id):
    with w['database'].session() as session:
        return [row.id for row in session.scalars(select(GenerationSegment).where(
            GenerationSegment.plan_revision_id == revision_id,
            GenerationSegment.removed.is_(False)).order_by(GenerationSegment.ordinal))]


def test_two_accepted_groups_pick_final_and_undo_restores_whole_pass(regroup_workspace):
    w = regroup_workspace
    with w['database'].session() as session:
        snapshot = capture_repair_base(session, w['base_id'])
    first_ids = _active_segment_ids(w, w['base_id'])
    first_id = apply_regroup(w, first_ids[0], first_ids[1],
                             group_index=0, snapshot=snapshot)
    second_ids = _active_segment_ids(w, first_id)
    second_id = apply_regroup(w, second_ids[0], second_ids[1],
                              group_index=1, snapshot=snapshot)

    summary = regroup_batch(w)
    assert summary['reason'] == REGROUP_REASON
    assert summary['attempt_count'] == 2
    assert summary['applied_count'] == 2
    assert summary['result_revision_id'] == second_id
    assert summary['can_undo'] is True

    history = w['client'].get(w['url'] + '/history').get_json()
    batch_entries = [item for item in history['items'] if item.get('repair_batch')]
    assert len(batch_entries) == 1
    entry = batch_entries[0]
    assert entry['id'] == second_id
    assert 'passage regroup' in entry['summary']
    assert 'timing repair' not in entry['summary']
    assert 'split' not in entry['summary'].lower()
    assert entry['origin'] == 'automatic'

    detail = w['client'].get(f"{w['url']}/repair-batches/{w['run_id']}").get_json()
    assert detail['repair_batch']['reason'] == REGROUP_REASON
    assert {item['reason'] for item in detail['items']} == {REGROUP_REASON}

    response = regroup_undo(w, summary)
    assert response.status_code == 201, response.get_json()
    restored_id = response.get_json()['plan_revision_id']
    with w['database'].session() as session:
        restored = session.get(GenerationPlanRevision, restored_id)
        assert restored.operation_json['reason'] == UNDO_REGROUP_REASON
        assert restored.operation_json['reason'] != UNDO_REPAIR_REASON
    assert _segment_texts(w, restored_id) == [
        'Alpha passage one.', 'Beta passage two.', 'Gamma passage three.']
    with w['database'].session() as session:
        selected = list(session.scalars(select(AudioTake).where(
            AudioTake.is_active.is_(True))))
        # Base (3) plus both accepted groups (2 + 1) plus the restore (3):
        # undo preserves every generation's audio, it never deletes takes.
        assert len(selected) == 3 + 2 + 1 + 3
        restored_selected = list(session.scalars(select(AudioTake).where(
            AudioTake.is_active.is_(True),
            AudioTake.generation_segment_id.in_(
                select(GenerationSegment.id).where(
                    GenerationSegment.plan_revision_id == restored_id)))))
        assert len(restored_selected) == 3
    assert regroup_batch(w)['can_undo'] is False


def test_rejected_only_regroup_keeps_base_result_and_no_undo(regroup_workspace):
    w = regroup_workspace
    ids = _active_segment_ids(w, w['base_id'])
    apply_regroup(w, ids[0], ids[1], status="not_applied",
                  reason_text="duration_misfit", group_index=0)
    with w['database'].session() as session:
        session.scalar(select(GenerationPlan).where(
            GenerationPlan.session_id == w['sid'])).active_revision_id = w['base_id']

    summary = regroup_batch(w)
    assert summary['reason'] == REGROUP_REASON
    assert summary['applied_count'] == 0
    assert summary['result_revision_id'] == w['base_id']
    assert summary['status'] == 'no_changes'
    assert summary['can_undo'] is False

    history = w['client'].get(w['url'] + '/history').get_json()
    entry = next(item for item in history['items'] if item.get('repair_batch'))
    assert entry['id'] == w['base_id']
    assert 'passage regroup' in entry['summary']


def test_rejected_after_accepted_retains_last_accepted_and_stale_edit_blocks_undo(regroup_workspace):
    w = regroup_workspace
    with w['database'].session() as session:
        snapshot = capture_repair_base(session, w['base_id'])
    ids = _active_segment_ids(w, w['base_id'])
    accepted_id = apply_regroup(w, ids[0], ids[1],
                                group_index=0, snapshot=snapshot)
    accepted_ids = _active_segment_ids(w, accepted_id)
    apply_regroup(w, accepted_ids[0], accepted_ids[1], status="not_applied",
                  reason_text="duration_misfit", group_index=1, snapshot=snapshot)
    with w['database'].session() as session:
        # Rejected candidates are staged but never activated.
        session.scalar(select(GenerationPlan).where(
            GenerationPlan.session_id == w['sid'])).active_revision_id = accepted_id

    summary = regroup_batch(w)
    assert summary['applied_count'] == 1
    assert summary['attempt_count'] == 2
    assert summary['result_revision_id'] == accepted_id
    assert summary['can_undo'] is True

    with w['database'].session() as session:
        row = session.scalars(select(GenerationSegment).where(
            GenerationSegment.plan_revision_id == accepted_id,
            GenerationSegment.removed.is_(False)).order_by(
                GenerationSegment.ordinal)).first()
        row.text += ' Later manual edit.'
    before = session_count(w)
    response = regroup_undo(w, summary)
    assert response.status_code == 409, response.get_json()
    assert session_count(w) == before
    assert regroup_batch(w)['can_undo'] is False


def session_count(w):
    with w['database'].session() as session:
        return session.scalar(select(func.count()).select_from(GenerationPlanRevision))


def test_legacy_timing_repair_batch_labels_unchanged(regroup_workspace):
    w = regroup_workspace
    with w['database'].immediate_session() as session:
        snapshot = capture_repair_base(session, w['base_id'])
        rows = list(session.scalars(select(GenerationSegment).where(
            GenerationSegment.plan_revision_id == w['base_id']).order_by(
                GenerationSegment.ordinal)))
        operation = {'action': 'split', 'segment_id': rows[0].id, 'cursor': 5,
                     'text_layer': 'display', 'reason': REPAIR_REASON,
                     'source_generation_run_id': w['run_id'],
                     'repair_status': 'applied', GUARD_KEY: snapshot}
        result = w['generation'].revise_topology_in_session(
            session, w['sid'], w['base_id'], operation)
        _top_up_takes(session, result['plan_revision_id'], w['artifact_id'])
        session.flush()
        record_accepted_repair(session, result['plan_revision_id'])

    summary = regroup_batch(w)
    assert summary['reason'] == REPAIR_REASON
    history = w['client'].get(w['url'] + '/history').get_json()
    entry = next(item for item in history['items'] if item.get('repair_batch'))
    assert 'timing repair' in entry['summary']


if __name__ == "__main__":
    unittest.main()
