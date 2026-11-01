"""Two-person collaboration across real HTTP adapters, with private data fencing."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from authlib.oauth2.rfc7636 import create_s256_code_challenge
from sqlalchemy import func, select

from pandrator.web.accounts import installation_mode
from pandrator.web.models import AppSetting, ProjectMembership
from pandrator.web.workspace_schemas import (
    WorkspaceInvitationResponse,
    WorkspaceListResponse,
    WorkspaceMembersResponse,
)
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import PASSWORD, enable, new_account


def sign_in(case, login):
    client = case.app.test_client()
    response = client.post('/api/v1/auth/login', json={'login_name': login, 'password': PASSWORD})
    assert response.status_code == 200, response.get_json()
    return client, {'X-CSRF-Token': response.get_json()['csrf_token']}


def create_workspace(case, name='Translation team'):
    response = case.client.post('/api/v1/workspaces', json={'name': name}, headers={'X-CSRF-Token': case.csrf, 'Idempotency-Key': 'workspace-create-test'})
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def invite(case, workspace, user, role='member'):
    response = case.client.post(f'/api/v1/workspaces/{workspace["id"]}/invitations', json={'user_id': user.user_id, 'role': role, 'expected_revision': workspace['revision']}, headers={'X-CSRF-Token': case.csrf, 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_two_people_share_content_and_workspace_survives_creator_departure(case, monkeypatch):
    peer = new_account(case)
    enable(case, monkeypatch)
    workspace = create_workspace(case)
    wid = workspace['id']
    client, headers = sign_in(case, 'alice')
    before = client.get('/api/v1/workspaces').get_json()
    WorkspaceListResponse.model_validate(before)
    assert [item['kind'] for item in before['items']] == ['personal']
    assert client.get(f'/api/v1/workspaces/{wid}').status_code == 404
    invitation = invite(case, workspace, peer)
    WorkspaceInvitationResponse.model_validate(invitation)
    inbox = client.get('/api/v1/workspace-invitations').get_json()['items']
    assert [item['id'] for item in inbox] == [invitation['id']]
    assert client.get(f'/api/v1/workspaces/{wid}/members').status_code == 404
    assert client.post(f'/api/v1/workspace-invitations/{invitation["id"]}/accept').status_code == 403
    accepted = client.post(f'/api/v1/workspace-invitations/{invitation["id"]}/accept', headers=headers)
    assert accepted.status_code == 200, accepted.get_json()
    shared = client.get(f'/api/v1/workspaces/{wid}').get_json()
    assert shared['role'] == 'member' and shared['allowed_actions'] == ['read', 'edit']
    roster = client.get(f'/api/v1/workspaces/{wid}/members').get_json()
    WorkspaceMembersResponse.model_validate(roster)
    assert peer.user_id in {item['user_id'] for item in roster['items']}
    assert client.patch(f'/api/v1/workspaces/{wid}', json={'name': 'Not permitted', 'expected_revision': shared['revision']}, headers=headers).status_code == 404
    created = client.post('/api/v1/sessions', json={'name': 'Shared translation', 'workspace_id': wid}, headers={**headers, 'Idempotency-Key': 'member-shared-project'})
    assert created.status_code == 201, created.get_json()
    sid = created.get_json()['id']
    detail = client.get(f'/api/v1/sessions/{sid}').get_json()
    assert detail['workspace_id'] == wid and 'edit' in detail['allowed_actions']
    assert case.client.get(f'/api/v1/sessions?workspace_id={wid}').get_json()['items'][0]['id'] == sid
    folders = client.post(f'/api/v1/project-folders?workspace_id={wid}', json={'name': 'Shared folder'}, headers={**headers, 'Idempotency-Key': 'member-shared-folder'})
    assert folders.status_code == 201 and folders.get_json()['workspace_id'] == wid
    assert client.get('/api/v1/project-folders').get_json()['workspace_id'] != wid
    protected = case.client.post(f'/api/v1/workspaces/{wid}/leave', json={'expected_revision': shared['revision']}, headers={'X-CSRF-Token': case.csrf})
    assert protected.status_code == 409 and protected.get_json()['error']['code'] == 'last_workspace_manager'
    promoted = case.client.patch(f'/api/v1/workspaces/{wid}/members/{peer.user_id}', json={'role': 'manager', 'expected_revision': shared['revision']}, headers={'X-CSRF-Token': case.csrf})
    assert promoted.status_code == 200, promoted.get_json()
    left = case.client.post(f'/api/v1/workspaces/{wid}/leave', json={'expected_revision': promoted.get_json()['revision']}, headers={'X-CSRF-Token': case.csrf})
    assert left.status_code == 200
    assert client.get(f'/api/v1/sessions/{sid}').status_code == 200
    assert case.client.get(f'/api/v1/sessions/{sid}').status_code == 404
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(ProjectMembership)) == 0


def test_create_replay_returns_current_authorized_projection_not_stored_json(case, monkeypatch):
    enable(case, monkeypatch)
    workspace = create_workspace(case)
    wid = workspace['id']
    renamed = case.client.patch(f'/api/v1/workspaces/{wid}', json={'name': 'Current name', 'expected_revision': 1}, headers={'X-CSRF-Token': case.csrf})
    assert renamed.status_code == 200
    replay = case.client.post('/api/v1/workspaces', json={'name': 'Translation team'}, headers={'X-CSRF-Token': case.csrf, 'Idempotency-Key': 'workspace-create-test'})
    assert replay.status_code == 201 and replay.headers['Idempotency-Replayed'] == 'true'
    assert replay.get_json()['name'] == 'Current name' and replay.get_json()['revision'] == 2


def test_named_mcp_enrollment_approves_selected_workspace_and_explicit_enrollment_scope(case, monkeypatch):
    peer = new_account(case)
    enable(case, monkeypatch)
    workspace = create_workspace(case)
    invitation = invite(case, workspace, peer)
    client, headers = sign_in(case, 'alice')
    assert client.post(f'/api/v1/workspace-invitations/{invitation["id"]}/accept', headers=headers).status_code == 200
    verifier = 'x' * 64
    client_id = str(uuid4())
    scopes = ['app.read', 'app.write', 'app.workspaces.enroll']
    authorize = client.get('/api/v1/auth/automation/authorize', query_string={
        'response_type': 'code', 'client_id': client_id, 'client_name': 'Disposable named MCP',
        'redirect_uri': 'http://127.0.0.1:43123/callback', 'scope': ' '.join(scopes),
        'state': 'test-state', 'code_challenge': create_s256_code_challenge(verifier), 'code_challenge_method': 'S256',
    })
    assert authorize.status_code == 200
    html = authorize.get_data(as_text=True)
    assert 'Approve access to these workspaces' in html
    nonce = re.search(r'name="authorization_nonce" value="([^"]+)"', html)
    assert nonce is not None
    approval = client.post('/api/v1/auth/automation/authorize', data={
        'authorization_nonce': nonce.group(1), 'decision': 'approve', 'workspace_id': workspace['id'],
    })
    assert approval.status_code == 302, approval.get_data(as_text=True)
    code = parse_qs(urlsplit(approval.headers['Location']).query)['code'][0]
    token = case.app.test_client().post('/api/v1/auth/automation/token', data={
        'grant_type': 'authorization_code', 'code': code, 'client_id': client_id,
        'redirect_uri': 'http://127.0.0.1:43123/callback', 'code_verifier': verifier,
    })
    assert token.status_code == 200, token.get_json()
    bearer = {'Authorization': f'Bearer {token.get_json()["access_token"]}'}
    api = case.app.test_client()
    visible = api.get('/api/v1/workspaces', headers=bearer)
    assert visible.status_code == 200
    assert [item['id'] for item in visible.get_json()['items']] == [workspace['id']]
    assert api.get('/api/v1/project-folders', headers=bearer).status_code == 404
    assert api.get(f'/api/v1/project-folders?workspace_id={workspace["id"]}', headers=bearer).status_code == 200
    enrolled = api.post('/api/v1/workspaces', json={'name': 'MCP-created team'}, headers={**bearer, 'Idempotency-Key': 'mcp-workspace-create'})
    assert enrolled.status_code == 201, enrolled.get_json()
    assert len(api.get('/api/v1/workspaces', headers=bearer).get_json()['items']) == 2
    assert api.get('/api/v1/admin/users', headers=bearer).status_code == 403


def test_self_credential_routes_hide_other_accounts_and_do_not_escalate_scopes(case, monkeypatch):
    new_account(case)
    enable(case, monkeypatch)
    client, headers = sign_in(case, 'alice')
    owner_token, _raw = case.auth.create_api_token('Owner private sentinel')
    own = client.post('/api/v1/auth/tokens', json={'label': 'Alice only', 'scopes': ['app.read']}, headers=headers)
    assert own.status_code == 201, own.get_json()
    listing = client.get('/api/v1/auth/tokens')
    assert listing.status_code == 200 and [item['id'] for item in listing.get_json()['items']] == [own.get_json()['id']]
    assert 'Owner private sentinel' not in listing.get_data(as_text=True)
    assert client.delete(f'/api/v1/auth/tokens/{owner_token.id}', headers=headers).status_code == 404
    assert client.post('/api/v1/auth/tokens', json={'label': 'Denied', 'scopes': ['app.admin']}, headers=headers).status_code == 403
    assert client.get('/api/v1/automation/local-paths').status_code == 403
    assert client.put('/api/v1/automation/local-paths', json={'source_roots': []}, headers=headers).status_code == 403
    assert client.delete(f'/api/v1/auth/tokens/{own.get_json()["id"]}', headers=headers).status_code == 204


def test_mode_admission_is_independent_of_json_key_order_and_rejects_extra_fields(case, monkeypatch):
    peer = new_account(case)
    enable(case, monkeypatch)
    with case.database.immediate_session() as session:
        setting = session.get(AppSetting, installation_mode.INSTALLATION_POLICY_KEY)
        assert setting is not None
        setting.value_json = {'mode': 'collaboration', 'schema_version': 1}
    assert case.auth.collaboration_enabled()
    client, _headers = sign_in(case, 'alice')
    assert client.get('/api/v1/workspaces').status_code == 200
    with case.database.immediate_session() as session:
        setting = session.get(AppSetting, installation_mode.INSTALLATION_POLICY_KEY)
        assert setting is not None
        setting.value_json = {'schema_version': 1, 'mode': 'collaboration', 'extra': True}
    # Current known account revision comes from the credential record, not a guess.
    from pandrator.web.accounts.auth_models import UserCredential
    with case.database.snapshot_session() as session:
        credential = session.get(UserCredential, peer.user_id)
        assert credential is not None
        revision = credential.auth_revision
    assert not case.auth.solo_access_allowed(peer.user_id, revision)
