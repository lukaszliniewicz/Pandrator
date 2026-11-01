"""Existing voice MCP tools through current account/workspace HTTP authority."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from pandrator.web import models as m
from pandrator.web.openapi import build_openapi_document
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.voice_lifecycle import GetVoiceSamplesInput, VoiceCreateInput
from pandrator_mcp.schemas.voice_metadata import UpdateVoiceMetadataInput
from pandrator_mcp.tools.voice_lifecycle import create_voice, get_voice_samples
from pandrator_mcp.tools.voice_metadata import update_voice_metadata
from tests.test_provider_workspace_access import scoped_token
from tests.test_speech_catalogue_workspace_access import (
    case as case,  # noqa: F401
)
from tests.test_speech_catalogue_workspace_access import (
    local_only as local_only,  # noqa: F401
)
from tests.test_speech_catalogue_workspace_access import team


class VoiceRecordHttpClient(ApplicationVoiceMethods):
    def __init__(self, client: Any, csrf: dict[str, str]):
        self.client, self.csrf = client, csrf
        self.last_status = 0

    def _request_json(self, path: str, **options: Any) -> dict[str, Any]:
        headers = dict(self.csrf)
        if options.get("idempotency_key"):
            headers["Idempotency-Key"] = options["idempotency_key"]
        if options.get("if_match_revision") is not None:
            headers["If-Match"] = f'"{options["if_match_revision"]}"'
        response = self.client.open(
            path,
            method=options.get("method", "GET"),
            query_string=options.get("parameters"),
            json=options.get("body"),
            headers=headers,
        )
        self.last_status = response.status_code
        if response.status_code >= 400:
            raise PandratorMcpError("scope_denied", "Disposable HTTP request denied.")
        assert len(response.data) <= options.get("maximum_body_bytes", 512 * 1024)
        result = response.get_json()
        assert isinstance(result, dict)
        return result

    def _request_binary_json(self, path: str, **options: Any) -> dict[str, Any]:
        raise AssertionError("Voice metadata tools must not request binary content.")


def _runtime(application: VoiceRecordHttpClient) -> McpRuntime:
    return cast(McpRuntime, SimpleNamespace(require_application=lambda: application))


def test_mcp_create_edit_and_sample_list_use_actual_voice_owner(case, monkeypatch):
    members = team(case, monkeypatch)
    alice = VoiceRecordHttpClient(members.alice, members.ah)
    bob = VoiceRecordHttpClient(members.bob, members.bh)
    own = create_voice(
        _runtime(alice), VoiceCreateInput(name="Alice private narrator", idempotency_key="crud-own-mcp")
    ).result
    shared = create_voice(
        _runtime(bob),
        VoiceCreateInput(
            name="Shared narrator", workspace_id=members.workspace_id, idempotency_key="crud-team-mcp"
        ),
    )
    assert own["workspace_id"] != members.workspace_id
    assert shared.result["workspace_id"] == members.workspace_id
    assert shared.next_actions[0].arguments == {"workspace_id": members.workspace_id}
    voice_id = shared.result["id"]
    with case.database.immediate_session() as session:
        voice = session.get_one(m.Voice, voice_id)
        voice.metadata_json = {**voice.metadata_json, "private_extension": "SYNTHETIC PRIVATE PATH"}
    update = UpdateVoiceMetadataInput.model_validate({
        "voice_id": voice_id, "expected_revision": shared.result["revision"],
        "changes": {"name": "Shared team lead"},
    })
    edited = update_voice_metadata(_runtime(alice), update)
    assert edited.result["workspace_id"] == members.workspace_id
    assert edited.result["name"] == "Shared team lead"
    assert "SYNTHETIC PRIVATE PATH" not in str(edited.result)
    assert edited.next_actions[0].arguments == {"workspace_id": members.workspace_id}
    samples = get_voice_samples(_runtime(alice), GetVoiceSamplesInput(voice_id=voice_id))
    assert samples["workspace_id"] == members.workspace_id and samples["items"] == []
    with pytest.raises(PandratorMcpError):
        update_voice_metadata(_runtime(alice), update)
    assert alice.last_status == 409
    _, readonly = scoped_token(case, members.alice_id, {"app.read"}, workspace=members.workspace_id)
    read_client = VoiceRecordHttpClient(members.alice, readonly)
    assert get_voice_samples(_runtime(read_client), GetVoiceSamplesInput(voice_id=voice_id))["items"] == []
    with pytest.raises(PandratorMcpError):
        update_voice_metadata(_runtime(read_client), update)
    assert read_client.last_status == 403


def test_mcp_voice_receipt_and_by_id_reads_recheck_current_membership(case, monkeypatch):
    members = team(case, monkeypatch)
    application = VoiceRecordHttpClient(members.alice, members.ah)
    runtime = _runtime(application)
    command = VoiceCreateInput(
        name="Retained team voice", workspace_id=members.workspace_id,
        idempotency_key="crud-team-retry-mcp",
    )
    first = create_voice(runtime, command)
    assert create_voice(runtime, command).result == first.result
    with case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (members.workspace_id, members.alice_id)))
    with pytest.raises(PandratorMcpError):
        create_voice(runtime, command)
    assert application.last_status == 404
    with pytest.raises(PandratorMcpError):
        get_voice_samples(runtime, GetVoiceSamplesInput(voice_id=first.result["id"]))
    assert application.last_status == 404


def test_voice_record_openapi_matches_workspace_query_and_current_scopes():
    paths = build_openapi_document()["paths"]
    for path, method in (
        ("/api/v1/voices", "post"),
        ("/api/v1/voices/{voiceId}", "patch"),
        ("/api/v1/voices/{voiceId}", "delete"),
        ("/api/v1/voices/{voiceId}/samples", "get"),
        ("/api/v1/voices/{voiceId}/samples/{sampleId}", "delete"),
    ):
        operation = paths[path][method]
        roots = {item["name"]: item for item in operation["parameters"]}
        assert roots["workspace_id"]["in"] == "query"
        assert roots["workspace_id"]["schema"]["maxLength"] == 80
        assert "session_id" not in roots
        assert operation["x-required-scopes"] == (["app.read"] if method == "get" else ["app.read", "app.write"])
