"""Exercise native MCP language branches against the real disposable API."""

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator
from mcp import Client

from pandrator_mcp.catalog import ACTION_CATALOG
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.context import build_runtime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from tests import test_web_session_forks as fork_fixture


def test_native_mcp_fork_and_parallel_language_runs(tmp_path: Path):
    fixture = fork_fixture.WebSessionForkTests()
    fixture.setUp()
    try:

        def request_json(
            path,
            *,
            method="GET",
            body=None,
            idempotency_key=None,
            if_match_revision=None,
            **_kwargs,
        ):
            headers = dict(fixture.headers)
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            if if_match_revision is not None:
                headers["If-Match"] = str(if_match_revision)
            response = fixture.client.open(path, method=method, json=body, headers=headers)
            data = response.get_json()
            if response.status_code >= 400:
                error = data["error"]
                raise PandratorMcpError(error["code"], error["message"])
            return data

        application = object.__new__(ApplicationClient)
        application._request_json = request_json
        runtime = build_runtime(
            McpSettings(target_name="unconfigured", configuration_path=tmp_path / "absent.json")
        )
        runtime.startup_error = None
        runtime.application = application

        async def exercise():
            async with Client(build_server(runtime), raise_exceptions=True) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                for name in (
                    "pandrator_fork_session",
                    "pandrator_create_translation_project",
                    "pandrator_create_translation_branches",
                    "pandrator_get_translation_project",
                ):
                    assert name in tools
                    action = ACTION_CATALOG.get(name)
                    annotations = tools[name].annotations
                    assert annotations is not None
                    assert annotations.read_only_hint == (not action.mutating)

                async def call(name, args):
                    Draft202012Validator(tools[name].input_schema).validate(args)
                    result = await client.call_tool(name, args)
                    assert not result.is_error
                    return result.structured_content["result"]

                fork = await call(
                    "pandrator_fork_session",
                    {
                        "session_id": fixture.record["id"],
                        "checkpoint_artifact_id": fixture.correction.id,
                        "expected_revision": fixture.record["revision"],
                        "idempotency_key": "mcp-fork-test-001",
                        "target_language": "ja",
                    },
                )
                assert fork["target_language"] == "ja"
                assert fork["checkpoint_artifact_id"] != fixture.correction.id
                assert "storage_key" not in fork
                project_args = {
                    "session_id": fixture.record["id"],
                    "checkpoint_artifact_id": fixture.correction.id,
                    "expected_revision": fixture.record["revision"],
                    "idempotency_key": "mcp-project-test-001",
                }
                project = (await call("pandrator_create_translation_project", project_args))[
                    "project"
                ]
                assert (await call("pandrator_create_translation_project", project_args))[
                    "project"
                ]["id"] == project["id"]
                args = {
                    "project_id": project["id"],
                    "expected_revision": project["revision"],
                    "targets": [{"target_language": "de"}, {"target_language": "ja"}],
                    "idempotency_key": "mcp-branches-test-001",
                }
                project = (await call("pandrator_create_translation_branches", args))["project"]
                branches = project["branches"]
                assert len(branches) == 2
                assert len({branch["session_id"] for branch in branches}) == 2
                # Both independent native passive runs remain claimable at once.
                runs = []
                for branch in branches:
                    runs.append(
                        await call(
                            "pandrator_create_dispatch_run",
                            {
                                "session_id": branch["session_id"],
                                "kind": "translation",
                                "source_artifact_id": branch["source_checkpoint_artifact_id"],
                                "source_language": "en",
                                "target_language": branch["target_language"],
                                "idempotency_key": f"mcp-run-{branch['target_language']}-001",
                            },
                        )
                    )
                packets = []
                for run in runs:
                    packets.append(
                        await call(
                            "pandrator_claim_dispatch_batch",
                            {
                                "run_id": run["id"],
                                "idempotency_key": f"claim-{run['id']}",
                            },
                        )
                    )
                assert packets[0]["batch_id"] != packets[1]["batch_id"]
                assert packets[0]["lease_token"] != packets[1]["lease_token"]
                status = (
                    await call(
                        "pandrator_get_translation_project",
                        {
                            "session_id": branches[0]["session_id"],
                        },
                    )
                )["project"]
                assert all(b["translation_status"] == "running" for b in status["branches"])
                first = packets[0]
                await call(
                    "pandrator_submit_dispatch_batch",
                    {
                        "batch_id": first["batch_id"],
                        "lease_token": first["lease_token"],
                        "idempotency_key": "submit-first-language-001",
                        "result": {
                            "kind": "translation",
                            "translations": [
                                {"cue_id": cue["cue_id"], "text": "Hallo!"}
                                for cue in first["batch"]["cues"]
                            ],
                        },
                    },
                )
                status = (
                    await call(
                        "pandrator_get_translation_project",
                        {
                            "session_id": fixture.record["id"],
                        },
                    )
                )["project"]
                assert {b["translation_status"] for b in status["branches"]} == {
                    "completed",
                    "running",
                }
                second = packets[1]
                await call(
                    "pandrator_submit_dispatch_batch",
                    {
                        "batch_id": second["batch_id"],
                        "lease_token": second["lease_token"],
                        "idempotency_key": "submit-second-language-001",
                        "result": {
                            "kind": "translation",
                            "translations": [
                                {"cue_id": cue["cue_id"], "text": "こんにちは。"}
                                for cue in second["batch"]["cues"]
                            ],
                        },
                    },
                )
                assert all(
                    b["translation_status"] == "completed"
                    for b in (
                        await call(
                            "pandrator_get_translation_project",
                            {"session_id": fixture.record["id"]},
                        )
                    )["project"]["branches"]
                )

        asyncio.run(exercise())
    finally:
        fixture.tearDown()


def test_client_preserves_fork_revision_and_batch_idempotency():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={})
    client.fork_session(
        "session/a",
        checkpoint_artifact_id="cp",
        expected_revision=7,
        idempotency_key="fork-client-test",
        carry_media_assets=False,
    )
    assert client._request_json.call_args.args == ("/api/v1/sessions/session%2Fa/forks",)
    assert client._request_json.call_args.kwargs["body"] == {
        "checkpoint_artifact_id": "cp",
        "expected_revision": 7,
        "carry_media_assets": False,
    }
    client.create_translation_branches(
        "project/a",
        expected_revision=3,
        targets=[{"target_language": "ja"}],
        idempotency_key="branches-client-test",
    )
    assert client._request_json.call_args.args == (
        "/api/v1/translation-projects/project%2Fa/branches",
    )
    assert client._request_json.call_args.kwargs["idempotency_key"] == "branches-client-test"


def test_mcp_fork_requires_revision_and_bounded_targets():
    from pydantic import ValidationError

    from pandrator_mcp.schemas.session_branches import (
        CreateTranslationBranchesInput,
        ForkSessionInput,
    )

    with pytest.raises(ValidationError):
        ForkSessionInput.model_validate(
            {"session_id": "s", "checkpoint_artifact_id": "cp", "idempotency_key": "fork-test-001"}
        )
    with pytest.raises(ValidationError):
        CreateTranslationBranchesInput(
            project_id="p", expected_revision=1, targets=[], idempotency_key="project-test-001"
        )
