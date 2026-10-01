import tempfile
import unittest

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import DispatchBatch, DispatchRun
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.source_management import assert_session_idle
from tests.web_test_support import prepare_web_test_data_root


class DispatchLifecycleControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
            background_maintenance=False,
        )
        self.client = self.app.test_client()
        token = bootstrap.issue()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.extension = self.app.extensions["pandrator"]
        self.database = self.extension["database"]
        self.source_counter = 0

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _headers(self, key):
        return {"X-CSRF-Token": self.csrf, "Idempotency-Key": key}

    def _source(self, *, target_language="pl"):
        record = self.extension["sessions"].create(
            f"Dispatch lifecycle {self.source_counter}",
            workflow_kind="subtitles",
            source_language="en",
            target_language=target_language,
        )
        self.source_counter += 1
        directory = self.extension["paths"].sessions / record.storage_key
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "source.srt"
        path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nFirst cue.\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nSecond cue.\n",
            encoding="utf-8",
        )
        artifact = self.extension["artifacts"].register(
            path,
            kind="srt",
            role="transcription",
            session_id=record.id,
        )
        self.extension["workflow_handlers"]._store_srt_document(
            record.id, artifact, "transcription", language="en"
        )
        return record.id, artifact.id

    def _create_run(self, session_id, source_id, *, kind="correction"):
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/dispatch-runs",
            json={
                "kind": kind,
                "source_artifact_id": source_id,
                "max_segments_per_batch": 1,
            },
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def _claim(self, run_id, key):
        response = self.client.post(
            f"/api/v1/dispatch-runs/{run_id}/claim",
            json={},
            headers=self._headers(key),
        )
        self.assertEqual(200, response.status_code, response.get_json())
        return response.get_json()

    def _submit(self, claim, key):
        return self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "response_text": '{"operations": []}',
            },
            headers=self._headers(key),
        )

    def _terminate(
        self,
        run_id,
        *,
        expected_status,
        action,
        reason,
        key,
        replacement_run_id=None,
    ):
        body = {
            "expected_status": expected_status,
            "action": action,
            "reason": reason,
        }
        if replacement_run_id is not None:
            body["replacement_run_id"] = replacement_run_id
        return self.client.post(
            f"/api/v1/dispatch-runs/{run_id}/terminate",
            json=body,
            headers=self._headers(key),
        )

    def _assert_idle(self, session_id):
        with self.database.session() as session:
            assert_session_idle(session, session_id)

    def test_partial_cancellation_unblocks_idle_and_preserves_accepted_batch(self):
        session_id, source_id = self._source()
        run = self._create_run(session_id, source_id)
        first_claim = self._claim(run["id"], "lifecycle-claim-first")
        self.assertEqual(
            run["source_content_hash"],
            first_claim["task"]["source_content_hash"],
        )
        accepted = self._submit(first_claim, "lifecycle-submit-first")
        self.assertEqual(200, accepted.status_code, accepted.get_json())
        second_claim = self._claim(run["id"], "lifecycle-claim-second")

        with self.assertRaises(RevisionConflict):
            self._assert_idle(session_id)

        body = {
            "expected_status": "running",
            "action": "cancelled",
            "reason": "The selected source changed.",
        }
        terminated = self._terminate(
            run["id"],
            expected_status="running",
            action="cancelled",
            reason="The selected source changed.",
            key="lifecycle-terminate-001",
        )
        self.assertEqual(200, terminated.status_code, terminated.get_json())
        self.assertEqual("cancelled", terminated.get_json()["status"])
        self.assertEqual("The selected source changed.", terminated.get_json()["error_message"])
        self.assertEqual("cancelled", terminated.get_json()["termination"]["action"])

        replay = self.client.post(
            f"/api/v1/dispatch-runs/{run['id']}/terminate",
            json=body,
            headers=self._headers("lifecycle-terminate-001"),
        )
        self.assertEqual(200, replay.status_code, replay.get_json())
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual(terminated.get_json(), replay.get_json())

        late_claim = self.client.post(
            f"/api/v1/dispatch-runs/{run['id']}/claim",
            json={},
            headers=self._headers("lifecycle-claim-second"),
        )
        self.assertEqual(409, late_claim.status_code, late_claim.get_json())
        self.assertEqual("run_not_claimable", late_claim.get_json()["error"]["code"])

        late_submit = self._submit(second_claim, "lifecycle-submit-late")
        self.assertEqual(409, late_submit.status_code, late_submit.get_json())
        self.assertEqual("run_not_submitable", late_submit.get_json()["error"]["code"])

        accepted_replay = self._submit(first_claim, "lifecycle-submit-first")
        self.assertEqual(200, accepted_replay.status_code, accepted_replay.get_json())
        self.assertEqual("true", accepted_replay.headers["Idempotency-Replayed"])
        self.assertEqual("cancelled", self.client.get(
            f"/api/v1/dispatch-runs/{run['id']}"
        ).get_json()["status"])

        with self.database.session() as session:
            stored_run = session.get(DispatchRun, run["id"])
            batches = list(
                session.scalars(
                    select(DispatchBatch)
                    .where(DispatchBatch.dispatch_run_id == run["id"])
                    .order_by(DispatchBatch.ordinal)
                ).all()
            )
            self.assertEqual(1, stored_run.completed_batch_count)
            self.assertEqual("completed", batches[0].status)
            self.assertEqual(accepted.get_json()["batch_id"], batches[0].id)
            self.assertIsNotNone(batches[0].accepted_at)
            self.assertEqual("cancelled", batches[1].status)
            self.assertIsNone(batches[1].lease_token)
            self.assertIsNone(batches[1].lease_expires_at)

        self._assert_idle(session_id)

    def test_stale_status_and_incompatible_replacement_conflict(self):
        session_id, source_id = self._source()
        run = self._create_run(session_id, source_id)
        translation = self._create_run(session_id, source_id, kind="translation")

        incompatible = self._terminate(
            run["id"],
            expected_status="ready",
            action="superseded",
            reason="A newer run is selected.",
            key="lifecycle-replacement-conflict",
            replacement_run_id=translation["id"],
        )
        self.assertEqual(409, incompatible.status_code, incompatible.get_json())
        self.assertEqual(
            "replacement_run_conflict",
            incompatible.get_json()["error"]["code"],
        )

        cancelled = self._terminate(
            run["id"],
            expected_status="ready",
            action="cancelled",
            reason="No longer needed.",
            key="lifecycle-cancel-stale-check",
        )
        self.assertEqual(200, cancelled.status_code, cancelled.get_json())
        stale = self._terminate(
            run["id"],
            expected_status="ready",
            action="cancelled",
            reason="No longer needed.",
            key="lifecycle-stale-status",
        )
        self.assertEqual(409, stale.status_code, stale.get_json())
        self.assertEqual("run_status_conflict", stale.get_json()["error"]["code"])
        self.assertEqual("cancelled", stale.get_json()["error"]["details"]["actual_status"])

    def test_supersession_accepts_matching_replacement_and_idle_tracks_other_runs(self):
        session_id, source_id = self._source()
        original = self._create_run(session_id, source_id)
        replacement = self._create_run(session_id, source_id)
        unrelated = self._create_run(session_id, source_id)

        superseded = self._terminate(
            original["id"],
            expected_status="ready",
            action="superseded",
            reason="Replacing this run.",
            key="lifecycle-supersede-001",
            replacement_run_id=replacement["id"],
        )
        self.assertEqual(200, superseded.status_code, superseded.get_json())
        self.assertEqual("superseded", superseded.get_json()["status"])
        self.assertEqual(
            replacement["id"],
            superseded.get_json()["termination"]["replacement_run_id"],
        )
        with self.assertRaises(RevisionConflict):
            self._assert_idle(session_id)

        for run, key in ((replacement, "lifecycle-cancel-replacement"), (unrelated, "lifecycle-cancel-unrelated")):
            response = self._terminate(
                run["id"],
                expected_status="ready",
                action="cancelled",
                reason="End test work.",
                key=key,
            )
            self.assertEqual(200, response.status_code, response.get_json())
        self._assert_idle(session_id)


if __name__ == "__main__":
    unittest.main()
