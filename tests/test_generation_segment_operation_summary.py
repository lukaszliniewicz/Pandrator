"""A segment listing page carries a summary of its revision's operation by default.

A restore's ``operation_json`` holds per-segment ``mapping`` and ``lineage`` maps:
873 KB on a 5,167-segment book, sent with every page, even a page of one row.
"""

from __future__ import annotations

import json
import uuid

from pandrator.web import models as m
from pandrator.web.generation_segment_reads import summarize_operation
from tests.test_performance_plans import case as case  # noqa: F401


def _large_restore(case) -> dict:  # noqa: F811
    mapping = {f"segment-{index}": f"restored-{index}" for index in range(400)}
    operation = {
        "action": "restore",
        "reason": "Restore an earlier revision",
        "batch": {"id": "batch-1"},
        "target_revision_id": "target",
        "expected_revision_id": case["revision_id"],
        "mapping": mapping,
        "lineage": {key: [value] for key, value in mapping.items()},
    }
    with case["services"]["database"].session() as session:
        session.get(m.GenerationPlanRevision, case["revision_id"]).operation_json = operation
    return operation


def _page(case, query: str = ""):  # noqa: F811
    return case["client"].get(
        f"/api/v1/sessions/{case['session_id']}/generation-segments?limit=1{query}"
    )


def test_default_page_omits_large_operation_values_and_names_them(case):  # noqa: F811
    operation = _large_restore(case)
    response = _page(case)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["operation_json"] == {
        key: operation[key]
        for key in ("action", "reason", "batch", "target_revision_id", "expected_revision_id")
    }
    assert body["operation_json_omitted"] == ["lineage", "mapping"]
    assert len(response.get_data()) < 16 * 1024
    compact = _page(case, "&view=compact").get_json()
    assert compact["operation_json_omitted"] == ["lineage", "mapping"]


def test_full_operation_on_request(case):  # noqa: F811
    operation = _large_restore(case)
    body = _page(case, "&operation=full").get_json()
    assert body["operation_json"] == operation
    assert body["operation_json_omitted"] == []
    summary = _page(case, "&operation=summary").get_json()
    assert summary["operation_json_omitted"] == ["lineage", "mapping"]


def test_an_unknown_operation_value_is_refused(case):  # noqa: F811
    response = _page(case, "&operation=everything")
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "validation_error"


def test_small_operations_and_empty_plans_lose_nothing(case):  # noqa: F811
    body = _page(case).get_json()
    assert body["operation_json_omitted"] == []
    created = case["client"].post(
        "/api/v1/sessions",
        json={"name": f"No plan {uuid.uuid4().hex[:8]}", "workflow_kind": "audiobook"},
        headers=case["headers"],
    ).get_json()
    empty = case["client"].get(f"/api/v1/sessions/{created['id']}/generation-segments").get_json()
    assert (empty["operation_json"], empty["operation_json_omitted"]) == ({}, [])


def test_summary_rule_is_by_size_not_by_key():
    small = {"mapping": {"a": "b"}, "notes": "x" * 5000, "action": "split"}
    kept, omitted = summarize_operation(small, full=False)
    assert kept == {"mapping": {"a": "b"}, "action": "split"}
    assert omitted == ["notes"]
    # The limit is on compact UTF-8 bytes: 4096 bytes is kept, one more is not.
    at_limit = "é" * 2047  # 2 quotes + 4094 bytes = 4096
    assert len(json.dumps(at_limit, ensure_ascii=False).encode("utf-8")) == 4096
    assert summarize_operation({"v": at_limit}, full=False) == ({"v": at_limit}, [])
    assert summarize_operation({"v": at_limit + "a"}, full=False) == ({}, ["v"])
    assert summarize_operation(None, full=False) == ({}, [])
    assert summarize_operation(small, full=True) == (small, [])


class _ListingApplication:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.options: list[dict] = []

    def list_generation_segments(self, session_id, **options):
        self.options.append(options)
        return self.payload


def _mcp_listing(payload: dict, **arguments):
    from types import SimpleNamespace

    from pandrator_mcp.schemas.generation import ListGenerationSegmentsInput
    from pandrator_mcp.tools.generation import list_generation_segments

    application = _ListingApplication(payload)
    runtime = SimpleNamespace(require_application=lambda: application)
    listed = list_generation_segments(
        runtime, ListGenerationSegmentsInput(session_id="session-1", **arguments)
    )
    return listed, application.options[-1]


SUMMARY_PAGE = {
    "items": [],
    "operation_json": {
        "action": "restore", "reason": "Restore", "batch": {"id": "b"},
        "target_revision_id": "target",
    },
    "operation_json_omitted": ["lineage", "mapping"],
}


def test_mcp_asks_for_the_whole_operation_only_on_request():
    _listed, options = _mcp_listing(SUMMARY_PAGE)
    assert "operation" not in options
    _listed, options = _mcp_listing(SUMMARY_PAGE, operation="full")
    assert options["operation"] == "full"


def test_mcp_full_view_passes_the_server_summary_and_its_omissions():
    listed, _options = _mcp_listing(SUMMARY_PAGE)
    assert listed["operation_json"] == SUMMARY_PAGE["operation_json"]
    assert listed["operation_json_omitted"] == ["lineage", "mapping"]


def test_mcp_short_views_name_every_key_they_leave_out():
    listed, _options = _mcp_listing(SUMMARY_PAGE, view="compact")
    assert listed["operation_json"] == {
        "action": "restore", "batch": {"id": "b"}, "target_revision_id": "target",
    }
    assert listed["operation_json_omitted"] == ["lineage", "mapping", "reason"]


def test_mcp_full_operation_is_kept_whole_in_a_short_view():
    whole = {**SUMMARY_PAGE["operation_json"], "mapping": {"a": "b"}}
    listed, _options = _mcp_listing(
        {"items": [], "operation_json": whole, "operation_json_omitted": []},
        view="compact", operation="full",
    )
    assert listed["operation_json"] == whole
    assert listed["operation_json_omitted"] == []


def test_mcp_client_sends_operation_only_when_full():
    from pandrator_mcp.clients.application_generation import (
        ApplicationGenerationMethods,
    )

    sent: list[dict] = []

    class _Client(ApplicationGenerationMethods):
        def _request_json(self, path, *, parameters=None, **_kwargs):
            sent.append(dict(parameters or {}))
            return {}

        def _request_binary_json(self, *_args, **_kwargs):
            raise AssertionError("not used")

    client = _Client()
    client.list_generation_segments("session-1")
    client.list_generation_segments("session-1", operation="full")
    assert "operation" not in sent[0]
    assert sent[1]["operation"] == "full"
