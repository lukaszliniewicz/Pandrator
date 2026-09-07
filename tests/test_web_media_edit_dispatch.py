import json
import tempfile
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.dispatch import DispatchError
from pandrator.web.media_edit_dispatch import MediaEditDispatchRunService
from pandrator.web.models import (
    MediaEditDispatchBatch,
    MediaEditDispatchRun,
    MediaEditPlanRevision,
)
from tests import test_web_media_edit as _media_edit_fixture


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
