"""Accepted subtitle work must not keep the SQLite writer during preparation."""

import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from sqlalchemy import select

from pandrator.web.artifact_selection import choose_artifact
from pandrator.web.database import Database
from pandrator.web.models import ApiIdempotency, Artifact, DispatchBatch, DispatchRun
from tests import test_web_dispatch as dispatch_support


class DispatchShortTransactionTests(unittest.TestCase):
    setUp = dispatch_support.DispatchWebTests.setUp
    tearDown = dispatch_support.DispatchWebTests.tearDown
    _source = dispatch_support.DispatchWebTests._source
    _create = dispatch_support.DispatchWebTests._create
    _claim = dispatch_support.DispatchWebTests._claim
    _headers = dispatch_support.DispatchWebTests._headers
    _stage_artifact = dispatch_support.DispatchWebTests._stage_artifact

    def _case(self, suffix="main"):
        session_id, source_id = self._source()
        run = self._create(session_id, source_artifact_id=source_id)
        claim = self._claim(run["id"], f"short-claim-{suffix}")
        body = {
            "lease_token": claim["lease_token"],
            "result": {"kind": "correction", "operations": []},
        }
        return session_id, source_id, run, claim, body

    def _submit(self, claim, body, key="short-submit-main"):
        response = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json=body,
            headers=self._headers(key),
        )
        return response.status_code, response.get_json()

    def _outputs(self, run_id):
        with self.extension["database"].session() as session:
            return [
                artifact.id
                for artifact in session.scalars(select(Artifact)).all()
                if (artifact.metadata_json or {}).get("dispatch_run_id") == run_id
            ]

    def test_http_acceptance_commits_and_heartbeat_writes_during_cpu_preparation(self):
        _, _, run, claim, body = self._case()
        _, _, _, other_claim, _ = self._case("heartbeat")
        dispatch = self.extension["dispatch"]
        prepare = dispatch._prepare_materialization
        entered, release = threading.Event(), threading.Event()

        def held(snapshot):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test preparation barrier timed out")
            return prepare(snapshot)

        other_database = Database(self.extension["database"].path)
        try:
            with patch.object(dispatch, "_prepare_materialization", side_effect=held):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self._submit, claim, body)
                    try:
                        self.assertTrue(entered.wait(10))
                        with other_database.snapshot_session() as session:
                            accepted = session.get(DispatchBatch, claim["batch_id"])
                            pending = session.get(DispatchRun, run["id"])
                            reservation = session.scalar(
                                select(ApiIdempotency).where(
                                    ApiIdempotency.idempotency_key == "short-submit-main"
                                )
                            )
                            self.assertEqual("completed", accepted.status)
                            self.assertEqual("finalizing", pending.status)
                            self.assertEqual(202, reservation.status_code)
                            self.assertIsNone(pending.result_artifact_id)
                        heartbeat = other_database.run_immediate(
                            lambda session: dispatch.renew_in_session(
                                session,
                                batch_id=other_claim["batch_id"],
                                lease_token=other_claim["lease_token"],
                                lease_seconds=120,
                            ),
                            timeout_seconds=0.5,
                            busy_timeout_ms=25,
                        )
                        self.assertEqual("leased", heartbeat["status"])
                    finally:
                        release.set()
                    status, result = future.result(15)
            self.assertEqual(200, status, result)
            self.assertEqual(1, len(self._outputs(run["id"])))
        finally:
            other_database.dispose()

    def test_prepare_failure_and_crash_after_acceptance_resume_exact_key_once(self):
        _, _, run, claim, body = self._case()
        dispatch = self.extension["dispatch"]
        with patch.object(
            dispatch, "_prepare_materialization", side_effect=OSError("synthetic disk failure")
        ):
            status, result = self._submit(claim, body)
        self.assertEqual(202, status, result)
        self.assertEqual("materialization_failed", result["error_code"])
        self.assertEqual([], self._outputs(run["id"]))
        status, result = self._submit(claim, body)
        self.assertEqual(200, status, result)
        status, replay = self._submit(claim, body)
        self.assertEqual(200, status, replay)
        self.assertEqual(result["result_artifact_id"], replay["result_artifact_id"])
        self.assertEqual(1, len(self._outputs(run["id"])))

    def test_crash_before_prepare_leaves_accepted_reservation_replayable(self):
        _, _, run, claim, body = self._case()
        dispatch = self.extension["dispatch"]
        with patch.object(
            dispatch, "finalize_pending", side_effect=SystemExit("synthetic process loss")
        ):
            with self.assertRaises(SystemExit):
                self._submit(claim, body)
        with self.extension["database"].session() as session:
            self.assertEqual("finalizing", session.get(DispatchRun, run["id"]).status)
            self.assertEqual("completed", session.get(DispatchBatch, claim["batch_id"]).status)
        status, result = self._submit(claim, body)
        self.assertEqual(200, status, result)
        self.assertEqual(1, len(self._outputs(run["id"])))

    def test_concurrent_preparations_publish_one_artifact_and_keep_winner_file(self):
        _, _, run, claim, body = self._case()
        dispatch = self.extension["dispatch"]
        with patch.object(
            dispatch, "_prepare_materialization", side_effect=OSError("delay publication")
        ):
            self.assertEqual(202, self._submit(claim, body)[0])
        prepare = dispatch._prepare_materialization
        barrier = threading.Barrier(2)
        paths = []

        def held(snapshot):
            prepared = prepare(snapshot)
            paths.append(prepared.destination)
            barrier.wait(10)
            return prepared

        with patch.object(dispatch, "_prepare_materialization", side_effect=held):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(dispatch.finalize_pending, run["id"]) for _ in range(2)]
                results = [future.result(20) for future in futures]
        self.assertTrue(all(status == 200 for _, status in results), results)
        self.assertEqual(results[0][0]["result_artifact_id"], results[1][0]["result_artifact_id"])
        self.assertEqual(1, len(self._outputs(run["id"])))
        self.assertEqual(1, sum(path.exists() for path in paths))

    def test_settings_and_source_changes_during_prepare_refuse_publication(self):
        for field in ("settings", "source", "selection", "head", "physical_source"):
            with self.subTest(field=field):
                _, source_id, run, claim, body = self._case(field)
                dispatch = self.extension["dispatch"]
                prepare = dispatch._prepare_materialization

                def changed(snapshot, prepare=prepare, field=field, run=run, source_id=source_id):
                    prepared = prepare(snapshot)
                    if field == "head":
                        self._stage_artifact(
                            run["session_id"],
                            name="new-head.srt",
                            role="correction",
                            text="Newer result.",
                            parent_id=source_id,
                        )
                        return prepared
                    if field == "selection":
                        selected_id = self._stage_artifact(
                            run["session_id"],
                            name="new-source.srt",
                            role="transcription",
                            text="New source.",
                            parent_id=source_id,
                        )
                        with self.extension["database"].immediate_session() as session:
                            choose_artifact(session, run["session_id"], "transcribe", selected_id)
                        return prepared
                    if field == "physical_source":
                        path = self.extension["artifacts"].paths.managed_path(
                            snapshot.source.relative_path
                        )
                        path.write_text("changed physical content", encoding="utf-8")
                        return prepared
                    with self.extension["database"].immediate_session() as session:
                        if field == "settings":
                            pending = session.get(DispatchRun, run["id"])
                            pending.settings_json = {**pending.settings_json, "changed": True}
                        else:
                            session.get(Artifact, source_id).content_hash = "changed"
                    return prepared

                with patch.object(dispatch, "_prepare_materialization", side_effect=changed):
                    status, result = self._submit(claim, body, f"short-submit-{field}")
                self.assertEqual(409, status, result)
                replay_status, replay = self._submit(claim, body, f"short-submit-{field}")
                self.assertEqual(409, replay_status, replay)
                self.assertEqual(result["error"]["code"], replay["error"]["code"])
                self.assertTrue(replay["error"]["details"]["batch_accepted"])
                self.assertEqual([], self._outputs(run["id"]))
                with self.extension["database"].session() as session:
                    self.assertEqual(
                        "completed", session.get(DispatchBatch, claim["batch_id"]).status
                    )
                    self.assertEqual("failed", session.get(DispatchRun, run["id"]).status)

    def test_cancellation_during_prepare_does_not_resurrect_run(self):
        _, _, run, claim, body = self._case()
        dispatch = self.extension["dispatch"]
        prepare = dispatch._prepare_materialization

        def cancelled(snapshot):
            prepared = prepare(snapshot)
            self.extension["database"].run_immediate(
                lambda session: dispatch.terminate_in_session(
                    session,
                    run_id=run["id"],
                    expected_status="finalizing",
                    action="cancelled",
                    reason="Synthetic cancellation during preparation",
                )
            )
            return prepared

        with patch.object(dispatch, "_prepare_materialization", side_effect=cancelled):
            status, result = self._submit(claim, body)
        self.assertEqual(202, status, result)
        self.assertEqual("cancelled", result["status"])
        self.assertEqual([], self._outputs(run["id"]))
        with self.extension["database"].session() as session:
            self.assertEqual("cancelled", session.get(DispatchRun, run["id"]).status)

    def test_busy_publication_budget_returns_durable_accepted_receipt(self):
        _, _, run, claim, body = self._case()
        dispatch = self.extension["dispatch"]
        original = dispatch._prepare_materialization
        connection = sqlite3.connect(self.extension["database"].path, timeout=0.1)

        def held(snapshot):
            prepared = original(snapshot)
            connection.execute("BEGIN IMMEDIATE")
            return prepared

        try:
            with patch.object(dispatch, "_prepare_materialization", side_effect=held):
                status, result = self._submit(claim, body)
            self.assertEqual(202, status, result)
            self.assertEqual("finalizing", result["status"])
            self.assertIsNone(result["result_artifact_id"])
        finally:
            connection.rollback()
            connection.close()
        status, result = self._submit(claim, body)
        self.assertEqual(200, status, result)
        self.assertEqual(1, len(self._outputs(run["id"])))

    def test_serialized_text_loss_is_refused_and_accepted_work_remains_retryable(self):
        _, _, run, claim, body = self._case()
        with patch(
            "pandrator.web.dispatch._finalize_dispatch_values",
            return_value=[
                {
                    "start_ms": 0,
                    "end_ms": 1000,
                    "text": "Lost accepted text.",
                }
            ],
        ):
            status, result = self._submit(claim, body)
        self.assertEqual(202, status, result)
        self.assertEqual("materialization_failed", result["error_code"])
        self.assertEqual([], self._outputs(run["id"]))
        self.assertEqual(200, self._submit(claim, body)[0])
