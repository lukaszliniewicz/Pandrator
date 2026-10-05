"""Main native tool DTO validation retains request context and safe failures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from pydantic import create_model, model_validator

import pandrator_mcp.server as adapter
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from tests.test_mcp_generic_dispatch_validation import failure_value
from tests.test_mcp_media_edit_registration import fixture_runtime

SENTINEL = "main DTO validation stdout sentinel"
MARKER = "private-main-validation-fixture-" * 8
KEY = "guard:main:1"

CASES = [
    ("pandrator_explain_system", "ExplainSystemInput", "explain_system", {}),
    ("pandrator_recommend_next_steps", "RecommendNextStepsInput", "recommend_next_steps", {}),
    ("pandrator_get_target_status", "TargetStatusInput", "target_status", {}),
    ("pandrator_get_system_status", "SystemStatusInput", "system_status", {}),
    ("pandrator_get_capabilities", "CapabilitiesInput", "capabilities", {}),
    ("pandrator_browse_local_sources", "BrowseLocalSourcesInput", "browse_local_sources", {}),
    ("pandrator_list_sessions", "ListSessionsInput", "list_sessions", {}),
    ("pandrator_get_session", "GetSessionInput", "get_session", {"session_id": "session-1"}),
    (
        "pandrator_trash_session",
        "TrashSessionInput",
        "trash_session",
        {"session_id": "session-1", "expected_revision": 1},
    ),
    (
        "pandrator_restore_session",
        "RestoreSessionInput",
        "restore_session",
        {"session_id": "session-1", "expected_revision": 1},
    ),
    ("pandrator_get_workflow", "GetWorkflowInput", "get_workflow", {"session_id": "session-1"}),
    (
        "pandrator_get_workflow_inputs",
        "GetWorkflowInputsInput",
        "get_workflow_inputs",
        {"session_id": "session-1"},
    ),
    (
        "pandrator_select_workflow_input",
        "SelectWorkflowInputInput",
        "select_workflow_input",
        {
            "session_id": "session-1",
            "consumer": "generation",
            "role": "source",
            "artifact_id": "artifact-1",
            "expected_outcome_revision": 1,
            "expected_selection_revision": 1,
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_get_subtitle_evidence_routes",
        "GetSubtitleEvidenceRoutesInput",
        "get_subtitle_evidence_routes",
        {},
    ),
    (
        "pandrator_preview_subtitles",
        "PreviewSubtitlesInput",
        "preview_subtitles",
        {"session_id": "session-1"},
    ),
    (
        "pandrator_replace_subtitle_text",
        "ReplaceSubtitleTextInput",
        "replace_subtitle_text",
        {
            "session_id": "session-1",
            "stage": "transcribe",
            "expected_revision": 1,
            "search_text": "Fixture",
            "replacement_text": "Corrected",
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_patch_subtitle_cues",
        "PatchSubtitleCuesInput",
        "patch_subtitle_cues",
        {
            "session_id": "session-1",
            "stage": "transcribe",
            "expected_revision": 1,
            "cues": [{"ordinal": 1, "text": "Fixture"}],
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_import_subtitles",
        "ImportSubtitlesInput",
        "import_subtitles",
        {
            "session_id": "session-1",
            "stage": "transcribe",
            "expected_revision": 1,
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_get_session_settings",
        "GetSessionSettingsInput",
        "get_session_settings",
        {"session_id": "session-1", "section": "text"},
    ),
    (
        "pandrator_describe_parameters",
        "DescribeParametersInput",
        "describe_parameters",
        {"query": "Fixture"},
    ),
    ("pandrator_list_sources", "ListSourcesInput", "list_sources", {}),
    (
        "pandrator_create_text_source",
        "CreateTextSourceInput",
        "create_text_source",
        {
            "session_id": "session-1",
            "text": "Fixture",
            "expected_session_revision": 1,
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_import_local_source",
        "ImportLocalSourceInput",
        "import_local_source",
        {
            "session_id": "session-1",
            "root": "approved",
            "relative_path": "clip.wav",
            "expected_session_revision": 1,
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_request_subtitle_evidence",
        "RequestSubtitleEvidenceInput",
        "request_subtitle_evidence",
        {
            "session_id": "session-1",
            "source_artifact_id": "source-1",
            "cue_id": 1,
            "reason": "Fixture",
            "routes": ["whisper"],
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_get_subtitle_evidence",
        "GetSubtitleEvidenceInput",
        "get_subtitle_evidence",
        {"evidence_id": "evidence-1"},
    ),
    (
        "pandrator_resolve_subtitle_evidence",
        "ResolveSubtitleEvidenceInput",
        "resolve_subtitle_evidence",
        {
            "session_id": "session-1",
            "evidence_id": "evidence-1",
            "action": "dismissed",
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_create_session",
        "CreateSessionInput",
        "create_session",
        {"name": "Fixture", "idempotency_key": "guard:main:1"},
    ),
    (
        "pandrator_update_session",
        "UpdateSessionInput",
        "update_session",
        {
            "session_id": "session-1",
            "expected_revision": 1,
            "idempotency_key": "guard:main:1",
            "name": "Fixture",
        },
    ),
    (
        "pandrator_attach_existing_source",
        "AttachExistingSourceInput",
        "attach_existing_source",
        {
            "session_id": "session-1",
            "source_asset_id": "source-1",
            "expected_session_revision": 1,
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_update_session_settings",
        "UpdateSessionSettingsInput",
        "update_session_settings",
        {
            "session_id": "session-1",
            "section": "text",
            "expected_revision": 1,
            "value": {},
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_patch_session_settings",
        "PatchSessionSettingsInput",
        "patch_session_settings",
        {
            "session_id": "session-1",
            "section": "text",
            "expected_revision": 1,
            "value": {},
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_delete_output",
        "DeleteOutputInput",
        "delete_output",
        {"session_id": "session-1", "artifact_id": "artifact-1"},
    ),
    ("pandrator_plan_workflow", "PlanWorkflowInput", "plan_workflow", {"session_id": "session-1"}),
    (
        "pandrator_plan_orchestrated_workflow",
        "PlanOrchestratedWorkflowInput",
        "plan_orchestrated_workflow",
        {"session_id": "session-1", "goal": "Fixture"},
    ),
    (
        "pandrator_plan_export_variant",
        "PlanExportVariantInput",
        "plan_export_variant",
        {"session_id": "session-1"},
    ),
    (
        "pandrator_execute_workflow_plan",
        "ExecuteWorkflowPlanInput",
        "execute_workflow_plan",
        {
            "plan_id": "fixture",
            "plan_digest": "0000000000000000000000000000000000000000000000000000000000000000",
            "accepted_confirmations": [],
            "idempotency_key": "guard:main:1",
        },
    ),
    ("pandrator_list_artifacts", "ListArtifactsInput", "list_artifacts", {}),
    (
        "pandrator_download_artifact",
        "DownloadArtifactInput",
        "download_artifact",
        {"artifact_id": "artifact-1"},
    ),
    ("pandrator_get_provider_status", "ProviderStatusInput", "provider_status", {}),
    ("pandrator_get_tts_catalog", "TtsCatalogInput", "tts_catalog", {}),
    ("pandrator_get_audio_cpp_catalogue", "AudioCppCatalogueInput", "audio_cpp_catalogue", {}),
    (
        "pandrator_configure_tts",
        "ConfigureTtsInput",
        "configure_tts",
        {
            "session_id": "session-1",
            "service_id": "fixture",
            "expected_revision": 1,
            "idempotency_key": "guard:main:1",
        },
    ),
    ("pandrator_get_voice_catalog", "VoiceCatalogInput", "voice_catalog", {}),
    (
        "pandrator_adopt_subtitle_source",
        "AdoptSubtitleSourceInput",
        "adopt_subtitle_source",
        {
            "session_id": "session-1",
            "source_asset_id": "source-1",
            "idempotency_key": "guard:main:1",
        },
    ),
    ("pandrator_list_work", "ListWorkInput", "list_work", {}),
    ("pandrator_get_work", "GetWorkInput", "get_work", {"work_id": "work-1"}),
    ("pandrator_get_work_log", "GetWorkLogInput", "get_work_log", {"work_id": "work-1"}),
    (
        "pandrator_cancel_work",
        "CancelWorkInput",
        "cancel_work",
        {"work_id": "work-1", "idempotency_key": "guard:main:1"},
    ),
    (
        "pandrator_plan_component_change",
        "PlanComponentChangeInput",
        "plan_component_change",
        {
            "kind": "install",
            "components": [{"component_id": "audio_cpp"}],
            "expected_revision": 1,
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_execute_component_plan",
        "ExecuteComponentPlanInput",
        "execute_component_plan",
        {
            "plan_id": "fixture",
            "plan_digest": "0000000000000000000000000000000000000000000000000000000000000000",
            "accepted_confirmations": [],
            "idempotency_key": "guard:main:1",
        },
    ),
    (
        "pandrator_control_runtime",
        "ControlRuntimeInput",
        "control_runtime",
        {
            "action": "start",
            "runtime_target": "application",
            "service_ids": [],
            "confirmation": "runtime-control",
            "idempotency_key": "guard:main:1",
        },
    ),
]


async def invoke(runtime: Any, tool: str, arguments: dict[str, Any], protocol: str) -> Any:
    async with Client(
        adapter.build_server(runtime), mode=protocol, raise_exceptions=False
    ) as client:
        return await client.call_tool(tool, arguments)


def install_probe(
    monkeypatch: pytest.MonkeyPatch, model: str, observed: list[dict[str, str]]
) -> None:
    @model_validator(mode="before")
    @classmethod
    def probe(cls: Any, value: Any) -> Any:
        print(SENTINEL)
        observed.append(correlation_headers())
        raise ValueError("Controlled main DTO validation failure.")

    original = getattr(adapter, model)
    validators: dict[str, Any] = {"probe": probe}
    controlled = create_model("Controlled" + model, __base__=original, __validators__=validators)
    monkeypatch.setattr(adapter, model, controlled)


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"], ids=["modern", "legacy"])
@pytest.mark.parametrize("tool,model,handler,arguments", CASES, ids=[c[0] for c in CASES])
def test_main_dto_validation_runs_under_request_and_stdout_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    protocol: str,
    tool: str,
    model: str,
    handler: str,
    arguments: dict[str, Any],
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    observed: list[dict[str, str]] = []
    install_probe(monkeypatch, model, observed)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, tool, arguments, protocol))
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert calls == application.mock_calls == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count(SENTINEL) == 1
    failure = failure_value(result, tool)
    assert len(observed) == 1
    assert observed[0]["X-Request-ID"] == failure["request_id"]
    assert observed[0]["traceparent"].startswith("00-")


@pytest.mark.parametrize("tool,model,handler,arguments", CASES, ids=[c[0] for c in CASES])
def test_main_valid_input_reaches_original_handler_with_typed_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool: str,
    model: str,
    handler: str,
    arguments: dict[str, Any],
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    observed: list[Any] = []

    def controlled(current: Any, values: Any) -> dict[str, Any]:
        assert current is runtime and isinstance(values, getattr(adapter, model))
        observed.append((values.model_dump(mode="json"), correlation_headers()))
        print(SENTINEL)
        return {"fixture": "ok"}

    monkeypatch.setattr(adapter, handler, controlled)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, tool, arguments, "2026-07-28"))
    assert not result.is_error
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert calls == application.mock_calls == []
    assert len(observed) == 1
    envelope = result.structured_content
    assert envelope["result"] == {"fixture": "ok"}
    assert observed[0][1]["X-Request-ID"] == envelope["request_id"]
    for field, value in arguments.items():
        if field == "cues":
            assert observed[0][0][field][0]["ordinal"] == 1
            assert observed[0][0][field][0]["text"] == "Fixture"
        elif field == "components":
            assert observed[0][0][field][0]["component_id"] == "audio_cpp"
        else:
            assert observed[0][0][field] == value
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count(SENTINEL) == 1


INVALID = [
    ("pandrator_get_session", {"session_id": MARKER}),
    ("pandrator_describe_parameters", {}),
    (
        "pandrator_resolve_subtitle_evidence",
        {
            "session_id": "session-1",
            "evidence_id": "evidence-1",
            "action": "edited",
            "idempotency_key": KEY,
        },
    ),
    (
        "pandrator_update_session",
        {"session_id": "session-1", "expected_revision": 1, "idempotency_key": KEY},
    ),
    (
        "pandrator_update_session_settings",
        {
            "session_id": "session-1",
            "section": "tts",
            "expected_revision": 1,
            "value": {"api_key": MARKER},
            "idempotency_key": KEY,
        },
    ),
    (
        "pandrator_patch_session_settings",
        {
            "session_id": "session-1",
            "section": "tts",
            "expected_revision": 1,
            "value": {"api_key": MARKER},
            "idempotency_key": KEY,
        },
    ),
    (
        "pandrator_patch_subtitle_cues",
        {
            "session_id": "session-1",
            "stage": "transcribe",
            "expected_revision": 1,
            "cues": [{"ordinal": 0}],
            "idempotency_key": KEY,
        },
    ),
    (
        "pandrator_patch_subtitle_cues",
        {
            "session_id": "session-1",
            "stage": "transcribe",
            "expected_revision": 1,
            "cues": [{"ordinal": i, "text": MARKER} for i in range(1, 102)],
            "idempotency_key": KEY,
        },
    ),
]


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"], ids=["modern", "legacy"])
@pytest.mark.parametrize("tool,arguments", INVALID)
def test_main_domain_failures_are_typed_and_do_not_expose_inputs(
    tmp_path: Path, protocol: str, tool: str, arguments: dict[str, Any]
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, tool, arguments, protocol))
    assert calls == application.mock_calls == []
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    failure = failure_value(result, tool)
    assert MARKER not in json.dumps(failure)


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"], ids=["modern", "legacy"])
def test_nested_cue_dto_validation_runs_under_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    protocol: str,
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    observed: list[dict[str, str]] = []
    install_probe(monkeypatch, "CuePatchInput", observed)
    tool = "pandrator_patch_subtitle_cues"
    arguments = next(c[3] for c in CASES if c[0] == tool)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, tool, arguments, protocol))
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert calls == application.mock_calls == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count(SENTINEL) == 1
    failure = failure_value(result, tool)
    assert len(observed) == 1 and observed[0]["X-Request-ID"] == failure["request_id"]
