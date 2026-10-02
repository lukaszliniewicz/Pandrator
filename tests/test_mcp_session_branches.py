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
        source_settings = fixture.client.put(
            f"/api/v1/sessions/{fixture.record['id']}/settings/subtitles",
            json={"value": {"language_defaults": False, "max_chars_per_line": 42, "max_cps": 15}},
            headers={**fixture.headers, "If-Match": "0"},
        )
        assert source_settings.status_code == 200, source_settings.get_json()

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
                    "pandrator_preview_translation_project_operation",
                    "pandrator_get_translation_project_operation",
                    "pandrator_execute_translation_project_operation",
                    "pandrator_cancel_translation_project_operation",
                    "pandrator_retry_translation_project_operation_preview",
                    "pandrator_get_translation_project_export_manifest",
                    "pandrator_request_translation_project_export_bundle",
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
                    "targets": [{"target_language": "de"}, {"target_language": "ja",
                                "carry_source_subtitle_settings": True}],
                    "idempotency_key": "mcp-branches-test-001",
                }
                project = (await call("pandrator_create_translation_branches", args))["project"]
                branches = project["branches"]
                assert len(branches) == 2
                assert len({branch["session_id"] for branch in branches}) == 2
                for branch in branches:
                    settings = fixture.client.get(
                        f"/api/v1/sessions/{branch['session_id']}/settings/subtitles"
                    ).get_json()
                    carry = branch["target_language"] == "ja"
                    assert settings["effective"]["language_defaults"] is (not carry)
                    limits = settings["subtitle_profiles"]["target"]["limits"]
                    assert limits["max_chars_per_line"]["effective"] == (42 if carry else 60)
                    assert limits["max_chars_per_second"]["effective"] == (15 if carry else 20)
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


@pytest.mark.parametrize("value", ["true", 1, None])
def test_carry_subtitle_settings_is_strict_across_api_and_mcp(value):
    from pydantic import ValidationError

    from pandrator.web.multilingual_setup import MultilingualSetup
    from pandrator.web.translation_project_routes import TranslationBranchTarget
    from pandrator_mcp.schemas.session_branches import TranslationBranchTargetInput
    from pandrator_mcp.schemas.sessions import MultilingualSetup as McpMultilingualSetup

    for model in (TranslationBranchTarget, TranslationBranchTargetInput):
        assert model(target_language="ja").carry_source_subtitle_settings is False
        assert model(target_language="ja", carry_source_subtitle_settings=True).carry_source_subtitle_settings is True
        with pytest.raises(ValidationError):
            model(target_language="ja", carry_source_subtitle_settings=value)
    for model in (MultilingualSetup, McpMultilingualSetup):
        with pytest.raises(ValidationError):
            model(target_languages=["ja"], carry_source_subtitle_settings=value)


@pytest.mark.parametrize("payload", [
    {"target_languages": [" PT_br ", "ZH_hant_TW"]},
    {"target_languages": [f"en-{index}" for index in range(20)]},
    {"target_languages": ["aa-" + "bbbbbbbb-" * 4 + "c"],
     "generate_voiceover": True, "keep_source_subtitles": False,
     "carry_source_subtitle_settings": True},
])
def test_mcp_multilingual_dto_matches_backend_wire_shape(payload):
    from pandrator.web.multilingual_setup import MultilingualSetup as BackendMultilingualSetup
    from pandrator_mcp.schemas import MultilingualSetup

    assert MultilingualSetup is not BackendMultilingualSetup
    assert MultilingualSetup.model_json_schema() == BackendMultilingualSetup.model_json_schema()
    assert MultilingualSetup.model_validate(payload).model_dump(mode="json") == (
        BackendMultilingualSetup.model_validate(payload).model_dump(mode="json")
    )


@pytest.mark.parametrize("payload", [
    {"target_languages": []},
    {"target_languages": [f"en-{index}" for index in range(21)]},
    *({"target_languages": [language]} for language in (
        "auto", " AUTO ", "a", "abcdefghi", "en-123456789", "en--us", "en!", " ",
        "aa-" + "bbbbbbbb-" * 4 + "cc", None, 1,
    )),
    {"target_languages": ["pt-br", " PT_BR "]},
    {"target_languages": ["en"], "unexpected": True},
    *({"target_languages": ["en"], field: value}
      for field in ("generate_voiceover", "keep_source_subtitles", "carry_source_subtitle_settings")
      for value in ("true", 1, 0, None)),
])
def test_mcp_multilingual_dto_rejects_backend_invalid_inputs(payload):
    from pydantic import ValidationError

    from pandrator.web.multilingual_setup import MultilingualSetup as BackendMultilingualSetup
    from pandrator_mcp.schemas import MultilingualSetup

    for model in (MultilingualSetup, BackendMultilingualSetup):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


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


