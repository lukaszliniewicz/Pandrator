"""Native enqueue receipts replay before mutable media-edit admission."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from pandrator.web.models import ApiIdempotency, Job, JobEvent, utcnow
from tests import test_web_media_edit_routes as route_tests

KEY = "media-enqueue:fixture:1"
OPERATIONS = {
    "propose": ("proposeMediaEdit", "media_edit.propose"),
    "render": ("renderMediaEdit", "media_edit.render"),
}
BODIES = {
    "propose": {"revision": 1, "instructions": "Trim setup chatter."},
    "render": {"revision": 1, "subtitles_only": False},
}


@dataclass
class Harness:
    case: Any
    plan: dict[str, Any]

    @property
    def services(self) -> dict[str, Any]:
        return self.case.extension

    def post(
        self,
        operation: str,
        body: dict[str, Any] | None = None,
        *,
        key: str = KEY,
        session_id: str | None = None,
    ):
        return self.case.client.post(
            f"/api/v1/sessions/{session_id or self.case.session.id}/media-edit/{operation}",
            json=copy.deepcopy(BODIES[operation] if body is None else body),
            headers={"X-CSRF-Token": self.case.csrf, "Idempotency-Key": key},
        )


@pytest.fixture
def harness() -> Iterator[Harness]:
    case = route_tests.MediaEditProposalRouteTests(methodName="runTest")
    case.setUp()
    plan = {"revision": 1, "reviewed": True}
    try:
        with patch.object(
            case.extension["media_edit"],
            "state",
            side_effect=lambda _session_id: {"plan": copy.deepcopy(plan)},
        ):
            yield Harness(case, plan)
    finally:
        case.tearDown()


def assert_counts(harness: Harness, jobs: int, events: int, receipts: int) -> None:
    with harness.services["database"].session() as db:
        assert db.query(Job).count() == jobs
        assert db.query(JobEvent).count() == events
        assert db.query(ApiIdempotency).count() == receipts


def receipt(db: Any, operation: str) -> ApiIdempotency:
    record = db.scalar(
        select(ApiIdempotency).where(
            ApiIdempotency.operation_id == OPERATIONS[operation][0],
            ApiIdempotency.idempotency_key == KEY,
        )
    )
    assert record is not None
    return record


def first_request(harness: Harness, operation: str) -> tuple[Any, dict[str, Any]]:
    response = harness.post(operation)
    assert response.status_code == 202, response.get_json()
    assert "Idempotency-Replayed" not in response.headers
    value = response.get_json()
    assert set(value) == {
        "id",
        "kind",
        "session_id",
        "workflow_run_id",
        "status",
        "result",
        "progress",
        "progress_detail",
        "error",
        "created_at",
        "started_at",
        "finished_at",
        "updated_at",
    }
    assert value["kind"] == OPERATIONS[operation][1]
    assert value["session_id"] == harness.case.session.id
    assert value["status"] == "queued"
    assert value["result"] is None
    assert value["error"] is None
    assert_counts(harness, 1, 1, 1)
    with harness.services["database"].session() as db:
        job = db.get(Job, value["id"])
        assert job is not None
        assert job.kind == OPERATIONS[operation][1]
        assert job.payload_json["session_id"] == harness.case.session.id
        assert job.payload_json["revision"] == 1
        if operation == "propose":
            assert job.payload_json["instructions"] == "Trim setup chatter."
        else:
            assert job.payload_json["subtitles_only"] is False
        event = db.scalar(select(JobEvent))
        assert event is not None
        assert event.job_id == job.id
        assert event.event_type == "job.queued"
        record = receipt(db, operation)
        assert record.state == "completed"
        assert record.status_code == 202
        assert record.resource_kind == "job"
        assert record.resource_id == job.id
        assert record.response_json == value
    return response, value


def terminal_and_legacy(harness: Harness, operation: str, job_id: str, *, legacy: bool) -> str:
    with harness.services["database"].session() as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.status = "succeeded"
        record = receipt(db, operation)
        if legacy:
            record.request_digest = harness.services["idempotency"].request_digest(
                OPERATIONS[operation][0], copy.deepcopy(job.payload_json)
            )
        return record.request_digest


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("change", ["revision", "settings"])
@pytest.mark.parametrize("digest_mode", ["current", "legacy"])
def test_replay_precedes_mutable_admission(
    harness: Harness, operation: str, change: str, digest_mode: str
) -> None:
    original, value = first_request(harness, operation)
    digest = terminal_and_legacy(harness, operation, value["id"], legacy=digest_mode == "legacy")
    settings = harness.services["workspace_settings"]
    if change == "revision":
        harness.plan.update(revision=2, reviewed=False)
    else:
        section = "correction" if operation == "propose" else "subtitles"
        before = settings.get(harness.case.session.id, section)
        updated = settings.update(
            harness.case.session.id,
            section,
            before["revision"],
            {"model_name": "fixture/changed"}
            if operation == "propose"
            else {"max_chars_per_line": 61},
        )
        assert updated["effective"] != before["effective"]
    with (
        patch.object(
            harness.services["media_edit"], "state", wraps=harness.services["media_edit"].state
        ) as state,
        patch.object(settings, "get", wraps=settings.get) as get_settings,
    ):
        replay = harness.post(operation)
    assert replay.status_code == 202, replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.data == original.data
    assert replay.get_json() == value
    state.assert_not_called()
    get_settings.assert_not_called()
    assert_counts(harness, 1, 1, 1)
    if digest_mode == "legacy":
        with harness.services["database"].session() as db:
            assert receipt(db, operation).request_digest == digest


CONFLICTS = [
    ("propose", "instructions", "Changed instructions."),
    ("propose", "model", "fixture/other"),
    ("propose", "revision", 2),
    ("render", "subtitles_only", True),
    ("render", "revision", 2),
    ("propose", "session", None),
    ("render", "session", None),
]


@pytest.mark.parametrize(("operation", "field", "new_value"), CONFLICTS)
def test_changed_request_conflicts_without_admission(
    harness: Harness, operation: str, field: str, new_value: Any
) -> None:
    first_request(harness, operation)
    body = copy.deepcopy(BODIES[operation])
    session_id = None
    if field == "session":
        session_id = (
            harness.services["sessions"].create("Another media edit", workflow_kind="media_edit").id
        )
    else:
        body[field] = new_value
    settings = harness.services["workspace_settings"]
    with (
        patch.object(
            harness.services["media_edit"], "state", wraps=harness.services["media_edit"].state
        ) as state,
        patch.object(settings, "get", wraps=settings.get) as get_settings,
    ):
        conflict = harness.post(operation, body, session_id=session_id)
    assert conflict.status_code == 409, conflict.get_json()
    assert conflict.get_json()["error"]["code"] == "idempotency_conflict"
    state.assert_not_called()
    get_settings.assert_not_called()
    assert_counts(harness, 1, 1, 1)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_completion_failure_rolls_back_job_event_and_receipt(
    harness: Harness, operation: str
) -> None:
    with (
        patch.object(
            harness.services["idempotency"],
            "complete",
            side_effect=RuntimeError("fixture completion failed"),
        ),
        pytest.raises(RuntimeError, match="fixture completion failed"),
    ):
        harness.post(operation)
    assert_counts(harness, 0, 0, 0)
    first_request(harness, operation)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_expired_receipt_can_create_fresh_job(harness: Harness, operation: str) -> None:
    _, original = first_request(harness, operation)
    with harness.services["database"].session() as db:
        job = db.get(Job, original["id"])
        assert job is not None
        job.status = "succeeded"
        receipt(db, operation).expires_at = utcnow() - timedelta(seconds=1)
    fresh = harness.post(operation)
    assert fresh.status_code == 202, fresh.get_json()
    assert fresh.get_json()["id"] != original["id"]
    assert "Idempotency-Replayed" not in fresh.headers
    assert_counts(harness, 2, 2, 1)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_matching_active_job_survives_new_key(harness: Harness, operation: str) -> None:
    _, original = first_request(harness, operation)
    reused = harness.post(operation, key="media-enqueue:fixture:2")
    assert reused.status_code == 202, reused.get_json()
    assert reused.get_json()["id"] == original["id"]
    assert "Idempotency-Replayed" not in reused.headers
    assert_counts(harness, 1, 1, 2)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_legacy_receipt_without_recorded_job_conflicts(harness: Harness, operation: str) -> None:
    _, original = first_request(harness, operation)
    digest = terminal_and_legacy(harness, operation, original["id"], legacy=True)
    with harness.services["database"].session() as db:
        job = db.get(Job, original["id"])
        assert job is not None
        db.delete(job)
    assert_counts(harness, 0, 0, 1)
    response = harness.post(operation)
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["error"]["code"] == "idempotency_conflict"
    assert_counts(harness, 0, 0, 1)
    with harness.services["database"].session() as db:
        record = receipt(db, operation)
        assert record.resource_id == original["id"]
        assert record.request_digest == digest
        assert record.response_json == original
