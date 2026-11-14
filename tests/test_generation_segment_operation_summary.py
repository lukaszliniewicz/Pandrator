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
