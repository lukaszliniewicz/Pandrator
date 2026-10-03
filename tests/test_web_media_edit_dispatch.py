import json
import tempfile
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.dispatch import DispatchError
from pandrator.web.media_edit_dispatch import MediaEditDispatchRunService
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    MediaEditDispatchBatch,
    MediaEditDispatchRun,
    MediaEditPlan,
    MediaEditPlanRevision,
)
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.credentials import CredentialResolver
from pandrator_mcp.errors import PandratorMcpError, ToolFailure
from tests import test_mcp_application_client as _client_fixture
from tests import test_web_media_edit as _media_edit_fixture


def _materialization_snapshot(fixture):
    """Read all columns of the four tables materialization can mutate."""
    with fixture.database.session() as session:
        return {
            model.__tablename__: sorted(
                [dict(row) for row in session.execute(select(model.__table__)).mappings()],
                key=lambda row: json.dumps(row, default=str, sort_keys=True),
            )
            for model in (Artifact, ArtifactEdge, MediaEditPlan, MediaEditPlanRevision)
        }


def _rendered_pair(fixture):
    rendered = fixture._register("edited.mp4", "media_edit_media", b"edited", "video")
    derived = fixture._register(
        "edited.srt",
        "media_edit_subtitles",
        "subtitles",
        "srt",
        parent_ids=[rendered.id],
    )
    return rendered, derived


class MediaEditDispatchServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _media_edit_fixture.MediaEditServiceTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def _prepared(self):
        self.fixture._seed_external()
        media_edit = self.fixture._service()
        media_edit.prepare(self.fixture.session_id)
        return media_edit, MediaEditDispatchRunService(
            self.fixture.database, media_edit
        )

    def _create_and_claim(self, dispatch, *, revision=1, suffix="1"):
        with self.fixture.database.immediate_session() as session:
            run = dispatch.create_in_session(
                session,
                session_id=self.fixture.session_id,
                revision=revision,
                instructions="  remove silence  ",
            )
        with self.fixture.database.immediate_session() as session:
            claim = dispatch.claim_in_session(
                session,
                run_id=run["id"],
                claim_key=f"claim-{suffix:0>4}",
                lease_seconds=30,
            )
        return run, claim

    def test_create_claim_replay_and_cue_only_packet(self):
        _media_edit, dispatch = self._prepared()
        run, claim = self._create_and_claim(dispatch)
        with self.fixture.database.session() as session:
            batch = session.get(MediaEditDispatchBatch, claim["batch_id"])
            self.assertIsNotNone(batch)
            self.assertEqual("remove silence", run["instructions"])
            self.assertEqual(1, run["batch_count"])
            self.assertGreaterEqual(len(batch.input_json["cues"]), 1)
            self.assertNotIn('"words"', json.dumps(batch.input_json))

        with self.fixture.database.immediate_session() as session:
            replay = dispatch.claim_in_session(
                session,
                run_id=run["id"],
                claim_key="claim-0001",
                lease_seconds=30,
            )
        self.assertEqual(claim["lease_token"], replay["lease_token"])
        self.assertEqual(claim["batch_id"], replay["batch_id"])

    def test_cue_packet_omits_cues_wholly_outside_media(self):
        cues = MediaEditDispatchRunService._cue_evidence(
            SimpleNamespace(
                duration_ms=1_000,
                cues_json=[
                    {"id": "inside", "start_ms": 900, "end_ms": 1_100, "text": "last"},
                    {
                        "id": "outside",
                        "start_ms": 1_100,
                        "end_ms": 1_200,
                        "text": "after",
                    },
                ],
            )
        )
        self.assertEqual(["inside"], [cue["id"] for cue in cues])

    def test_create_rejects_blank_instructions(self):
        _media_edit, dispatch = self._prepared()
        with (
            self.fixture.database.immediate_session() as session,
            self.assertRaises(DispatchError) as raised,
        ):
            dispatch.create_in_session(
                session,
                session_id=self.fixture.session_id,
                revision=1,
                instructions="  ",
            )
        self.assertEqual("invalid_instructions", raised.exception.code)
        self.assertEqual(422, raised.exception.status)

    def test_invalid_result_keeps_lease_and_empty_result_materializes(self):
        _media_edit, dispatch = self._prepared()
        run, claim = self._create_and_claim(dispatch)
        with self.fixture.database.immediate_session() as session:
            with self.assertRaises(DispatchError) as raised:
                dispatch.submit_in_session(
                    session,
                    batch_id=claim["batch_id"],
                    lease_token=claim["lease_token"],
                    submission_key="submit-0001",
                    result={
                        "kind": "media_edit",
                        "cuts": [
                            {
                                "start_cue_id": "missing",
                                "end_cue_id": "missing",
                                "reason": "bad cue",
                            }
                        ],
                    },
                )
            self.assertEqual(422, raised.exception.status)
            batch = session.get(MediaEditDispatchBatch, claim["batch_id"])
            self.assertEqual("leased", batch.status)
            self.assertEqual(claim["lease_token"], batch.lease_token)

        with self.fixture.database.session() as session:
            source = session.get(MediaEditPlanRevision, run["source_revision_id"])
            source_ranges = source.keep_ranges_json
        with self.fixture.database.immediate_session() as session:
            result, status = dispatch.submit_in_session(
                session,
                batch_id=claim["batch_id"],
                lease_token=claim["lease_token"],
                submission_key="submit-0001",
                result={"kind": "media_edit", "cuts": []},
            )
        self.assertEqual(200, status)
        self.assertTrue(result["finalized"])
        self.assertEqual(1, result["source_revision_number"])
        self.assertEqual(2, result["result_revision"])
        with self.fixture.database.session() as session:
            run_record = session.get(MediaEditDispatchRun, run["id"])
            revision = session.get(MediaEditPlanRevision, result["result_revision_id"])
            self.assertEqual("completed", run_record.status)
            self.assertFalse(revision.reviewed)
            self.assertEqual(source_ranges, revision.keep_ranges_json)
            self.assertEqual([], revision.evidence_json["agent_proposal"]["cuts"])

        with self.fixture.database.immediate_session() as session:
            replay, replay_status = dispatch.submit_in_session(
                session,
                batch_id=claim["batch_id"],
                lease_token=claim["lease_token"],
                submission_key="submit-0001",
                result={"kind": "media_edit", "cuts": []},
            )
        self.assertEqual(200, replay_status)
        self.assertEqual(result, replay)

    def test_proposal_preserves_pinned_manual_cuts_labels_and_replays(self):
        media_edit, dispatch = self._prepared()
        prior = media_edit.update(
            self.fixture.session_id, 1,
            keep_ranges=[
                {"start_ms": 0, "end_ms": 500, "label": "Opening"},
                {"start_ms": 4500, "end_ms": 5000, "label": "Closing"},
            ], reviewed=True,
        )["plan"]
        _run, claim = self._create_and_claim(dispatch, revision=2, suffix="keep")
        cue_id = claim["batch"]["cues"][0]["id"]
        proposed = {"kind": "media_edit", "cuts": [{
            "start_cue_id": cue_id, "end_cue_id": cue_id,
            "reason": "Remove speech already outside retained ranges.",
        }]}
        with self.fixture.database.immediate_session() as session:
            result, status = dispatch.submit_in_session(
                session, batch_id=claim["batch_id"], lease_token=claim["lease_token"],
                submission_key="submit-keep", result=proposed,
            )
        self.assertEqual(200, status)
        final = media_edit.revision(self.fixture.session_id, result["result_revision"])
        assert final is not None
        self.assertEqual(prior["keep_ranges"], final["keep_ranges"])
        self.assertFalse(final["reviewed"])
        self.assertEqual(prior["revision_id"], final["parent_revision_id"])
        source = media_edit.revision(self.fixture.session_id, 2)
        assert source is not None
        self.assertEqual(prior["keep_ranges"], source["keep_ranges"])
        with self.fixture.database.immediate_session() as session:
            replay, replay_status = dispatch.submit_in_session(
                session, batch_id=claim["batch_id"], lease_token=claim["lease_token"],
                submission_key="submit-keep", result=proposed,
            )
        self.assertEqual((result, status), (replay, replay_status))
        self.assertIn("Existing cuts are retained", claim["task"]["instructions"])

    def test_nonempty_result_refines_and_records_passive_provenance(self):
        _media_edit, dispatch = self._prepared()
        run, claim = self._create_and_claim(dispatch, suffix="2")
        cue_id = claim["batch"]["cues"][0]["id"]
        with self.fixture.database.immediate_session() as session:
            result, status = dispatch.submit_in_session(
                session,
                batch_id=claim["batch_id"],
                lease_token=claim["lease_token"],
                submission_key="submit-0002",
                result={
                    "kind": "media_edit",
                    "cuts": [
                        {
                            "start_cue_id": cue_id,
                            "end_cue_id": cue_id,
                            "reason": "remove silence",
                        }
                    ],
                },
            )
        self.assertEqual(200, status)
        with self.fixture.database.session() as session:
            revision = session.get(MediaEditPlanRevision, result["result_revision_id"])
            self.assertFalse(revision.reviewed)
            self.assertEqual("passive_dispatch", revision.operation_json["type"])
            self.assertEqual(run["id"], revision.operation_json["dispatch_run_id"])
            self.assertEqual("remove silence", revision.instructions)
            self.assertNotEqual(
                revision.keep_ranges_json,
                session.get(
                    MediaEditPlanRevision, run["source_revision_id"]
                ).keep_ranges_json,
            )

    def test_passive_result_can_remove_captionless_leading_media(self):
        _media_edit, dispatch = self._prepared()
        run, claim = self._create_and_claim(dispatch, suffix="8")
        cue_id = claim["batch"]["cues"][0]["id"]

        with self.fixture.database.immediate_session() as session:
            result, status = dispatch.submit_in_session(
                session,
                batch_id=claim["batch_id"],
                lease_token=claim["lease_token"],
                submission_key="submit-0008",
                result={
                    "kind": "media_edit",
                    "cuts": [
                        {
                            "start_at_media_start": True,
                            "end_cue_id": cue_id,
                            "reason": "Remove captionless setup before the first cue.",
                        }
                    ],
                },
            )

        self.assertEqual(200, status)
        with self.fixture.database.session() as session:
            revision = session.get(MediaEditPlanRevision, result["result_revision_id"])
            cut = revision.operation_json["cuts"][0]
            self.assertEqual(0, cut["start_ms"])
            self.assertTrue(cut["start_at_media_start"])
            self.assertEqual("media_start", cut["start"]["method"])
            self.assertEqual(
                run["id"],
                revision.evidence_json["passive_dispatch"]["dispatch_run_id"],
            )

    def test_revision_conflict_fails_without_rebase(self):
        media_edit, dispatch = self._prepared()
        run, claim = self._create_and_claim(dispatch, suffix="3")
        media_edit.prepare(self.fixture.session_id, force=True)
        with self.fixture.database.immediate_session() as session:
            with self.assertRaises(DispatchError) as raised:
                dispatch.submit_in_session(
                    session,
                    batch_id=claim["batch_id"],
                    lease_token=claim["lease_token"],
                    submission_key="submit-0003",
                    result={"kind": "media_edit", "cuts": []},
                )
            self.assertEqual(409, raised.exception.status)
            self.assertTrue(raised.exception.details["batch_accepted"])
        with self.fixture.database.session() as session:
            saved_run = session.get(MediaEditDispatchRun, run["id"])
            saved_batch = session.get(MediaEditDispatchBatch, claim["batch_id"])
            self.assertEqual("failed", saved_run.status)
            self.assertEqual("completed", saved_batch.status)
            self.assertIsNone(saved_run.result_revision_id)

    def test_deterministic_materialization_error_fails_accepted_run(self):
        media_edit, dispatch = self._prepared()
        run, claim = self._create_and_claim(dispatch, suffix="7")
        with (
            patch.object(
                media_edit,
                "apply_proposal_in_session",
                side_effect=ValueError(
                    "The proposal would remove the entire recording."
                ),
            ),
            self.fixture.database.immediate_session() as session,
        ):
            with self.assertRaises(DispatchError) as raised:
                dispatch.submit_in_session(
                    session,
                    batch_id=claim["batch_id"],
                    lease_token=claim["lease_token"],
                    submission_key="submit-0007",
                    result={"kind": "media_edit", "cuts": []},
                )
            self.assertEqual("materialization_rejected", raised.exception.code)
            self.assertEqual(422, raised.exception.status)
        with self.fixture.database.session() as session:
            saved_run = session.get(MediaEditDispatchRun, run["id"])
            saved_batch = session.get(MediaEditDispatchBatch, claim["batch_id"])
            self.assertEqual("failed", saved_run.status)
            self.assertEqual("completed", saved_batch.status)

    def test_partial_sql_failure_preserves_acceptance_and_exact_retry_materializes_once(self):
        media_edit, dispatch = self._prepared()
        run, claim = self._create_and_claim(dispatch, suffix="sql")
        rendered, derived = _rendered_pair(self.fixture)
        before = _materialization_snapshot(self.fixture)
        files = {
            self.fixture.paths.managed_path(item.relative_path): self.fixture.paths.managed_path(
                item.relative_path
            ).read_bytes()
            for item in (rendered, derived)
        }
        original_state = media_edit._state_in_session
        observed = {}

        def violate_revision_uniqueness(session, session_id):
            state = original_state(session, session_id)
            self.assertEqual(2, state["plan"]["revision"])
            revision = session.get(MediaEditPlanRevision, state["plan"]["revision_id"])
            assert revision is not None
            observed["attempt_id"] = revision.id
            for artifact_id in (rendered.id, derived.id):
                artifact = session.get(Artifact, artifact_id)
                assert artifact is not None
                self.assertEqual("stale", artifact.state)
            session.add(
                MediaEditPlanRevision(
                    plan_id=revision.plan_id,
                    parent_revision_id=revision.parent_revision_id,
                    revision_number=revision.revision_number,
                    source_media_artifact_id=revision.source_media_artifact_id,
                    editorial_transcript_artifact_id=revision.editorial_transcript_artifact_id,
                    timing_artifact_id=revision.timing_artifact_id,
                    duration_ms=revision.duration_ms,
                    instructions=revision.instructions,
                    keep_ranges_json=list(revision.keep_ranges_json),
                    cues_json=list(revision.cues_json),
                    evidence_json=dict(revision.evidence_json),
                    operation_json=dict(revision.operation_json),
                    reviewed=revision.reviewed,
                    content_hash=revision.content_hash,
                )
            )
            try:
                session.flush()
            except IntegrityError:
                observed["constraint_failure"] = True
                raise
            self.fail("The duplicate plan revision should violate its SQL constraint.")

        result = {"kind": "media_edit", "cuts": []}
        with (
            patch.object(media_edit, "_state_in_session", side_effect=violate_revision_uniqueness),
            self.fixture.database.immediate_session() as session,
        ):
            accepted, status = dispatch.submit_in_session(
                session,
                batch_id=claim["batch_id"],
                lease_token=claim["lease_token"],
                submission_key="submit-sql",
                result=result,
            )
        self.assertTrue(observed["constraint_failure"])
        self.assertEqual(202, status)
        self.assertTrue(accepted["accepted"])
        self.assertFalse(accepted["finalized"])
        self.assertEqual("finalizing", accepted["status"])
        self.assertEqual("materialization_failed", accepted["error_code"])
        self.assertEqual(before, _materialization_snapshot(self.fixture))
        with self.fixture.database.session() as session:
            saved_batch = session.get(MediaEditDispatchBatch, claim["batch_id"])
            saved_run = session.get(MediaEditDispatchRun, run["id"])
            assert saved_batch is not None and saved_run is not None
            self.assertEqual("completed", saved_batch.status)
            self.assertEqual("submit-sql", saved_batch.submission_key)
            self.assertIsNone(saved_batch.lease_expires_at)
            self.assertIsNone(saved_run.result_revision_id)
            self.assertIsNone(session.get(MediaEditPlanRevision, observed["attempt_id"]))
            accepted_hash = saved_batch.output_hash
        with self.fixture.database.immediate_session() as session:
            completed, completed_status = dispatch.submit_in_session(
                session,
                batch_id=claim["batch_id"],
                lease_token="retired-token",
                submission_key="submit-sql",
                result=result,
            )
        self.assertEqual(200, completed_status)
        self.assertTrue(completed["accepted"] and completed["finalized"])
        self.assertEqual(2, completed["result_revision"])
        with patch.object(dispatch, "_materialize", wraps=dispatch._materialize) as materialize:
            with self.fixture.database.immediate_session() as session:
                replay, replay_status = dispatch.submit_in_session(
                    session,
                    batch_id=claim["batch_id"],
                    lease_token="retired-token",
                    submission_key="submit-sql",
                    result=result,
                )
            materialize.assert_not_called()
        self.assertEqual((completed, completed_status), (replay, replay_status))
        with self.fixture.database.session() as session:
            revisions = session.scalars(select(MediaEditPlanRevision)).all()
            self.assertEqual([1, 2], sorted(r.revision_number for r in revisions))
            saved_batch = session.get(MediaEditDispatchBatch, claim["batch_id"])
            assert saved_batch is not None
            self.assertEqual(accepted_hash, saved_batch.output_hash)
            for artifact_id in (rendered.id, derived.id):
                artifact = session.get(Artifact, artifact_id)
                assert artifact is not None
                self.assertEqual("stale", artifact.state)
        self.assertTrue(all(path.read_bytes() == content for path, content in files.items()))

    def test_renew_release_and_expired_lease_reclaim(self):
        _media_edit, dispatch = self._prepared()
        run, claim = self._create_and_claim(dispatch, suffix="4")
        with self.fixture.database.immediate_session() as session:
            renewed = dispatch.renew_in_session(
                session,
                batch_id=claim["batch_id"],
                lease_token=claim["lease_token"],
                lease_seconds=30,
            )
            self.assertEqual("leased", renewed["status"])
            released = dispatch.release_in_session(
                session,
                batch_id=claim["batch_id"],
                lease_token=claim["lease_token"],
            )
            self.assertEqual("ready", released["status"])
        with self.fixture.database.immediate_session() as session:
            reclaimed = dispatch.claim_in_session(
                session,
                run_id=run["id"],
                claim_key="claim-0005",
                lease_seconds=30,
            )
            batch = session.get(MediaEditDispatchBatch, claim["batch_id"])
            batch.lease_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        with self.fixture.database.immediate_session() as session:
            expired_reclaimed = dispatch.claim_in_session(
                session,
                run_id=run["id"],
                claim_key="claim-0006",
                lease_seconds=30,
            )
        self.assertNotEqual(reclaimed["lease_token"], expired_reclaimed["lease_token"])