def test_translation_project_operation_inputs_are_strict_and_bounded():
    from pydantic import ValidationError

    from pandrator_mcp.schemas import TOOL_INPUT_MODELS
    from pandrator_mcp.schemas.session_branches import (
        CancelTranslationProjectOperationInput,
        ExecuteTranslationProjectOperationInput,
        GetTranslationProjectOperationInput,
        PreviewTranslationProjectOperationInput,
        RetryTranslationProjectOperationPreviewInput,
    )

    valid_preview = {
        "project_id": "project-1",
        "selected_branch_ids": [f"branch-{index}" for index in range(20)],
        "expected_project_revision": 3,
        "action": "export",
        "export_kind": "subtitles",
        "idempotency_key": "preview-project-operation-001",
    }
    parsed = PreviewTranslationProjectOperationInput.model_validate(valid_preview)
    assert parsed.export_kind == "subtitles"
    assert len(parsed.selected_branch_ids) == 20
    assert len(
        [model for model in TOOL_INPUT_MODELS if model.__name__ == "PreviewTranslationProjectOperationInput"]
    ) == 1

    for invalid in (
        {**valid_preview, "selected_branch_ids": []},
        {**valid_preview, "selected_branch_ids": [f"branch-{index}" for index in range(21)]},
        {**valid_preview, "selected_branch_ids": ["branch-1", "branch-1"]},
        {**valid_preview, "selected_branch_ids": ["branch-1", 2]},
        {**valid_preview, "expected_project_revision": True},
        {**valid_preview, "action": "translate-and-generate"},
        {**valid_preview, "export_kind": "all"},
        {**valid_preview, "target_identity": {"instance_id": "caller-controlled"}},
        {**valid_preview, "principal_subject": "caller-controlled"},
        {**valid_preview, "unexpected": True},
    ):
        with pytest.raises(ValidationError):
            PreviewTranslationProjectOperationInput.model_validate(invalid)

    digest = "a" * 64
    execute_input = {
        "operation_id": "operation-1",
        "preview_digest": digest,
        "idempotency_key": "execute-project-operation-001",
    }
    execute = ExecuteTranslationProjectOperationInput.model_validate(execute_input)
    assert execute.accepted_confirmations == []
    with pytest.raises(ValidationError):
        ExecuteTranslationProjectOperationInput.model_validate(
            {**execute_input, "required_confirmations": ["external_provider"]}
        )
    for model, payload in (
        (GetTranslationProjectOperationInput, {"operation_id": "operation-1"}),
        (
            CancelTranslationProjectOperationInput,
            {"operation_id": "operation-1", "idempotency_key": "cancel-project-op-001"},
        ),
        (
            RetryTranslationProjectOperationPreviewInput,
            {
                "operation_id": "operation-1",
                "expected_project_revision": 3,
                "idempotency_key": "retry-project-op-001",
            },
        ),
    ):
        assert model.model_validate(payload)
    for invalid_digest in ("A" * 64, "a" * 63, "g" * 64):
        with pytest.raises(ValidationError):
            ExecuteTranslationProjectOperationInput.model_validate(
                {
                    "operation_id": "operation-1",
                    "preview_digest": invalid_digest,
                    "idempotency_key": "execute-project-operation-001",
                }
            )


def test_operation_handlers_forward_explicit_confirmations_and_full_results():
    from unittest.mock import Mock

    from pandrator_mcp.schemas.session_branches import (
        ExecuteTranslationProjectOperationInput,
        GetTranslationProjectOperationInput,
    )
    from pandrator_mcp.tools.session_branches import (
        execute_translation_project_operation,
        get_translation_project_operation,
    )

    operation = {
        "id": "operation-1",
        "status": "awaiting_agent",
        "children": [
            {
                "branch_id": "branch-1",
                "state": "awaiting_agent",
                "dispatch_run_id": "dispatch-1",
                "manual_resume_required": True,
                "result": {
                    "artifact_id": "artifact-1",
                    "content_hash": "abc",
                    "download_url": "/api/v1/artifacts/artifact-1/content",
                },
            }
        ],
    }
    application = Mock()
    application.execute_translation_project_operation.return_value = operation
    application.get_translation_project_operation.return_value = operation
    runtime = Mock()
    runtime.require_application.return_value = application

    outcome = execute_translation_project_operation(
        runtime,
        ExecuteTranslationProjectOperationInput(
            operation_id="operation-1",
            preview_digest="a" * 64,
            accepted_confirmations=[],
            idempotency_key="execute-project-operation-001",
        ),
    )
    assert outcome.result == {"schema_version": "1", **operation}
    assert application.execute_translation_project_operation.call_args.kwargs[
        "accepted_confirmations"
    ] == []

    read = get_translation_project_operation(
        runtime, GetTranslationProjectOperationInput(operation_id="operation-1")
    )
    assert read.result == {"schema_version": "1", **operation}
    assert read.result["children"][0]["manual_resume_required"] is True
    assert read.result["children"][0]["result"]["download_url"].endswith("/content")


