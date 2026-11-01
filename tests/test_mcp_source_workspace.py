"""Explicit team source ingress through the MCP builders and real HTTP handlers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp import Client
from sqlalchemy import select

from pandrator.web import models as m
from pandrator_mcp.clients.application_sessions import ApplicationSessionMethods
from pandrator_mcp.clients.application_sources import ApplicationSourceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.e2e import CreateTextSourceInput, ImportLocalSourceInput
from pandrator_mcp.server import build_server
from pandrator_mcp.tools import e2e
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, team
from tests.test_mcp_e2e import _runtime
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_purge_workspace_http import project


def fake_runtime(root: Path) -> tuple[McpRuntime, Any]:
    runtime, application = _runtime(root)
    return cast(McpRuntime, runtime), application


class SourceHttpClient(ApplicationSourceMethods, ApplicationSessionMethods):
    def __init__(self, client: Any, csrf: dict[str, str]):
        self.client, self.csrf = client, csrf
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, **options: Any) -> dict[str, Any]:
        self.calls.append((path, options))
        headers = {**self.csrf, **options.get("extra_headers", {})}
        if options.get("idempotency_key"):
            headers["Idempotency-Key"] = options["idempotency_key"]
        if options.get("if_match_revision") is not None:
            headers["If-Match"] = str(options["if_match_revision"])
        values: dict[str, Any] = {}
        if "content_type" in options:
            values.update(data=options.get("body"), content_type=options["content_type"])
        elif options.get("body") is not None:
            values["json"] = options["body"]
        response = self.client.open(
            path,
            method=options.get("method", "GET"),
            query_string=options.get("parameters"),
            headers=headers,
            **values,
        )
        result = response.get_json()
        assert isinstance(result, dict)
        if response.status_code >= 400:
            raise PandratorMcpError("scope_denied", "Disposable source request denied.")
        return result

    def _request_binary_json(self, path: str, **options: Any) -> dict[str, Any]:
        return self._request_json(path, **options)


def test_text_source_team_scope_reaches_authenticated_http_and_preserves_secondary_role(
    case, monkeypatch
):
    u = team(case, monkeypatch)
    record = project(u.alice, u.ah, u.workspace_id)
    application = SourceHttpClient(u.alice, u.ah)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    result = e2e.create_text_source(
        runtime,
        CreateTextSourceInput(
            session_id=record["id"],
            workspace_id=u.workspace_id,
            text="Shared team text",
            role="reference",
            filename="shared.txt",
            expected_session_revision=record["revision"],
            idempotency_key="mcp-team-source-text",
        ),
    ).result
    assert result["workspace_id"] == u.workspace_id
    with case.database.snapshot_session() as unit:
        asset = unit.get_one(m.SourceAsset, result["source_asset_id"])
        link = unit.get_one(m.SessionSource, result["attachment"]["id"])
        assert asset.workspace_id == u.workspace_id and link.role == "reference"
        assert len(list(unit.scalars(select(m.SessionSource)))) == 1
    shared = body(u.bob.get("/api/v1/sources", query_string={"workspace_id": u.workspace_id}))[
        "items"
    ]
    assert [x["id"] for x in shared] == [result["source_asset_id"]]
    assert [path for path, _ in application.calls][0] == f"/api/v1/sessions/{record['id']}"
    init = next(options for path, options in application.calls if path.endswith("/uploads/init"))
    assert init["body"]["workspace_id"] == u.workspace_id and "session_id" not in init["body"]


@pytest.mark.parametrize("tool", ["text", "local"])
def test_mismatched_team_destination_fails_before_discovery_or_local_file_io(
    tmp_path, monkeypatch, tool
):
    runtime, application = fake_runtime(tmp_path)
    application.get_session = lambda _sid: {"workspace_id": "actual-team"}

    def forbidden(*_args, **_kwargs):
        pytest.fail("Mismatched source destination reached data or local file IO")

    monkeypatch.setattr(application, "list_sources", forbidden)
    monkeypatch.setattr(e2e, "_named_source_root", forbidden)
    arguments = {
        "session_id": "session-1",
        "workspace_id": "other-team",
        "expected_session_revision": 1,
        "idempotency_key": "source-wrong-team",
    }
    with pytest.raises(PandratorMcpError) as rejected:
        if tool == "text":
            e2e.create_text_source(
                runtime, CreateTextSourceInput(text="Not published", **arguments)
            )
        else:
            e2e.import_local_source(
                runtime,
                ImportLocalSourceInput(root="downloads", relative_path="file.txt", **arguments),
            )
    assert rejected.value.code == "validation_error" and application.calls == []


def test_local_import_team_scope_is_forwarded_without_absolute_paths(tmp_path):
    source = tmp_path / "fixture.txt"
    source.write_bytes(b"Team local source")
    runtime, application = fake_runtime(tmp_path)
    discovery = []
    application.get_session = lambda _sid: {"workspace_id": "team"}
    application.list_sources = lambda **kwargs: discovery.append(kwargs) or {"items": []}
    result = e2e.import_local_source(
        runtime,
        ImportLocalSourceInput(
            session_id="session-1",
            workspace_id="team",
            root="downloads",
            relative_path=source.name,
            expected_session_revision=1,
            idempotency_key="team-local-source",
        ),
    ).result
    assert discovery == [{"workspace_id": "team"}]
    assert application.calls[0][0] == "initialize_upload"
    assert application.calls[0][1]["workspace_id"] == "team"
    assert application.uploaded == source.read_bytes() and result["workspace_id"] == "team"
    assert str(tmp_path) not in str(result)


def test_registered_mcp_source_tools_expose_optional_workspace_and_forward_it(tmp_path):
    runtime, application = fake_runtime(tmp_path)
    application.get_session = lambda _sid: {"workspace_id": "team"}
    application.list_sources = lambda **_kwargs: {"items": []}

    async def exercise():
        async with Client(build_server(runtime), mode="auto", raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ("pandrator_create_text_source", "pandrator_import_local_source"):
                schema = tools[name].input_schema
                assert "workspace_id" in schema["properties"]
                assert "workspace_id" not in schema.get("required", [])
            result = await client.call_tool(
                "pandrator_create_text_source",
                {
                    "session_id": "session-1",
                    "workspace_id": "team",
                    "text": "Team source",
                    "expected_session_revision": 1,
                    "idempotency_key": "registered-team-source",
                },
            )
            assert not result.is_error

    asyncio.run(exercise())
    assert (
        next(values for name, values in application.calls if name == "initialize_upload")[
            "workspace_id"
        ]
        == "team"
    )