class MediaEditDispatchRouteTests(unittest.TestCase):
    def test_post_write_rejection_is_durable_and_mcp_preserves_acceptance_details(self):
        fixture = _media_edit_fixture.MediaEditServiceTests()
        fixture.setUp()
        app = None
        try:
            bootstrap = BootstrapTokenStore()
            token = bootstrap.issue()
            app = create_app(
                data_root=str(fixture.paths.root),
                testing=True,
                bootstrap_tokens=bootstrap,
            )
            http = app.test_client()
            csrf = http.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
            services = app.extensions["pandrator"]
            fixture.database.dispose()
            fixture.database = services["database"]
            fixture.artifacts = services["artifacts"]
            fixture.session_id = (
                services["sessions"]
                .create(
                    "Failure receipt",
                    workflow_kind="media_edit",
                )
                .id
            )
            fixture._seed_external()
            fixture._service().prepare(fixture.session_id)
            rendered, derived = _rendered_pair(fixture)
            media_edit = services["media_edit"]
            dispatch = services["media_edit_dispatch"]
            response = http.post(
                f"/api/v1/sessions/{fixture.session_id}/media-edit-dispatch-runs",
                json={"revision": 1, "instructions": "trim"},
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "create-receipt"},
            )
            self.assertEqual(201, response.status_code)
            run_id = response.get_json()["id"]
            response = http.post(
                f"/api/v1/media-edit-dispatch-runs/{run_id}/claim",
                json={"lease_seconds": 30},
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "claim-receipt"},
            )
            self.assertEqual(200, response.status_code)
            claim = response.get_json()
            before = _materialization_snapshot(fixture)
            original_state = media_edit._state_in_session
            observed = {}

            def reject_after_adoption(session, session_id):
                state = original_state(session, session_id)
                self.assertEqual(2, state["plan"]["revision"])
                observed["attempt_id"] = state["plan"]["revision_id"]
                for artifact_id in (rendered.id, derived.id):
                    artifact = session.get(Artifact, artifact_id)
                    assert artifact is not None
                    self.assertEqual("stale", artifact.state)
                raise ValueError("Injected rejection after revision adoption.")

            path = f"/api/v1/media-edit-dispatch-batches/{claim['batch_id']}/submit"
            headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "submit-receipt"}
            payload = {
                "lease_token": claim["lease_token"],
                "result": {"kind": "media_edit", "cuts": []},
            }
            with patch.object(media_edit, "_state_in_session", side_effect=reject_after_adoption):
                rejection = http.post(path, json=payload, headers=headers)
            self.assertEqual(422, rejection.status_code)
            error = rejection.get_json()["error"]
            self.assertEqual("materialization_rejected", error["code"])
            self.assertEqual(
                {"batch_accepted": True, "run_id": run_id, "retryable": False}, error["details"]
            )
            self.assertEqual(before, _materialization_snapshot(fixture))
            with fixture.database.session() as session:
                saved_run = session.get(MediaEditDispatchRun, run_id)
                saved_batch = session.get(MediaEditDispatchBatch, claim["batch_id"])
                assert saved_run is not None and saved_batch is not None
                self.assertEqual("failed", saved_run.status)
                self.assertEqual("completed", saved_batch.status)
                self.assertIsNone(saved_run.result_revision_id)
                self.assertIsNone(session.get(MediaEditPlanRevision, observed["attempt_id"]))
            with patch.object(dispatch, "_materialize", wraps=dispatch._materialize) as materialize:
                replay = http.post(path, json=payload, headers=headers)
                materialize.assert_not_called()
            self.assertEqual(202, replay.status_code)
            self.assertEqual("failed", replay.get_json()["status"])
            self.assertTrue(replay.get_json()["accepted"])
            self.assertFalse(replay.get_json()["finalized"])
            self.assertEqual(before, _materialization_snapshot(fixture))

            transport = _client_fixture.FakeSession(
                [
                    _client_fixture.FakeResponse(rejection.status_code, rejection.get_json()),
                ]
            )
            client = ApplicationClient(
                _client_fixture.local_registry("http://127.0.0.1:8097").bind("local"),
                CredentialResolver(()),
                session=transport,
                local_bootstrap=lambda _target, _session: "csrf-value",
            )
            with self.assertRaises(PandratorMcpError) as failure:
                client.submit_media_edit_dispatch_batch(
                    claim["batch_id"],
                    lease_token=claim["lease_token"],
                    result=payload["result"],
                    idempotency_key="submit-receipt",
                )
            assert failure.exception.code == "materialization_rejected"
            self.assertEqual(
                {"batch_accepted": True, "run_id": run_id, "retryable": False, "status": 422},
                failure.exception.details,
            )
            typed = ToolFailure(
                code=failure.exception.code,
                message=str(failure.exception),
                request_id="receipt-proof",
                details=failure.exception.details,
            )
            self.assertEqual("materialization_rejected", typed.model_dump()["code"])
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(b"edited", fixture.paths.managed_path(rendered.relative_path).read_bytes())
            self.assertEqual(
                b"subtitles", fixture.paths.managed_path(derived.relative_path).read_bytes()
            )
        finally:
            if app is not None:
                app.extensions["pandrator"]["database"].dispose()
            fixture.tearDown()

    def test_create_route_is_strict_and_wires_run_service(self):
        temporary = tempfile.TemporaryDirectory()
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        app = create_app(
            data_root=temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        try:
            client = app.test_client()
            csrf = client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
            session = app.extensions["pandrator"]["sessions"].create(
                "Media edit", workflow_kind="media_edit"
            )
            service = app.extensions["pandrator"]["media_edit_dispatch"]
            payload = {
                "id": "run-1",
                "session_id": session.id,
                "instructions": "trim",
                "status": "ready",
            }
            with patch.object(
                service, "create_in_session", return_value=payload
            ) as create:
                response = client.post(
                    f"/api/v1/sessions/{session.id}/media-edit-dispatch-runs",
                    json={"revision": 1, "instructions": " trim "},
                    headers={"X-CSRF-Token": csrf},
                )
            self.assertEqual(201, response.status_code)
            create.assert_called_once()
            self.assertEqual("trim", response.get_json()["instructions"])
            strict = client.post(
                f"/api/v1/sessions/{session.id}/media-edit-dispatch-runs",
                json={"revision": 1, "instructions": "trim", "extra": True},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(422, strict.status_code)
        finally:
            app.extensions["pandrator"]["database"].dispose()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
