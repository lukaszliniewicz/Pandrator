"""Disposable-database regression tests for grouped repair history and Undo."""

import wave

import pytest
from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
)
from pandrator.web.repair_batches import (
    GUARD_KEY,
    capture_repair_base,
    record_accepted_repair,
    repair_state_hash,
)


@pytest.fixture
def workspace(tmp_path):
    tokens = BootstrapTokenStore()
    token = tokens.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=tokens)
    client = app.test_client()
    auth = client.post('/api/v1/auth/bootstrap', json={'token': token}).get_json()
    headers = {'X-CSRF-Token': auth['csrf_token'], 'Idempotency-Key': 'repair-undo-test'}
    services = app.extensions['pandrator']
    sid = client.post('/api/v1/sessions', json={'name': 'Batch repair fixture', 'workflow_kind': 'voiceover'}, headers=headers).get_json()['id']
    generation, database = services['generation'], services['database']
    plan = generation.create_plan(sid, source_revision_id=None, settings={}, segments=[
        {'text': 'First sentence. Second sentence.', 'source_segment_ids': [1, 2]},
        {'text': 'Unchanged final sentence.', 'source_segment_ids': [3]},
    ])
    base_id = plan['active_revision_id']
    audio_path = services['paths'].uploads / 'fixture.wav'
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
                            operation='generate', status='completed', settings_snapshot_json={}, settings_hash='fixture')
        session.add(run)
        session.flush()
        run_id, first_id = run.id, rows[0].id
    context = dict(app=app, client=client, headers=headers, services=services,
                   database=database, generation=generation, sid=sid, base_id=base_id,
                   run_id=run_id, first_id=first_id, artifact_id=artifact.id,
                   url=f'/api/v1/sessions/{sid}/generation-plan')
    yield context
    database.dispose()


def apply_repair(w, *, guarded=True):
    with w['database'].immediate_session() as session:
        snapshot = capture_repair_base(session, w['base_id']) if guarded else None
        operation = {'action': 'split', 'segment_id': w['first_id'], 'cursor': 15,
                     'text_layer': 'display', 'reason': 'early_timing_repair',
                     'source_generation_run_id': w['run_id'], 'repair_status': 'applied'}
        if guarded:
            operation[GUARD_KEY] = snapshot
        result = w['generation'].revise_topology_in_session(session, w['sid'], w['base_id'], operation)
        for segment_id in result['affected_segment_ids']:
            segment = session.get(GenerationSegment, segment_id)
            segment.status = 'completed'
            session.add(AudioTake(generation_segment_id=segment_id, artifact_id=w['artifact_id'], status='completed', is_active=True))
        session.flush()
        if guarded:
            record_accepted_repair(session, result['plan_revision_id'])
    return result['plan_revision_id']


def batch(w):
    response = w['client'].get(f"{w['url']}/repair-batches/{w['run_id']}")
    assert response.status_code == 200, response.get_json()
    return response.get_json()['repair_batch']


def undo(w, summary=None, **overrides):
    summary = summary or batch(w)
    body = {key: summary[key] for key in ('expected_revision_id', 'expected_state_hash')}
    body.update(overrides)
    return w['client'].post(f"{w['url']}/repair-batches/{w['run_id']}/undo", json=body, headers=w['headers'])


def revision_count(w):
    with w['database'].session() as session:
        return session.scalar(select(func.count()).select_from(GenerationPlanRevision))


