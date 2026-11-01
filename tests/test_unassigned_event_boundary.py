"""Unassigned retained events confer no authority through payload hints."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import func, select, text

from pandrator.web.access.contracts import AccessContext
from pandrator.web.models import JobEvent, User, new_id
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import credential, job
from tests.test_job_access_routes import no_network as no_network


def add_event(session, job_id, *, marker="hidden-unassigned", payload=None):
    row = JobEvent(
        job_id=job_id,
        event_type="job.progress",
        payload_json=payload or {"detail": marker},
    )
    session.add(row)
    session.flush()
    return row.id


def test_unassigned_and_foreign_events_filter_before_limit_and_decode(case):
    caller = credential(case, "editor")
    owned, hidden = job(case), job(case, project="b")
    expected = []
    with case.database.immediate_session() as session:
        null_id = add_event(session, None)
        session.execute(
            text("UPDATE job_events SET payload_json = :broken WHERE id = :id"),
            {"broken": "malformed legacy JSON", "id": null_id},
        )
        for _ in range(55):
            add_event(
                session, None, payload={"session_id": case.a.id, "detail": "claimed owned session"}
            )
            add_event(session, hidden)
        for index in range(3):
            expected.append(add_event(session, owned, marker=f"allowed-{index}"))
            add_event(session, None)
        latest = add_event(session, None)
    context = AccessContext(caller.actor)
    with case.database.snapshot_session() as session:
        first = case.services.work.events_after_in_session(session, context, 0, limit=2)
        assert [row.id for row in first.items] == expected[:2]
        assert first.next_cursor == expected[1]
        second = case.services.work.events_after_in_session(
            session, context, first.next_cursor, limit=2
        )
        assert [row.id for row in second.items] == expected[2:]
        assert second.next_cursor == latest
        assert all(row.work_id == owned for row in first.items + second.items)


@pytest.mark.parametrize("name", ["editor", "owner", "outsider"])
def test_no_role_can_adopt_unassigned_event_payload_hints(case, name):
    caller = credential(case, name)
    with case.database.immediate_session() as session:
        latest = add_event(
            session,
            None,
            payload={
                "session_id": case.a.id,
                "workflow_run_id": new_id(),
                "workspace_id": case.team,
                "detail": "Unassigned private detail",
            },
        )
    with case.database.snapshot_session() as session:
        page = case.services.work.events_after_in_session(session, AccessContext(caller.actor), 0)
        assert page.items == []
        assert page.next_cursor == latest
        assert session.scalar(select(JobEvent.job_id).where(JobEvent.id == latest)) is None


def test_http_unassigned_only_page_advances_without_payload(case):
    caller = credential(case, "editor")
    with case.database.immediate_session() as session:
        cursor = add_event(session, None, marker="never-stream-this-private-metadata")
    response = caller.client.get("/api/v1/events?after=0", headers=caller.headers, buffered=False)
    try:
        first = next(response.response).decode()
    finally:
        response.close()
    assert "event: stream.cursor" in first
    assert f"id: {cursor}\n" in first
    assert "never-stream-this-private-metadata" not in first
    assert "job.progress" not in first


@pytest.mark.parametrize("identifier", [None, "", "  ", 7])
def test_current_event_producer_rejects_unassigned_id_before_effects(case, monkeypatch, identifier):
    queue = case.services.jobs
    redact = Mock(side_effect=AssertionError("Invalid event reached payload projection"))
    monkeypatch.setattr(queue, "redact_diagnostic", redact)
    with case.database.immediate_session() as session:
        pending = User(id=new_id(), display_name="Do not flush", is_active=True)
        session.add(pending)
        with pytest.raises(ValueError, match="requires a job ID"):
            queue._event(session, identifier, "job.progress", {"detail": "private"})
        assert pending in session.new
        session.expunge(pending)
        assert session.scalar(select(func.count()).select_from(JobEvent)) == 0
    redact.assert_not_called()


def test_assigned_event_keeps_job_scope_and_trusted_diagnostics(case):
    caller = credential(case, "editor")
    owned = job(case)
    with case.database.immediate_session() as session:
        row = case.services.jobs._event(session, owned, "job.progress", {"detail": "assigned"})
        session.flush()
        assigned = row.id
        unassigned = add_event(session, None)
    with case.database.snapshot_session() as session:
        page = case.services.work.events_after_in_session(session, AccessContext(caller.actor), 0)
        assert [row.id for row in page.items] == [assigned]
        assert page.items[0].data["session_id"] == case.a.id
        assert page.next_cursor == unassigned
    # Existing trusted operator diagnostic path retains historical rows for recovery.
    assert {row.id for row in case.services.jobs.events_after(0)} == {assigned, unassigned}