def test_operation_client_uses_fixed_http_paths_and_idempotency_headers():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={"id": "operation-1"})

    client.preview_translation_project_operation(
        "project/a",
        selected_branch_ids=["branch-1", "branch-2"],
        expected_project_revision=4,
        action="generate",
        export_kind="configured",
        idempotency_key="preview-project-operation-001",
    )
    call = client._request_json.call_args
    assert call.args == ("/api/v1/translation-projects/project%2Fa/operations/preview",)
    assert call.kwargs == {
        "method": "POST",
        "body": {
            "selected_branch_ids": ["branch-1", "branch-2"],
            "expected_project_revision": 4,
            "action": "generate",
            "export_kind": "configured",
        },
        "idempotency_key": "preview-project-operation-001",
    }

    client.get_translation_project_operation("operation/a")
    assert client._request_json.call_args.args == (
        "/api/v1/translation-project-operations/operation%2Fa",
    )
    assert client._request_json.call_args.kwargs == {}

    client.execute_translation_project_operation(
        "operation/a",
        preview_digest="a" * 64,
        accepted_confirmations=["external_provider"],
        idempotency_key="execute-project-operation-001",
    )
    assert client._request_json.call_args.args == (
        "/api/v1/translation-project-operations/operation%2Fa/execute",
    )
    assert client._request_json.call_args.kwargs == {
        "method": "POST",
        "body": {
            "preview_digest": "a" * 64,
            "accepted_confirmations": ["external_provider"],
        },
        "idempotency_key": "execute-project-operation-001",
    }

    client.cancel_translation_project_operation(
        "operation/a", idempotency_key="cancel-project-operation-001"
    )
    assert client._request_json.call_args.args == (
        "/api/v1/translation-project-operations/operation%2Fa/cancel",
    )
    assert client._request_json.call_args.kwargs == {
        "method": "POST",
        "body": {},
        "idempotency_key": "cancel-project-operation-001",
    }

    client.retry_translation_project_operation_preview(
        "operation/a",
        expected_project_revision=5,
        idempotency_key="retry-project-operation-001",
    )
    assert client._request_json.call_args.args == (
        "/api/v1/translation-project-operations/operation%2Fa/retry-preview",
    )
    assert client._request_json.call_args.kwargs == {
        "method": "POST",
        "body": {"expected_project_revision": 5},
        "idempotency_key": "retry-project-operation-001",
    }


def test_export_manifest_and_bundle_client_use_fixed_paths_and_idempotency():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={})

    client.get_translation_project_export_manifest("operation/a")
    assert client._request_json.call_args.args == (
        "/api/v1/translation-project-operations/operation%2Fa/exports/manifest",
    )
    assert client._request_json.call_args.kwargs == {}

    client.request_translation_project_export_bundle(
        "operation/a",
        expected_manifest_digest="a" * 64,
        idempotency_key="request-export-bundle-001",
    )
    assert client._request_json.call_args.args == (
        "/api/v1/translation-project-operations/operation%2Fa/exports/bundle",
    )
    assert client._request_json.call_args.kwargs == {
        "method": "POST",
        "body": {"expected_manifest_digest": "a" * 64},
        "idempotency_key": "request-export-bundle-001",
    }


def test_translation_project_export_inputs_are_strict_and_bounded():
    from pydantic import ValidationError

    from pandrator_mcp.schemas.session_branches import (
        GetTranslationProjectExportManifestInput,
        RequestTranslationProjectExportBundleInput,
    )

    assert GetTranslationProjectExportManifestInput(operation_id="operation-1")
    for invalid in (
        {"operation_id": ""},
        {"operation_id": "operation-1", "principal_subject": "caller"},
        {"operation_id": "operation-1", "target_identity": {"instance_id": "caller"}},
    ):
        with pytest.raises(ValidationError):
            GetTranslationProjectExportManifestInput.model_validate(invalid)

    valid = {
        "operation_id": "operation-1",
        "expected_manifest_digest": "a" * 64,
        "idempotency_key": "request-export-bundle-001",
    }
    assert RequestTranslationProjectExportBundleInput.model_validate(valid)
    for invalid in (
        {**valid, "expected_manifest_digest": "A" * 64},
        {**valid, "expected_manifest_digest": "a" * 63},
        {**valid, "expected_manifest_digest": "g" * 64},
        {**valid, "idempotency_key": "short"},
        {**valid, "idempotency_key": "k" * 201},
        {**valid, "idempotency_key": "bad key!"},
        {**valid, "principal_subject": "caller"},
        {**valid, "target_identity": {"instance_id": "caller"}},
        {key: value for key, value in valid.items() if key != "idempotency_key"},
    ):
        with pytest.raises(ValidationError):
            RequestTranslationProjectExportBundleInput.model_validate(invalid)


