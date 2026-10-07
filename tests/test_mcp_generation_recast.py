"""Exact portable recast routes, native signatures and compact receipts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.e2e import PlanExportVariantInput
from pandrator_mcp.schemas.generation import AssembleGenerationRunInput, ListGenerationSegmentsInput
from pandrator_mcp.schemas.generation_recast import (
    PreviewRecastInput,
    PreviewUndoRecastInput,
    RecastVoicesInput,
    UndoRecastInput,
)
from pandrator_mcp.tools.e2e import plan_export_variant
from pandrator_mcp.tools.generation import assemble_generation_run, list_generation_segments
from pandrator_mcp.tools.generation_recast import (
    preview_recast,
    preview_undo_recast,
    recast_voices,
    undo_recast,
)

KEY = "recast:fixture:1"
RUN = {
    "id": "run-1", "session_id": "session/a", "job_id": "job-1", "status": "queued",
    "plan_revision_id": "plan-1", "settings_snapshot": {"private": "omit"},
    "media": {"private": "omit"},
    "audio_version": {"kind": "recast", "target_count": 2, "generated_count": 0,
                      "selectors": [{"key": "narrator", "label": "Narrator"}],
                      "before_available": True, "before_take_ids": {"private": "omit"}},
}


@pytest.mark.parametrize("action,arguments,path,body,key", [
    ("preview", {"session_id": "session/a", "selector_keys": None,
                 "speech_plan_revision_id": None, "force": False, "include_missing": False},
     "/api/v1/sessions/session%2Fa/generation-recast/preview",
     {"selector_keys": None, "speech_plan_revision_id": None, "force": False,
      "include_missing": False}, None),
    ("recast", {"session_id": "session/a", "selector_keys": [],
                "expected_selection_hash": "hash", "idempotency_key": KEY},
     "/api/v1/sessions/session%2Fa/generation-recast",
     {"selector_keys": [], "expected_selection_hash": "hash"}, KEY),
    ("undo_preview", {"generation_run_id": "run/a"},
     "/api/v1/generation-runs/run%2Fa/audio-version/undo-preview", {}, None),
    ("undo", {"generation_run_id": "run/a", "expected_selection_hash": "hash",
              "idempotency_key": KEY},
     "/api/v1/generation-runs/run%2Fa/audio-version/undo",
     {"expected_selection_hash": "hash"}, KEY),
])
def test_exact_allowlisted_post_routes_preserve_null_and_empty_selectors(
    action, arguments, path, body, key
):
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={"ok": True})
    assert client.generation_recast_request(action, arguments) == {"ok": True}
    client._request_json.assert_called_once_with(
        path, method="POST", body=body, idempotency_key=key, maximum_body_bytes=512 * 1024
    )
    with pytest.raises(ValueError):
        client.generation_recast_request("delete", {})


def _runtime(payload):
    application = Mock()
    application.generation_recast_request.return_value = payload
    return SimpleNamespace(require_application=lambda: application), application


def test_previews_keep_compact_metadata_and_bound_segments():
    preview = {
        "speech_plan_revision_id": "plan-1", "selection_hash": "hash", "controls_revision": 4,
        "settings_summary": {"service": "test", "model": "model"},
        "selectors": [{"key": "narrator", "label": "Narrator", "affected_count": 3,
                       "changed_count": 2, "missing_count": 1, "unverified_count": 1,
                       "overridden_count": 0, "error": None}],
        "selected_keys": ["narrator"], "generate_count": 2, "keep_count": 4,
        "missing_count": 1, "shared_segment_count": 1, "blocked_reason": None,
        "segments": [{"id": str(i)} for i in range(80)], "settings_snapshot": {"omit": True},
    }
    runtime, application = _runtime(preview)
    outcome = preview_recast(runtime, PreviewRecastInput(session_id="session-1"))
    assert outcome.result == {"schema_version": "1", **{
        key: value for key, value in preview.items() if key not in {"segments", "settings_snapshot"}
    }, "segments": preview["segments"][:50]}
    assert application.generation_recast_request.call_args.args == (
        "preview", {"session_id": "session-1", "selector_keys": None,
                    "speech_plan_revision_id": None, "force": False, "include_missing": False}
    )
    assert not outcome.next_actions

    undo_preview = {"generation_run_id": "run-1", "speech_plan_revision_id": "plan-1",
                    "selection_hash": "hash", "restore_count": 2, "blocked_reason": None}
    runtime, _ = _runtime(undo_preview)
    outcome = preview_undo_recast(runtime, PreviewUndoRecastInput(generation_run_id="run-1"))
    assert outcome.result == {"schema_version": "1", **undo_preview}


def test_writes_return_compact_run_and_version_receipts_without_another_fetch():
    runtime, application = _runtime(RUN)
    outcome = recast_voices(runtime, RecastVoicesInput(
        session_id="session/a", selector_keys=["narrator"], expected_selection_hash="hash",
        idempotency_key=KEY, include_missing=True,
    ))
    assert outcome.result["id"] == "run-1"
    assert outcome.result["application_link"] == "/sessions/session%2Fa/voice"
    assert outcome.result["audio_version"]["target_count"] == 2
    assert "before_take_ids" not in outcome.result["audio_version"]
    assert "settings_snapshot" not in outcome.result and "media" not in outcome.result
    assert outcome.work.id == "job-1"
    assert not outcome.next_actions
    assert len(application.mock_calls) == 1
    application.generation_recast_request.assert_called_once()

    runtime, application = _runtime({"run": RUN, "restored_count": 2})
    outcome = undo_recast(runtime, UndoRecastInput(
        generation_run_id="run-1", expected_selection_hash="undo-hash", idempotency_key=KEY
    ))
    assert outcome.result["run"]["application_link"] == "/sessions/session%2Fa"
    assert outcome.result["restored_count"] == 2
    assert "settings_snapshot" not in outcome.result["run"]
    assert not outcome.next_actions and outcome.work is None
    application.generation_recast_request.assert_called_once()


@pytest.mark.parametrize("handler,arguments", [
    (recast_voices, RecastVoicesInput(session_id="session-1", expected_selection_hash="hash",
                                    idempotency_key=KEY)),
    (undo_recast, UndoRecastInput(generation_run_id="run-1", expected_selection_hash="hash",
                                idempotency_key=KEY)),
])
def test_api_conflicts_propagate_and_timeout_replay_preserves_exact_arguments(handler, arguments):
    runtime, application = _runtime({})
    conflict = PandratorMcpError("revision_conflict", "Selection changed.")
    application.generation_recast_request.side_effect = conflict
    with pytest.raises(PandratorMcpError) as raised:
        handler(runtime, arguments)
    assert raised.value is conflict and not conflict.next_actions
    timeout = PandratorMcpError("application_response_timeout", "Timed out.")
    application.generation_recast_request.side_effect = timeout
    with pytest.raises(PandratorMcpError):
        handler(runtime, arguments)
    assert timeout.next_actions[0].arguments == arguments.model_dump(mode="json")
    assert len(timeout.next_actions) == 1


@pytest.mark.parametrize("side", ["before", "after"])
def test_historical_reads_and_assemblies_forward_side_and_keep_provenance(side):
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={})
    client.list_generation_segments("session/a", generation_run_id="run-1", audio_version_side=side)
    assert client._request_json.call_args.kwargs["parameters"]["audio_version_side"] == side
    client.create_output_assembly("session/a", generation_run_id="run-1", audio_version_side=side,
                                  idempotency_key=KEY)
    assert client._request_json.call_args.kwargs["body"] == {
        "generation_run_id": "run-1", "audio_version_side": side
    }
    application = Mock()
    segment = {"id": "segment-1", "history_take_id": "take-before",
               "takes": [{"id": "take-before", "rendered_voices": [{"scope": "narrator"}]}]}
    application.list_generation_segments.return_value = {"items": [segment]}
    application.create_output_assembly.return_value = {"id": "assembly-1", "status": "queued"}
    runtime = SimpleNamespace(require_application=lambda: application)
    for view in ("full", "compact"):
        result = list_generation_segments(runtime, ListGenerationSegmentsInput(
            session_id="session-1", generation_run_id="run-1", audio_version_side=side, view=view
        ))
        assert result["items"][0]["history_take_id"] == "take-before"
        assert result["items"][0]["takes"][0]["rendered_voices"] == [{"scope": "narrator"}]
        assert application.list_generation_segments.call_args.kwargs["audio_version_side"] == side
    outcome = assemble_generation_run(runtime, AssembleGenerationRunInput(
        session_id="session-1", generation_run_id="run-1", audio_version_side=side,
        idempotency_key=KEY
    ))
    assert application.create_output_assembly.call_args.kwargs["audio_version_side"] == side
    assert outcome.result["audio_version_side"] == side


def test_portable_inputs_reject_bad_guards_and_preserve_null_defaults():
    assert PreviewRecastInput(session_id="s").selector_keys is None
    assert PreviewRecastInput(session_id="s", selector_keys=[]).selector_keys == []
    with pytest.raises(ValidationError):
        PreviewRecastInput(session_id="s", selector_keys=["narrator"] * 1001)
    with pytest.raises(ValidationError):
        RecastVoicesInput(session_id="s", expected_selection_hash="", idempotency_key=KEY)
    with pytest.raises(ValidationError):
        UndoRecastInput(generation_run_id="r", expected_selection_hash="hash", idempotency_key="bad")
    with pytest.raises(ValidationError):
        ListGenerationSegmentsInput(session_id="s", audio_version_side="invalid")


@pytest.mark.parametrize("side", ["before", "after", None])
def test_export_variant_forwards_explicit_and_default_version_side(side):
    arguments = {"session_id": "session-1", "generation_run_id": "run-1"}
    if side is not None:
        arguments["audio_version_side"] = side
    application = Mock()
    application.create_workflow_plan.return_value = {"id": "plan-1"}
    runtime = SimpleNamespace(require_application=lambda: application)
    assert plan_export_variant(runtime, PlanExportVariantInput(**arguments)) == {"id": "plan-1"}
    application.create_workflow_plan.assert_called_once_with(
        "session-1", target_stage="export", overrides={"output": {
            "generation_run_id": "run-1", "audio_version_side": side or "after",
            "export_mode": "media", "audio_mode": "mixed", "subtitle_mode": "none",
            "subtitle_selection": "translation", "subtitle_format": "srt",
        }}, expires_in_minutes=30, continuation=False,
    )


def test_native_transport_exposes_flat_schemas_preserves_nulls_and_forwards_sides():
    from mcp import Client

    from pandrator_mcp.server import build_server

    async def exercise():
        runtime, application = _runtime(RUN)
        application.list_generation_segments.return_value = {"items": []}
        application.create_output_assembly.return_value = {"id": "assembly-1", "status": "queued"}
        application.create_workflow_plan.return_value = {"id": "export-plan-1"}
        async with Client(build_server(runtime), mode="auto", raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ("pandrator_preview_recast", "pandrator_recast_voices",
                         "pandrator_preview_undo_recast", "pandrator_undo_recast"):
                assert "arguments" not in tools[name].input_schema["properties"]
                spec = ACTION_CATALOG.get(name)
                assert spec.risk == (RiskClass.READ if "preview" in name else RiskClass.WRITE)
                assert spec.requires_idempotency == ("preview" not in name)
            props = tools["pandrator_preview_recast"].input_schema["properties"]
            assert {branch["type"] for branch in props["selector_keys"]["anyOf"]} == {"array", "null"}
            for selected in (None, [], ["narrator"]):
                result = await client.call_tool("pandrator_recast_voices", {
                    "session_id": "session/a", "selector_keys": selected,
                    "speech_plan_revision_id": None, "expected_selection_hash": "hash",
                    "idempotency_key": KEY,
                })
                assert not result.is_error
                assert application.generation_recast_request.call_args.args[1]["selector_keys"] == selected
            for name in ("pandrator_list_generation_segments", "pandrator_assemble_generation_run"):
                props = tools[name].input_schema["properties"]
                assert props["audio_version_side"]["enum"] == ["before", "after"]
                for side in ("before", "after"):
                    args = {"session_id": "session/a", "generation_run_id": "run-1",
                            "audio_version_side": side}
                    if "assemble" in name:
                        args["idempotency_key"] = KEY
                    result = await client.call_tool(name, args)
                    assert not result.is_error
                    method = (application.create_output_assembly if "assemble" in name
                              else application.list_generation_segments)
                    assert method.call_args.kwargs["audio_version_side"] == side

            export_schema = tools["pandrator_plan_export_variant"].input_schema["properties"]
            assert export_schema["audio_version_side"]["enum"] == ["before", "after"]
            assert export_schema["audio_version_side"]["default"] == "after"
            for side in ("before", "after", None):
                args = {"session_id": "session/a", "generation_run_id": "run-1"}
                if side is not None:
                    args["audio_version_side"] = side
                result = await client.call_tool("pandrator_plan_export_variant", args)
                assert not result.is_error
                output = application.create_workflow_plan.call_args.kwargs["overrides"]["output"]
                assert output["generation_run_id"] == "run-1"
                assert output["audio_version_side"] == (side or "after")

    asyncio.run(exercise())
