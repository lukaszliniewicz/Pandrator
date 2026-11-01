"""Workflow plans obey current project rights and reads never materialize state."""

from dataclasses import replace

import pytest
from sqlalchemy import event

from pandrator.web import models as m
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.outcome_plans import OutcomePlanService, derive_legacy_outcome
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import scoped_token


@pytest.fixture
def plans(case, monkeypatch):
    users = team(case, monkeypatch)
    private = body(users.alice.post('/api/v1/sessions', json={
        'name': 'Private workflow', 'workflow_kind': 'voiceover',
    }, headers=headers(users.ah, 'private-outcome-session')), 201)
    shared = body(users.alice.post('/api/v1/sessions', json={
        'name': 'Team workflow', 'workflow_kind': 'voiceover', 'workspace_id': users.workspace_id,
    }, headers=headers(users.ah, 'shared-outcome-session')), 201)
    return case, users, private, shared


def stored(case, sid):
    with case.database.snapshot_session() as session:
        row = session.get(m.OutcomePlan, sid)
        return None if row is None else (row.revision, dict(row.value_json))


@pytest.mark.parametrize('outsider', ['bob', 'owner'])
def test_outsider_and_unrelated_administrator_cannot_read_or_write_private_plan(plans, outsider):
    case, users, private, _shared = plans
    with case.database.immediate_session() as session:
        value = derive_legacy_outcome(session.get_one(m.SessionRecord, private['id']))
        value['focus'] = 'custom'
        session.add(m.OutcomePlan(session_id=private['id'], revision=1, value_json=value))
    before = stored(case, private['id'])
    client = getattr(users, outsider)
    csrf = users.bh if outsider == 'bob' else users.oh
    protected_reads = []
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if 'outcome_plans' in statement.lower() or 'outcome_plan_history' in statement.lower():
            protected_reads.append(statement)
    event.listen(case.database.engine, 'before_cursor_execute', observe)
    try:
        path = f"/api/v1/sessions/{private['id']}/outcome-plan"
        assert client.get(path).status_code == 404
        assert client.put(path, json={'value': {**value, 'focus': 'guided'}},
            headers={**csrf, 'If-Match': '"1"'}).status_code == 404
    finally:
        event.remove(case.database.engine, 'before_cursor_execute', observe)
    assert not protected_reads
    assert stored(case, private['id']) == before
    actor = actor_from_principal(current_browser_principal(replace(case, client=client)))
    assert actor is not None
    with execution_actor_scope(actor):
        service = OutcomePlanService(case.database)
        with pytest.raises(SessionNotFound):
            service.get(private['id'])
        with pytest.raises(SessionNotFound):
            service.update(private['id'], 1, value)


@pytest.mark.parametrize('scope', ['private', 'team'])
def test_owner_and_member_read_defaults_without_writes_then_save_from_revision_zero(plans, scope):
    case, users, private, shared = plans
    record = private if scope == 'private' else shared
    client, csrf = (users.alice, users.ah) if scope == 'private' else (users.bob, users.bh)
    path = f"/api/v1/sessions/{record['id']}/outcome-plan"
    assert stored(case, record['id']) is None
    writes = []
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        sql = statement.strip().upper()
        if sql.startswith(('INSERT ', 'UPDATE ', 'DELETE ')) and ('OUTCOME_PLAN' in sql or 'SESSION_SETTINGS' in sql):
            writes.append(statement)
    event.listen(case.database.engine, 'before_cursor_execute', observe)
    try:
        first = client.get(path)
        second = client.get(path)
        assert first.status_code == second.status_code == 200
        assert first.get_json() == second.get_json()
        assert first.headers['ETag'] == '"0"'
    finally:
        event.remove(case.database.engine, 'before_cursor_execute', observe)
    assert not writes and stored(case, record['id']) is None
    value = {**first.get_json()['value'], 'focus': 'custom'}
    saved = client.put(path, json={'value': value}, headers={**csrf, 'If-Match': '"0"'})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()['revision'] == 1 and saved.headers['ETag'] == '"1"'
    assert client.get(path).get_json()['value'] == value
    stale = client.put(path, json={'value': value}, headers={**csrf, 'If-Match': '"0"'})
    assert stale.status_code == 409 and stored(case, record['id']) == (1, value)


def test_named_viewer_reads_but_cannot_update_or_materialize_a_plan(plans):
    case, users, private, _shared = plans
    viewers = f"/api/v1/projects/{private['project_id']}/viewers"
    revision = body(users.alice.get(viewers))['revision']
    body(users.alice.put(f'{viewers}/{users.bob_id}', json={'expected_revision': revision}, headers=users.ah))
    path = f"/api/v1/sessions/{private['id']}/outcome-plan"
    read = users.bob.get(path)
    assert read.status_code == 200, read.get_json()
    assert read.get_json()['revision'] == 0 and stored(case, private['id']) is None
    denied = users.bob.put(path, json={'value': read.get_json()['value']},
        headers={**users.bh, 'If-Match': '"0"'})
    assert denied.status_code == 403
    assert stored(case, private['id']) is None


def test_readonly_project_credential_cannot_write_outcome_state(plans):
    case, users, private, _shared = plans
    _token, bearer = scoped_token(case, users.alice_id, {'app.read'}, project=private['project_id'])
    path = f"/api/v1/sessions/{private['id']}/outcome-plan"
    read = users.alice.get(path, headers=bearer)
    assert read.status_code == 200 and read.get_json()['revision'] == 0
    denied = users.alice.put(path, json={'value': read.get_json()['value']},
        headers={**bearer, 'If-Match': '"0"'})
    assert denied.status_code == 403 and stored(case, private['id']) is None


def test_cached_actor_loses_outcome_access_when_membership_is_removed(plans):
    case, users, _private, shared = plans
    actor = actor_from_principal(current_browser_principal(replace(case, client=users.bob)))
    assert actor is not None
    with case.database.immediate_session() as session:
        value = derive_legacy_outcome(session.get_one(m.SessionRecord, shared['id']))
        session.delete(session.get_one(WorkspaceMembership, (users.workspace_id, users.bob_id)))
    with execution_actor_scope(actor):
        service = OutcomePlanService(case.database)
        with pytest.raises(SessionNotFound):
            service.get(shared['id'])
        with pytest.raises(SessionNotFound):
            service.update(shared['id'], 0, value)
    assert stored(case, shared['id']) is None