def test_export_tools_preserve_full_manifest_and_bundle_job_links(tmp_path):
    manifest = {
        "operation_id": "operation-1",
        "complete": False,
        "manifest_digest": "a" * 64,
        "branches": [{"branch_id": "branch-1", "state": "incomplete", "artifacts": []}],
    }
    bundle = {
        "operation_id": "operation-1",
        "status": "queued",
        "job": {"id": "job-1", "status_url": "/api/v1/jobs/job-1"},
        "links": {"operation": "/api/v1/translation-project-operations/operation-1"},
    }
    application = Mock()
    application.get_translation_project_export_manifest.return_value = manifest
    application.request_translation_project_export_bundle.return_value = bundle
    runtime = build_runtime(
        McpSettings(target_name="unconfigured", configuration_path=tmp_path / "absent.json")
    )
    runtime.startup_error = None
    runtime.application = application

    async def exercise():
        async with Client(build_server(runtime), raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            manifest_tool = tools["pandrator_get_translation_project_export_manifest"]
            bundle_tool = tools["pandrator_request_translation_project_export_bundle"]
            assert manifest_tool.annotations is not None
            assert manifest_tool.annotations.read_only_hint is True
            assert bundle_tool.annotations is not None
            assert bundle_tool.annotations.read_only_hint is False

            result = await client.call_tool(
                "pandrator_get_translation_project_export_manifest",
                {"operation_id": "operation-1"},
            )
            assert result.structured_content["result"] == {"schema_version": "1", **manifest}

            result = await client.call_tool(
                "pandrator_request_translation_project_export_bundle",
                {
                    "operation_id": "operation-1",
                    "expected_manifest_digest": "a" * 64,
                    "idempotency_key": "request-export-bundle-001",
                },
            )
            assert result.structured_content["result"] == {"schema_version": "1", **bundle}

    asyncio.run(exercise())
    application.get_translation_project_export_manifest.assert_called_once_with("operation-1")
    application.request_translation_project_export_bundle.assert_called_once_with(
        "operation-1",
        expected_manifest_digest="a" * 64,
        idempotency_key="request-export-bundle-001",
    )


def test_project_operation_tools_register_and_mcp_import_stays_independent(tmp_path):
    import subprocess
    import sys

    from pandrator_mcp.schemas import TOOL_INPUT_MODELS
    from pandrator_mcp.schemas.session_branches import (
        CancelTranslationProjectOperationInput,
        ExecuteTranslationProjectOperationInput,
        GetTranslationProjectExportManifestInput,
        GetTranslationProjectOperationInput,
        PreviewTranslationProjectOperationInput,
        RequestTranslationProjectExportBundleInput,
        RetryTranslationProjectOperationPreviewInput,
    )

    expected = {
        "pandrator_preview_translation_project_operation": PreviewTranslationProjectOperationInput,
        "pandrator_get_translation_project_operation": GetTranslationProjectOperationInput,
        "pandrator_execute_translation_project_operation": ExecuteTranslationProjectOperationInput,
        "pandrator_cancel_translation_project_operation": CancelTranslationProjectOperationInput,
        "pandrator_retry_translation_project_operation_preview": RetryTranslationProjectOperationPreviewInput,
        "pandrator_get_translation_project_export_manifest": GetTranslationProjectExportManifestInput,
        "pandrator_request_translation_project_export_bundle": RequestTranslationProjectExportBundleInput,
    }
    registered_models = {model.__name__ for model in TOOL_INPUT_MODELS}
    assert {model.__name__ for model in expected.values()} <= registered_models

    runtime = build_runtime(
        McpSettings(target_name="unconfigured", configuration_path=tmp_path / "absent.json")
    )
    runtime.startup_error = None

    async def list_tools():
        async with Client(build_server(runtime), raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(expected) <= set(tools)
            for name, model in expected.items():
                action = ACTION_CATALOG.get(name)
                tool = tools[name]
                assert action.input_model == model.__name__
                assert tool.annotations is not None
                assert tool.annotations.read_only_hint == (not action.mutating)
                assert set(tool.input_schema.get("properties", {})).isdisjoint(
                    {"principal", "principal_subject", "target", "target_identity", "instance_id"}
                )

    asyncio.run(list_tools())

    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import pandrator_mcp.tools.session_branches; "
            "assert not any(name == 'pandrator.web' or name.startswith('pandrator.web.') "
            "for name in sys.modules)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr
