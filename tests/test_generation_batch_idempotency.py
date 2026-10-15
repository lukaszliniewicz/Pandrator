"""Atomic and retry-safe generation-segment batch updates."""

import pytest
from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    ApiIdempotency,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
)


@pytest.fixture
def generation_case(tmp_path):
    bootstrap = BootstrapTokenStore()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
    )
    client = app.test_client()
    headers = _bootstrap(client, bootstrap.issue())
    services = app.extensions["pandrator"]
    database = services["database"]
    created = client.post(
        "/api/v1/sessions",
        json={"name": "Batch update regression", "workflow_kind": "voiceover"},
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    session_id = created.get_json()["id"]
    plan = services["generation"].create_plan(
        session_id,
        source_revision_id=None,
        settings={},
        segments=[{"text": "Original first."}, {"text": "Original second."}],
    )
    segment_rows = services["generation"].list_segments(session_id)["items"]
    case = {
        "app": app,
        "bootstrap": bootstrap,
        "client": client,
        "database": database,
        "generation": services["generation"],
        "headers": headers,
        "plan_revision_id": plan["active_revision_id"],
        "segment_ids": [row["id"] for row in segment_rows],
        "services": services,
        "session_id": session_id,
    }
    yield case
    database.dispose()


def _bootstrap(client, token):
    response = client.post("/api/v1/auth/bootstrap", json={"token": token})
    assert response.status_code == 200, response.get_json()
    return {"X-CSRF-Token": response.get_json()["csrf_token"]}


def _batch(case, updates, *, key=None, client=None, headers=None):
    request_headers = dict(headers or case["headers"])
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return (client or case["client"]).patch(
        f"/api/v1/sessions/{case['session_id']}/generation-segments",
        json={"updates": updates},
        headers=request_headers,
    )


def _segment_state(case, segment_ids):
    with case["database"].session() as session:
        return {
            segment_id: (
                row.text,
                row.revision,
                row.plan_revision_id,
            )
            for segment_id in segment_ids
            if (row := session.get(GenerationSegment, segment_id)) is not None
        }


def test_lost_response_retry_replays_original_edit_copy(generation_case):
    case = generation_case
    original_id = case["segment_ids"][0]
    with case["database"].session() as session:
        session.add(
            GenerationRun(
                session_id=case["session_id"],
                plan_revision_id=case["plan_revision_id"],
                sequence_number=1,
                status="completed",
            )
        )

    updates = [{
        "id": original_id,
        "revision": 1,
        "changes": {"text": "Edited once."},
    }]
    first = _batch(case, updates, key="generation-batch-retry-01")
    assert first.status_code == 200, first.get_json()
    first_body = first.get_json()
    edited_id = first_body["items"][0]["id"]
    assert edited_id != original_id
    assert first_body["items"][0]["previous_segment_id"] == original_id

    retry = _batch(case, updates, key="generation-batch-retry-01")
    assert retry.status_code == 200, retry.get_json()
    assert retry.get_json() == first_body
    assert retry.headers["Idempotency-Replayed"] == "true"

    with case["database"].session() as session:
        plan = session.scalar(
            select(GenerationPlan).where(
                GenerationPlan.session_id == case["session_id"]
            )
        )
        assert plan.active_revision_id == first_body["items"][0]["plan_revision_id"]
        assert session.scalar(
            select(func.count(GenerationPlanRevision.id)).where(
                GenerationPlanRevision.plan_id == plan.id
            )
        ) == 2
        assert session.scalar(
            select(func.count(ApiIdempotency.id)).where(
                ApiIdempotency.operation_id == "updateGenerationSegments",
                ApiIdempotency.idempotency_key == "generation-batch-retry-01",
            )
        ) == 1
    assert _segment_state(case, [original_id, edited_id]) == {
        original_id: ("Original first.", 1, case["plan_revision_id"]),
        edited_id: (
            "Edited once.",
            first_body["items"][0]["revision"],
            first_body["items"][0]["plan_revision_id"],
        ),
    }

    changed = _batch(
        case,
        [{**updates[0], "changes": {"text": "A different edit."}}],
        key="generation-batch-retry-01",
    )
    assert changed.status_code == 409
    assert changed.get_json()["error"]["code"] == "idempotency_conflict"
    assert _segment_state(case, [original_id, edited_id])[edited_id][0] == "Edited once."


def test_stale_row_rejects_whole_batch_and_does_not_consume_key(generation_case):
    case = generation_case
    first_id, second_id = case["segment_ids"]
    before = _segment_state(case, [first_id, second_id])
    stale = _batch(
        case,
        [
            {"id": first_id, "revision": 1, "changes": {"text": "Would change."}},
            {"id": second_id, "revision": 2, "changes": {"text": "Stale row."}},
        ],
        key="generation-batch-stale-01",
    )
    assert stale.status_code == 409, stale.get_json()
    assert _segment_state(case, [first_id, second_id]) == before
    with case["database"].session() as session:
        assert session.scalar(
            select(func.count(ApiIdempotency.id)).where(
                ApiIdempotency.operation_id == "updateGenerationSegments",
                ApiIdempotency.idempotency_key == "generation-batch-stale-01",
            )
        ) == 0


@pytest.mark.parametrize("foreign_kind", ["missing", "cross_session"])
def test_invalid_or_cross_session_id_does_not_partially_update(
    generation_case, foreign_kind
):
    case = generation_case
    first_id, second_id = case["segment_ids"]
    foreign_id = "missing-segment-id"
    if foreign_kind == "cross_session":
        foreign_session = case["client"].post(
            "/api/v1/sessions",
            json={"name": "Other batch session", "workflow_kind": "voiceover"},
            headers=case["headers"],
        )
        assert foreign_session.status_code == 201, foreign_session.get_json()
        foreign_plan = case["generation"].create_plan(
            foreign_session.get_json()["id"],
            source_revision_id=None,
            settings={},
            segments=[{"text": "Foreign segment."}],
        )
        foreign_id = case["generation"].list_segments(
            foreign_session.get_json()["id"]
        )["items"][0]["id"]
        assert foreign_plan["active_revision_id"]

    before = _segment_state(case, [first_id, second_id])
    response = _batch(
        case,
        [
            {"id": first_id, "revision": 1, "changes": {"text": "Partial edit."}},
            {"id": foreign_id, "revision": 1, "changes": {"text": "Rejected."}},
        ],
    )
    assert response.status_code == 404, response.get_json()
    assert _segment_state(case, [first_id, second_id]) == before


def test_receipt_completion_failure_rolls_back_batch_and_receipt(
    generation_case, monkeypatch
):
    case = generation_case
    segment_id = case["segment_ids"][0]
    before = _segment_state(case, [segment_id])

    def fail_completion(*_args, **_kwargs):
        raise RuntimeError("injected completion failure")

    monkeypatch.setattr(case["services"]["idempotency"], "complete", fail_completion)
    with pytest.raises(RuntimeError, match="injected completion failure"):
        _batch(
            case,
            [{"id": segment_id, "revision": 1, "changes": {"text": "Must roll back."}}],
            key="generation-batch-rollback-01",
        )

    assert _segment_state(case, [segment_id]) == before
    with case["database"].session() as session:
        assert session.scalar(
            select(func.count(ApiIdempotency.id)).where(
                ApiIdempotency.operation_id == "updateGenerationSegments",
                ApiIdempotency.idempotency_key == "generation-batch-rollback-01",
            )
        ) == 0


def test_same_key_is_scoped_to_authenticated_principal(generation_case):
    case = generation_case
    other_client = case["app"].test_client()
    other_headers = _bootstrap(
        other_client,
        case["bootstrap"].issue(subject="separate-principal"),
    )
    first_id, second_id = case["segment_ids"]
    key = "generation-batch-principal-01"

    first = _batch(
        case,
        [{"id": first_id, "revision": 1, "changes": {"text": "Owner edit."}}],
        key=key,
    )
    second = _batch(
        case,
        [{"id": second_id, "revision": 1, "changes": {"text": "Other edit."}}],
        key=key,
        client=other_client,
        headers=other_headers,
    )
    assert first.status_code == 200, first.get_json()
    assert second.status_code == 200, second.get_json()
    with case["database"].session() as session:
        receipts = list(
            session.scalars(
                select(ApiIdempotency).where(
                    ApiIdempotency.operation_id == "updateGenerationSegments",
                    ApiIdempotency.idempotency_key == key,
                )
            )
        )
        assert {receipt.principal_subject for receipt in receipts} == {
            "owner",
            "separate-principal",
        }


def test_manual_optimized_text_readback_is_preserved(generation_case):
    case = generation_case
    exact_speech = "Café, spoken exactly 🙂 — punctuation stays."
    response = _batch(
        case,
        [{
            "id": case["segment_ids"][0],
            "revision": 1,
            "changes": {"text": "Updated display text.", "optimized_text": exact_speech},
        }],
        key="generation-batch-speech-01",
    )
    assert response.status_code == 200, response.get_json()
    item = response.get_json()["items"][0]
    assert item["optimized_text"] == exact_speech
    assert item["speech_plan"]["status"] == "manual_override"
    assert item["speech_plan"]["compiled_text"] == exact_speech


def test_browser_no_key_works_and_manager_bootstrap_requires_key(generation_case):
    case = generation_case
    browser = _batch(
        case,
        [{
            "id": case["segment_ids"][0],
            "revision": 1,
            "changes": {"text": "Browser update."},
        }],
    )
    assert browser.status_code == 200, browser.get_json()

    manager_client = case["app"].test_client()
    manager_headers = _bootstrap(
        manager_client,
        case["bootstrap"].issue(
            subject="manager-principal",
            kind="manager_bootstrap",
            scopes={"app.read", "app.write"},
        ),
    )
    missing_key = _batch(
        case,
        [{
            "id": case["segment_ids"][1],
            "revision": 1,
            "changes": {"text": "Must require retry identity."},
        }],
        client=manager_client,
        headers=manager_headers,
    )
    assert missing_key.status_code == 400, missing_key.get_json()
    assert missing_key.get_json()["error"]["code"] == "idempotency_key_required"