def test_undo_creates_one_restore_and_preserves_original_and_repaired_audio(workspace):
    w = workspace
    result_id = apply_repair(w)
    summary = batch(w)
    assert summary['can_undo'] is True
    with w['database'].session() as session:
        base_hash = repair_state_hash(session, w['base_id'])
        result_hash = repair_state_hash(session, result_id)
    response = undo(w, summary)
    assert response.status_code == 201, response.get_json()
    restored_id = response.get_json()['plan_revision_id']
    assert revision_count(w) == 3
    with w['database'].session() as session:
        assert repair_state_hash(session, w['base_id']) == base_hash
        assert repair_state_hash(session, result_id) == result_hash
        restored = session.get(GenerationPlanRevision, restored_id)
        assert restored.operation_json['reason'] == 'undo_timing_repairs'
        rows = list(session.scalars(select(GenerationSegment).where(GenerationSegment.plan_revision_id == restored_id).order_by(GenerationSegment.ordinal)))
        assert [row.text for row in rows] == ['First sentence. Second sentence.', 'Unchanged final sentence.']
        selected = list(session.scalars(select(AudioTake).where(AudioTake.generation_segment_id.in_([row.id for row in rows]), AudioTake.is_active.is_(True))))
        assert len(selected) == 2
        assert {take.artifact_id for take in selected} == {w['artifact_id']}
    replay = undo(w, summary)
    assert replay.status_code == 201
    assert replay.headers['Idempotency-Replayed'] == 'true'
    assert replay.get_json()['plan_revision_id'] == restored_id
    assert revision_count(w) == 3
    assert batch(w)['can_undo'] is False


@pytest.mark.parametrize('change', ['text', 'take', 'base', 'work', 'plan'])
def test_changes_after_acceptance_block_undo_without_mutation(workspace, change):
    w = workspace
    result_id = apply_repair(w)
    summary = batch(w)
    with w['database'].session() as session:
        rows = list(session.scalars(select(GenerationSegment).where(GenerationSegment.plan_revision_id == result_id)))
        if change == 'text':
            rows[0].text += ' Later manual edit.'  # Deliberately same revision ID.
        elif change == 'take':
            take = session.scalar(select(AudioTake).where(AudioTake.generation_segment_id == rows[0].id, AudioTake.is_active.is_(True)))
            take.is_active = False
            session.add(AudioTake(generation_segment_id=rows[0].id, artifact_id=w['artifact_id'], status='completed', is_active=True))
        elif change == 'base':
            session.get(GenerationSegment, w['first_id']).text += ' Changed original.'
        elif change == 'work':
            session.add(Job(session_id=w['sid'], kind='noop', status='queued', payload_json={}))
        else:
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == w['sid']))
            plan.active_revision_id = w['base_id']
    before = revision_count(w)
    response = undo(w, summary)
    assert response.status_code == 409, response.get_json()
    assert revision_count(w) == before
    assert batch(w)['can_undo'] is False


def test_unverified_legacy_batches_remain_inspectable_but_not_undoable(workspace):
    w = workspace
    apply_repair(w, guarded=False)
    summary = batch(w)
    assert summary['applied_count'] == 1
    assert not summary['can_undo']
    assert 'older batch' in summary['undo_disabled_reason']
    response = undo(w, summary, expected_state_hash='0' * 64)
    assert response.status_code == 409


def test_fifty_attempts_group_before_pagination_and_manual_changes_stay_separate(workspace):
    w = workspace
    with w['database'].session() as session:
        base = session.get(GenerationPlanRevision, w['base_id'])
        checkpoint_ids = []
        for index in range(50):
            checkpoint = GenerationPlanRevision(plan_id=base.plan_id, parent_revision_id=w['base_id'],
                source_revision_id=None, revision_number=index + 2, settings_json={}, content_hash=f'checkpoint-{index}',
                operation_json={'action': 'split', 'reason': 'early_timing_repair', 'source_generation_run_id': w['run_id'],
                                'repair_status': 'applied' if index < 49 else 'not_applied'})
            session.add(checkpoint)
            session.flush()
            checkpoint_ids.append(checkpoint.id)
        manual = GenerationPlanRevision(plan_id=base.plan_id, parent_revision_id=checkpoint_ids[-2],
            revision_number=52, settings_json={}, content_hash='manual', operation_json={'action': 'merge'})
        session.add(manual)
        session.flush()
        session.get(GenerationPlan, base.plan_id).active_revision_id = manual.id
        manual_id = manual.id
    first = w['client'].get(w['url'] + '/history?limit=1').get_json()
    assert first['total'] == 3
    assert first['checkpoint_total'] == 52
    assert first['items'][0]['id'] == manual_id
    second = w['client'].get(w['url'] + '/history', query_string={'limit': 1, 'before_revision_number': first['next_before_revision_number']}).get_json()
    assert second['items'][0]['repair_batch']['attempt_count'] == 50
    assert second['items'][0]['repair_batch']['applied_count'] == 49
    assert second['items'][0]['id'] == checkpoint_ids[-2]  # Rejecting the last trial must not replace accepted audio.
    third = w['client'].get(w['url'] + '/history', query_string={'limit': 1, 'before_revision_number': second['next_before_revision_number']}).get_json()
    assert third['items'][0]['id'] == w['base_id']
    assert third['next_before_revision_number'] is None
    details = w['client'].get(f"{w['url']}/repair-batches/{w['run_id']}?limit=7").get_json()
    assert len(details['items']) == 7
    assert details['next_before_revision_number'] is not None
    with w['database'].session() as session:
        session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == w['sid'])).active_revision_id = checkpoint_ids[10]
    selected = w['client'].get(w['url'] + '/history').get_json()
    entry = next(item for item in selected['items'] if item.get('repair_batch'))
    assert entry['id'] == checkpoint_ids[10]
    assert entry['is_active']
    assert entry['is_repair_checkpoint']
    assert 'selected checkpoint' in entry['summary']
    status_response = w['client'].get(w['url'] + '/status')
    assert status_response.status_code == 200, status_response.get_json()
    picker = status_response.get_json()
    assert len(picker['items']) == 3
    assert picker['selected_revision_id'] == checkpoint_ids[10]
    assert any(item['id'] == picker['selected_revision_id'] for item in picker['items'])
    raw = w['client'].get(w['url'] + '/revisions').get_json()
    assert raw['total'] == 52  # Legacy/raw API remains compatible.


@pytest.mark.parametrize('status,expected', [('not_applied', 'no_changes'), ('failed', 'failed'), ('stopped', 'stopped'), ('pending', 'stopped')])
def test_failed_rejected_and_interrupted_batches_do_not_claim_an_applied_result(workspace, status, expected):
    w = workspace
    result_id = apply_repair(w)
    with w['database'].session() as session:
        revision = session.get(GenerationPlanRevision, result_id)
        revision.operation_json = {**revision.operation_json, 'repair_status': status}
        session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == w['sid'])).active_revision_id = w['base_id']
    summary = batch(w)
    assert summary['status'] == expected
    assert summary['result_revision_id'] == w['base_id']
    assert summary['applied_count'] == 0
    assert summary['can_undo'] is False


def test_auth_validation_scope_and_stale_request_hash(workspace):
    w = workspace
    apply_repair(w)
    endpoint = f"{w['url']}/repair-batches/{w['run_id']}/undo"
    assert w['app'].test_client().get(w['url'] + '/history').status_code == 401
    assert w['client'].get(w['url'] + '/history?limit=0').status_code == 422
    assert w['client'].get(w['url'] + '/history?limit=nonsense').status_code == 422
    assert w['client'].post(endpoint, json={}, headers=w['headers']).status_code == 422
    summary = batch(w)
    body = {key: summary[key] for key in ('expected_revision_id', 'expected_state_hash')}
    assert w['client'].post(endpoint, json=body, headers={'X-CSRF-Token': w['headers']['X-CSRF-Token']}).status_code == 400
    assert undo(w, summary, expected_state_hash='f' * 64).status_code == 409
    sid2 = w['client'].post('/api/v1/sessions', json={'name': 'Other'}, headers={**w['headers'], 'Idempotency-Key': 'other-session'}).get_json()['id']
    assert w['client'].get(f'/api/v1/sessions/{sid2}/generation-plan/repair-batches/{w["run_id"]}').status_code == 404
    assert w['client'].post(f'/api/v1/sessions/{sid2}/generation-plan/repair-batches/{w["run_id"]}/undo', json=body, headers=w['headers']).status_code == 404
    assert revision_count(w) == 2
