"""Queue metadata explains why an edited recording still has stale audio."""
from datetime import timedelta

from pandrator.web.models import Job, utcnow


def test_queued_replacement_reports_live_session_export_only():
    from tests.test_web_generation_regeneration import GenerationRegenerationTests

    case = GenerationRegenerationTests()
    case.setUp()
    try:
        with case.database.session() as session:
            blocker = Job(kind="export.variant", session_id=case.session_id,
                          status="running", lease_expires_at=utcnow() + timedelta(minutes=1),
                          progress_detail="Extending final frame")
            session.add(blocker)
            session.flush()
            blocker_id = blocker.id
        run = case._start(operation="regenerate", segment_ids=case.segment_ids[:1])
        assert run["queued_segment_ids"] == case.segment_ids[:1]
        assert run["waiting_for_job"] == {
            "id": blocker_id, "kind": "export.variant", "progress_detail": "Extending final frame"
        }
        with case.database.session() as session:
            session.get(Job, blocker_id).lease_expires_at = utcnow() - timedelta(seconds=1)
        runs = case.client.get(f"/api/v1/sessions/{case.session_id}/generation-runs").get_json()["items"]
        assert next(item for item in runs if item["id"] == run["id"])["waiting_for_job"] is None
    finally:
        case.tearDown()
