"""Queue metadata explains why an edited recording still has stale audio."""
from datetime import timedelta

from sqlalchemy import event, select

from pandrator.web.models import Job, QuickTranscription, ResourceClaim, utcnow


def _seed_waiting_pair(
    case,
    *,
    requested_key="service:tts:xtts",
    claim_key=None,
    blocker_kind="generation.run",
    progress_detail="Extending final frame",
    private_owner=None,
):
    now = utcnow()
    owner = "worker-waiting-metadata"
    generation = 7
    blocker = Job(
        kind=blocker_kind,
        status="running",
        lease_owner=owner,
        lease_generation=generation,
        lease_expires_at=now + timedelta(minutes=1),
        progress_detail=progress_detail,
    )
    waiter = Job(kind="tts.preview", resource_keys_json=[requested_key])
    with case.database.session() as session:
        session.add_all([blocker, waiter])
        session.flush()
        claim = ResourceClaim(
            resource_key=claim_key or requested_key,
            job_id=blocker.id,
            lease_owner=owner,
            lease_generation=generation,
            expires_at=now + timedelta(minutes=1),
        )
        session.add(claim)
        if private_owner is not None:
            session.add(
                QuickTranscription(
                    owner_subject=private_owner,
                    job_id=blocker.id,
                    source_suffix=".wav",
                    size_bytes=1,
                    sha256="a" * 64,
                    expires_at=now + timedelta(hours=1),
                )
            )
        return waiter.id, blocker.id, claim.resource_key


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


def test_queued_preview_blocker_is_exposed_by_get_list_and_compact_snapshot():
    from tests.test_web_generation_regeneration import GenerationRegenerationTests

    case = GenerationRegenerationTests()
    case.setUp()
    try:
        waiter_id, blocker_id, _ = _seed_waiting_pair(case)
        expected = {
            "id": blocker_id,
            "kind": "generation.run",
            "progress_detail": "Extending final frame",
        }

        get_response = case.client.get(f"/api/v1/jobs/{waiter_id}")
        assert get_response.status_code == 200
        assert get_response.get_json()["waiting_for_job"] == expected

        list_response = case.client.get("/api/v1/jobs?limit=500")
        assert list_response.status_code == 200
        listed = next(
            item for item in list_response.get_json()["items"] if item["id"] == waiter_id
        )
        assert listed["waiting_for_job"] == expected

        snapshot_response = case.client.get("/api/v1/events/snapshot?view=compact")
        assert snapshot_response.status_code == 200
        snapshot = next(
            item
            for item in snapshot_response.get_json()["jobs"]["items"]
            if item["id"] == waiter_id
        )
        assert snapshot["waiting_for_job"] == expected
        assert "payload_json" not in snapshot
        assert "result_json" not in snapshot
    finally:
        case.tearDown()


def test_waiting_for_jobs_requires_live_matching_claim_and_owner_lease():
    from tests.test_web_generation_regeneration import GenerationRegenerationTests

    case = GenerationRegenerationTests()
    case.setUp()
    try:
        states = (
            "cleared_claim",
            "expired_claim",
            "expired_owner_lease",
            "terminal_owner",
            "stale_generation",
            "stale_owner",
        )
        waiter_ids = []
        claim_keys = {}
        blocker_ids = {}
        for index, state in enumerate(states):
            waiter_id, blocker_id, claim_key = _seed_waiting_pair(
                case,
                requested_key=f"resource:waiting-test:{index}",
            )
            waiter_ids.append(waiter_id)
            claim_keys[state] = claim_key
            blocker_ids[state] = blocker_id

        with case.database.session() as session:
            for state in states:
                claim = session.get(ResourceClaim, claim_keys[state])
                blocker = session.get(Job, blocker_ids[state])
                if state == "cleared_claim":
                    session.delete(claim)
                elif state == "expired_claim":
                    claim.expires_at = utcnow() - timedelta(seconds=1)
                elif state == "expired_owner_lease":
                    blocker.lease_expires_at = utcnow() - timedelta(seconds=1)
                elif state == "terminal_owner":
                    blocker.status = "succeeded"
                elif state == "stale_generation":
                    claim.lease_generation += 1
                elif state == "stale_owner":
                    claim.lease_owner = "different-worker"

        with case.database.session() as session:
            waiters = list(
                session.scalars(select(Job).where(Job.id.in_(waiter_ids))).all()
            )
            session.expunge_all()
        queue = case.app.extensions["pandrator"]["jobs"]
        assert queue.waiting_for_jobs(waiters) == {}
    finally:
        case.tearDown()


