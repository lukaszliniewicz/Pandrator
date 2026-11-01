"""Workspace tools keep typed inputs and fixed HTTP paths aligned with OpenAPI."""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from pandrator.web.openapi import build_openapi_document
from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application_workspaces import ApplicationWorkspaceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas import TOOL_INPUT_MODELS
from pandrator_mcp.tools.workspaces import register_workspace_tools
from pandrator_mcp.workspace_actions import WORKSPACE_OPERATIONS


class Recorder(ApplicationWorkspaceMethods):
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def _request_json(self, path, **kwargs):
        self.calls.append({'path': path, **kwargs})
        return {'ok': True}

    def _request_binary_json(self, *_args, **kwargs):
        raise AssertionError('Workspace commands use fixed JSON routes.')


SAMPLES = {
    'workspace_id': 'workspace/one', 'user_id': 'user/one', 'invitation_id': 'invitation/one',
    'name': 'Small team', 'expected_revision': 1, 'role': 'member',
    'idempotency_key': 'workspace-test-key', 'query': 'Al', 'include_archived': True,
    'limit': 50, 'offset': 0,
}


@pytest.mark.parametrize('operation', WORKSPACE_OPERATIONS, ids=lambda operation: operation.action)
def test_every_workspace_adapter_matches_catalog_and_openapi(operation):
    arguments = {name: SAMPLES[name] for name in operation.model.model_fields}
    client = Recorder()
    assert client.workspace_request(operation.action, arguments) == {'ok': True}
    call = client.calls[0]
    assert call['method'] == operation.method
    assert call['read_only_operation'] is (operation.method == 'GET')
    assert call['maximum_body_bytes'] == 16 * 1024
    assert '{' not in call['path'] and not any(part in call['path'] for part in ('workspace/one', 'user/one', 'invitation/one'))
    if operation.idempotency:
        assert call['idempotency_key'] == 'workspace-test-key'
    action = ACTION_CATALOG.get(operation.name)
    assert action.input_model in {model.__name__ for model in TOOL_INPUT_MODELS}
    assert action.required_scope == operation.scope
    assert set(action.required_scopes) == set(operation.required_scopes)
    assert action.risk == (RiskClass.READ if operation.method == 'GET' else RiskClass.WRITE)
    document = build_openapi_document()
    public = document['paths'][action.path][operation.method.lower()]
    assert public['operationId'] == action.downstream_operation_id
    assert set(public['x-required-scopes']) == set(operation.required_scopes)
    assert public['security'][-1] == {'nativeOAuth': list(operation.required_scopes)}


def test_workspace_inputs_reject_extra_fields_invalid_revision_and_unbounded_paths():
    client = Recorder()
    for action, values in (
        ('create_workspace', {'name': 'Team', 'idempotency_key': 'workspace-test-key', 'url': 'https://invalid.example'}),
        ('leave_workspace', {'workspace_id': 'one', 'expected_revision': True}),
        ('invite_workspace_user', {'workspace_id': 'one', 'user_id': 'two', 'role': 'administrator', 'expected_revision': 1, 'idempotency_key': 'workspace-test-key'}),
        ('get_workspace', {'workspace_id': 'x' * 1000}),
        ('enable_collaboration', {'expected_revision': -1}),
        ('list_workspaces_without_manager', {'limit': 101}),
        ('recover_workspace_manager', {'workspace_id': 'one', 'user_id': 'two', 'expected_revision': True}),
    ):
        with pytest.raises(ValidationError):
            client.workspace_request(action, values)
    with pytest.raises(ValueError, match='Unknown workspace action'):
        client.workspace_request('raw_http', {})
    assert client.calls == []


def test_fixed_read_queries_and_empty_invitation_response_body():
    client = Recorder()
    client.workspace_request('search_workspace_users', {'workspace_id': 'one', 'query': 'Al'})
    assert client.calls[-1]['parameters'] == {'q': 'Al'}
    client.workspace_request('list_workspaces', {})
    assert client.calls[-1]['parameters'] == {'include_archived': 'false'}
    client.workspace_request('accept_workspace_invitation', {'invitation_id': 'one'})
    assert client.calls[-1]['body'] == {}


def test_workspace_registration_has_exact_typed_signatures_and_response_modes():
    registered: dict[str, Any] = {}
    calls = []
    class Server:
        def tool(self, **metadata):
            def register(function):
                registered[metadata['name']] = (metadata, function)
                return function
            return register
    application = Recorder()
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    def validate(handler, current, model, values):
        calls.append(model.__name__)
        return {'result': handler(current, model.model_validate(values)).result}
    register_workspace_tools(Server(), runtime, validate, lambda result, mode: (mode, result), read_only='read', write_action='write')
    assert set(registered) == {operation.name for operation in WORKSPACE_OPERATIONS}
    for operation in WORKSPACE_OPERATIONS:
        metadata, function = registered[operation.name]
        signature = inspect.signature(function)
        assert set(signature.parameters) == {*operation.model.model_fields, 'response_mode'}
        assert metadata['annotations'] == ('read' if operation.method == 'GET' else 'write')
        assert metadata['meta']['pandrator/requiredScopes'] == list(operation.required_scopes)
        assert ', '.join(operation.required_scopes) in function.__doc__
    _, create = registered['pandrator_create_workspace']
    mode, result = create(name='Team', idempotency_key='workspace-test-key', response_mode='structured')
    assert mode == 'structured' and result['result'] == {'ok': True}
    with pytest.raises(ValueError):
        create(name='Team', idempotency_key='workspace-test-key', response_mode='raw')
    assert calls == ['CreateWorkspaceInput']


def test_workspace_scope_denial_reports_full_requirements_without_auth_probe():
    registered = {}
    calls = []

    class Server:
        def tool(self, **metadata):
            def register(function):
                registered[metadata['name']] = function
                return function
            return register

    class DeniedApplication:
        def workspace_request(self, action, arguments):
            calls.append((action, arguments))
            raise PandratorMcpError('scope_denied', 'Current authority is required.', details={'status': 403})

        def auth_status(self):
            raise AssertionError('A workspace operation must not add an auth-status request.')

    def validate(handler, current, model, values):
        return handler(current, model.model_validate(values))

    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: DeniedApplication()))
    register_workspace_tools(Server(), runtime, validate, lambda result, mode: result, read_only='read', write_action='write')
    with pytest.raises(PandratorMcpError) as rejected:
        registered['pandrator_create_workspace'](name='Team', idempotency_key='scope-test-key')
    assert calls == [('create_workspace', {'name': 'Team', 'idempotency_key': 'scope-test-key'})]
    assert rejected.value.code == 'scope_denied' and not rejected.value.retryable
    assert rejected.value.details == {
        'status': 403, 'required_scopes': ['app.read', 'app.write', 'app.workspaces.enroll'],
    }
    assert len(rejected.value.next_actions) == 1
    assert rejected.value.next_actions[0].tool == 'pandrator_explain_system'
    assert rejected.value.next_actions[0].arguments == {'topic': 'collaboration', 'detail': 'full'}
