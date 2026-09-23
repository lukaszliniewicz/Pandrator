"""Pass-2 history summaries: fast lists, null-not-zero counts, on-demand detail.

- Summary lists must not inspect audio reuse (no takes/artifacts/identity
  queries) and must not evaluate undo guards (no repair_state_hash/idle checks).
- Unchecked counts are null (never 0) with audio_reuse_checked=false; summary
  repair eligibility is can_undo=None with undo_checked=false.
- Legacy default payloads stay full; malformed summary flags answer 422.
- Single-revision detail is always full and session-scoped; undo stays
  server-authoritative.
"""

import wave

import pytest
from sqlalchemy import event, func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_review import parse_summary_flag
from pandrator.web.models import (
    AudioTake,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
)
from pandrator.web.repair_batches import (
    GUARD_KEY,
    capture_repair_base,
    record_accepted_repair,
)


@pytest.fixture
def workspace(tmp_path):
    tokens = BootstrapTokenStore()
    token = tokens.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=tokens)
    client = app.test_client()
    auth = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()
    headers = {"X-CSRF-Token": auth["csrf_token"], "Idempotency-Key": "pass2-summary-test"}
    services = app.extensions["pandrator"]
    sid = client.post(
        "/api/v1/sessions",
        json={"name": "Pass-2 summary fixture", "workflow_kind": "voiceover"},
        headers=headers,
    ).get_json()["id"]
    generation, database = services["generation"], services["database"]
    plan = generation.create_plan(
        sid,
        source_revision_id=None,
        settings={},
        segments=[
            {"text": "First sentence. Second sentence.", "source_segment_ids": [1, 2]},
            {"text": "Unchanged final sentence.", "source_segment_ids": [3]},
        ],
    )
    base_id = plan["active_revision_id"]
    audio_path = services["paths"].uploads / "fixture.wav"
    with wave.open(str(audio_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 160)
    artifact = services["artifacts"].register(
        audio_path, kind="audio", role="generation_take", session_id=sid
    )
    with database.session() as session:
        rows = list(
            session.scalars(
                select(GenerationSegment)
                .where(GenerationSegment.plan_revision_id == base_id)
                .order_by(GenerationSegment.ordinal)
            )
        )
        for row in rows:
            row.status = "completed"
            session.add(
                AudioTake(
                    generation_segment_id=row.id,
                    artifact_id=artifact.id,
                    status="completed",
                    is_active=True,
                )
            )
        run = GenerationRun(
            session_id=sid,
            plan_revision_id=base_id,
            sequence_number=1,
            operation="generate",
            status="completed",
            settings_snapshot_json={},
            settings_hash="fixture",
        )
        session.add(run)
        session.flush()
        run_id, first_id = run.id, rows[0].id
    context = dict(
        app=app,
        client=client,
        headers=headers,
        services=services,
        database=database,
        generation=generation,
        sid=sid,
        base_id=base_id,
        run_id=run_id,
        first_id=first_id,
        url=f"/api/v1/sessions/{sid}/generation-plan",
    )
    yield context
    database.dispose()


def apply_repair(w):
    with w["database"].immediate_session() as session:
        snapshot = capture_repair_base(session, w["base_id"])
        operation = {
            "action": "split",
            "segment_id": w["first_id"],
            "cursor": 15,
            "text_layer": "display",
            "reason": "early_timing_repair",
            "source_generation_run_id": w["run_id"],
            "repair_status": "applied",
            GUARD_KEY: snapshot,
        }
        result = w["generation"].revise_topology_in_session(
            session, w["sid"], w["base_id"], operation
        )
        for segment_id in result["affected_segment_ids"]:
            segment = session.get(GenerationSegment, segment_id)
            segment.status = "completed"
            session.add(
                AudioTake(
                    generation_segment_id=segment_id,
                    artifact_id=None,
                    status="completed",
                    is_active=False,
                )
            )
        session.flush()
        record_accepted_repair(session, result["plan_revision_id"])
    return result["plan_revision_id"]


class SummaryFlagTests:
    def test_strict_values(self):
        assert parse_summary_flag(None) is False
        assert parse_summary_flag("true") is True
        assert parse_summary_flag("1") is True
        assert parse_summary_flag("yes") is True
        assert parse_summary_flag("false") is False
        assert parse_summary_flag("0") is False
        assert parse_summary_flag("no") is False
        for bad in ("", "bogus", "2", "tru", "yes please"):
            try:
                parse_summary_flag(bad)
            except ValueError:
                continue
            raise AssertionError(f"summary={bad!r} must raise ValueError")


def test_revision_list_summary_has_null_counts_and_same_grouping(workspace):
    w = workspace
    full = w["client"].get(w["url"].replace("/generation-plan", "/generation-plan/revisions")).get_json()
    summary = w["client"].get(
        w["url"].replace("/generation-plan", "/generation-plan/revisions"), query_string={"summary": "true"}
    ).get_json()
    assert [item["id"] for item in full["items"]] == [item["id"] for item in summary["items"]]
    assert full["total"] == summary["total"]
    assert full["audio_reuse_checked"] is True
    assert summary["audio_reuse_checked"] is False
    for item in full["items"]:
        assert item["audio_reuse_checked"] is True
        assert isinstance(item["reusable_segment_count"], int)
        assert isinstance(item["stale_segment_count"], int)
    for item in summary["items"]:
        assert item["audio_reuse_checked"] is False
        assert item["reusable_segment_count"] is None
        assert item["stale_segment_count"] is None
        assert item["audio_settings_stale_segment_count"] is None
        assert item["audio_identity_unknown_segment_count"] is None
        # Cheap fields still present for grouping/selection.
        assert item["segment_count"] >= 0
        assert item["revision_number"] >= 1


def test_malformed_summary_is_422(workspace):
    w = workspace
    assert (
        w["client"]
        .get(
            w["url"].replace("/generation-plan", "/generation-plan/revisions"),
            query_string={"summary": "bogus"},
        )
        .status_code
        == 422
    )
    assert (
        w["client"].get(w["url"] + "/history", query_string={"summary": "bogus"}).status_code
        == 422
    )
    assert (
        w["client"]
        .get(w["url"].replace("/generation-plan", "/generation-plan/status"), query_string={"summary": "bogus"})
        .status_code
        == 422
    )


def test_grouped_summary_skips_guards_and_matches_full_grouping(workspace):
    w = workspace
    result_id = apply_repair(w)
    full = w["client"].get(w["url"] + "/history").get_json()
    summary = w["client"].get(w["url"] + "/history", query_string={"summary": "true"}).get_json()
    assert [item["entry_id"] for item in full["items"]] == [
        item["entry_id"] for item in summary["items"]
    ]
    assert full["total"] == summary["total"]
    assert full["checkpoint_total"] == summary["checkpoint_total"]
    assert full["undo_checked"] is True
    assert summary["undo_checked"] is False
    batch_full = next(item for item in full["items"] if item.get("repair_batch"))["repair_batch"]
    batch_summary = next(item for item in summary["items"] if item.get("repair_batch"))[
        "repair_batch"
    ]
    assert batch_full["result_revision_id"] == result_id
    assert batch_summary["result_revision_id"] == result_id
    assert batch_summary["attempt_count"] == batch_full["attempt_count"]
    assert batch_summary["applied_count"] == batch_full["applied_count"]
    assert batch_summary["status"] == batch_full["status"]
    assert batch_full["undo_checked"] is True
    assert isinstance(batch_full["can_undo"], bool)
    assert batch_summary["undo_checked"] is False
    assert batch_summary["can_undo"] is None
    assert batch_summary["undo_disabled_reason"] is None
    assert batch_summary["expected_revision_id"] is None
    assert batch_summary["expected_state_hash"] is None


def test_summary_issues_no_take_artifact_or_guard_queries(workspace):
    w = workspace
    apply_repair(w)
    log = []

    def recorder(conn, cursor, statement, parameters, context, executemany):
        log.append(statement)

    event.listen(w["database"].engine, "before_cursor_execute", recorder)
    try:
        from pandrator.web import repair_batches as batches

        batches.grouped_revision_history(
            w["database"], w["sid"], include_audio_reuse=False, include_undo_eligibility=False
        )
    finally:
        event.remove(w["database"].engine, "before_cursor_execute", recorder)
    assert not [item for item in log if "FROM audio_takes" in item], log
    assert not [item for item in log if "FROM artifacts" in item], log


def test_single_revision_detail_matches_full_and_is_session_scoped(workspace):
    w = workspace
    full = w["client"].get(w["url"].replace("/generation-plan", "/generation-plan/revisions")).get_json()
    target = full["items"][0]
    detail = w["client"].get(
        w["url"].replace("/generation-plan", f"/generation-plan/revisions/{target['id']}")
    )
    assert detail.status_code == 200, detail.get_json()
    body = detail.get_json()
    assert body["revision"] == target
    assert body["revision"]["audio_reuse_checked"] is True
    assert w["client"].get(
        w["url"].replace("/generation-plan", "/generation-plan/revisions/does-not-exist")
    ).status_code == 404
    other_headers = {key: value for key, value in w["headers"].items() if key != "Idempotency-Key"}
    other = w["client"].post(
        "/api/v1/sessions",
        json={"name": "Other session", "workflow_kind": "voiceover"},
        headers=other_headers,
    ).get_json()["id"]
    assert (
        w["client"].get(f"/api/v1/sessions/{other}/generation-plan/revisions/{target['id']}").status_code
        == 404
    )


def test_status_summary_keeps_selection_fields_with_null_counts(workspace):
    w = workspace
    full = w["client"].get(w["url"].replace("/generation-plan", "/generation-plan/status")).get_json()
    summary = w["client"].get(
        w["url"].replace("/generation-plan", "/generation-plan/status"), query_string={"summary": "true"}
    ).get_json()
    assert [item["id"] for item in full["items"]] == [item["id"] for item in summary["items"]]
    assert summary["selected_revision_id"] == full["selected_revision_id"]
    assert summary["can_prepare"] == full["can_prepare"]
    for item in summary["items"]:
        assert item["reusable_segment_count"] is None
        assert item["stale_segment_count"] is None


def test_undo_stays_server_authoritative_after_summary(workspace):
    w = workspace
    apply_repair(w)
    summary = w["client"].get(w["url"] + "/history", query_string={"summary": "true"}).get_json()
    batch = next(item for item in summary["items"] if item.get("repair_batch"))["repair_batch"]
    # A null summary eligibility snapshot can never authorize undo.
    denied = w["client"].post(
        f"{w['url']}/repair-batches/{batch['id']}/undo",
        json={"expected_revision_id": batch["result_revision_id"], "expected_state_hash": "0" * 64},
        headers=w["headers"],
    )
    assert denied.status_code == 409, denied.get_json()
    # The authoritative detail still drives a real undo.
    detail = w["client"].get(f"{w['url']}/repair-batches/{batch['id']}").get_json()
    assert detail["repair_batch"]["undo_checked"] is True
    assert detail["repair_batch"]["can_undo"] is True
    accepted = w["client"].post(
        f"{w['url']}/repair-batches/{batch['id']}/undo",
        json={
            "expected_revision_id": detail["repair_batch"]["expected_revision_id"],
            "expected_state_hash": detail["repair_batch"]["expected_state_hash"],
        },
        headers={**w["headers"], "Idempotency-Key": "pass2-summary-undo"},
    )
    assert accepted.status_code == 201, accepted.get_json()
    with w["database"].session() as session:
        count = session.scalar(select(func.count()).select_from(GenerationPlanRevision))
    assert count == 3