def test_waiting_for_jobs_canonicalizes_tts_aliases_and_ignores_other_services():
    from tests.test_web_generation_regeneration import GenerationRegenerationTests

    case = GenerationRegenerationTests()
    case.setUp()
    try:
        alias_waiter, alias_blocker, _ = _seed_waiting_pair(
            case,
            requested_key="service:tts:qwen3-tts",
            claim_key="service:tts:kobold_qwen",
            progress_detail="Alias blocker detail",
        )
        _, later_alias_blocker, _ = _seed_waiting_pair(
            case,
            requested_key="resource:unobserved-waiter",
            claim_key="service:tts:qwen3_tts",
            progress_detail="Later alias blocker detail",
        )
        unrelated_waiter, _, _ = _seed_waiting_pair(
            case,
            requested_key="service:tts:xtts",
            claim_key="service:tts:audio_cpp",
            progress_detail="Unrelated service detail",
        )
        with case.database.session() as session:
            session.get(Job, alias_blocker).created_at = utcnow() - timedelta(minutes=1)
            session.get(Job, later_alias_blocker).created_at = utcnow()
        with case.database.session() as session:
            waiters = list(
                session.scalars(
                    select(Job).where(Job.id.in_((alias_waiter, unrelated_waiter)))
                ).all()
            )
            session.expunge_all()

        queue = case.app.extensions["pandrator"]["jobs"]
        assert queue.waiting_for_jobs(waiters) == {
            alias_waiter: {
                "id": alias_blocker,
                "kind": "generation.run",
                "progress_detail": "Alias blocker detail",
            }
        }
    finally:
        case.tearDown()


def test_private_quick_transcription_is_never_reported_as_a_blocker():
    from tests.test_web_generation_regeneration import GenerationRegenerationTests

    case = GenerationRegenerationTests()
    case.setUp()
    try:
        waiter_id, _, _ = _seed_waiting_pair(
            case,
            blocker_kind="dubbing.export",
            progress_detail="Private transcription diagnostic",
            private_owner="somebody-else",
        )
        orphan_private_waiter_id, _, _ = _seed_waiting_pair(
            case,
            requested_key="resource:orphan-quick-transcription",
            blocker_kind="transcription.quick",
            progress_detail="Orphaned private-kind diagnostic",
        )
        get_response = case.client.get(f"/api/v1/jobs/{waiter_id}")
        assert get_response.status_code == 200
        assert get_response.get_json()["waiting_for_job"] is None

        list_response = case.client.get("/api/v1/jobs?limit=500")
        assert list_response.status_code == 200
        listed = next(
            item for item in list_response.get_json()["items"] if item["id"] == waiter_id
        )
        assert listed["waiting_for_job"] is None
        assert "Private transcription diagnostic" not in list_response.get_data(as_text=True)

        snapshot_response = case.client.get("/api/v1/events/snapshot?view=compact")
        assert snapshot_response.status_code == 200
        snapshot = next(
            item
            for item in snapshot_response.get_json()["jobs"]["items"]
            if item["id"] == waiter_id
        )
        assert snapshot["waiting_for_job"] is None
        assert "Private transcription diagnostic" not in snapshot_response.get_data(as_text=True)

        queue = case.app.extensions["pandrator"]["jobs"]
        orphan_private_waiter = queue.get(orphan_private_waiter_id)
        assert queue.waiting_for_jobs([orphan_private_waiter]) == {}
    finally:
        case.tearDown()


def test_waiting_for_jobs_uses_one_query_and_skips_query_without_candidates():
    from tests.test_web_generation_regeneration import GenerationRegenerationTests

    case = GenerationRegenerationTests()
    case.setUp()
    statements = []
    listening = False

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    try:
        first_waiter, _, _ = _seed_waiting_pair(
            case, requested_key="resource:batch:first"
        )
        second_waiter, _, _ = _seed_waiting_pair(
            case, requested_key="resource:batch:second"
        )
        no_resource = Job(kind="tts.preview")
        with case.database.session() as session:
            session.add(no_resource)
            session.flush()
            waiter_records = list(
                session.scalars(
                    select(Job).where(Job.id.in_((first_waiter, second_waiter)))
                ).all()
            )
            session.expunge_all()
            no_resource_id = no_resource.id
        with case.database.session() as session:
            no_resource_record = session.get(Job, no_resource_id)
            session.expunge(no_resource_record)

        queue = case.app.extensions["pandrator"]["jobs"]
        event.listen(case.database.engine, "before_cursor_execute", capture_sql)
        listening = True
        assert queue.waiting_for_jobs([]) == {}
        assert queue.waiting_for_jobs([no_resource_record]) == {}
        assert statements == []
        assert len(queue.waiting_for_jobs(waiter_records)) == 2
        assert len(statements) == 1
    finally:
        if listening:
            event.remove(case.database.engine, "before_cursor_execute", capture_sql)
        case.tearDown()
