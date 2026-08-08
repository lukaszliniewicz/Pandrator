import multiprocessing
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker, noop_handler
from pandrator.web.models import Job, ResourceClaim, utcnow
from tests.web_test_support import prepare_web_test_data_root


def _process_claim(
    database_path: str,
    worker_id: str,
    ready_queue,
    start_event,
    result_queue,
) -> None:
    """Open an independent engine and contend for one job from a child process."""
    database = Database(Path(database_path))
    try:
        ready_queue.put(worker_id)
        if not start_event.wait(30):
            result_queue.put(("error", worker_id, "start timeout"))
            return
        claimed = JobQueue(database).claim(worker_id)
        result_queue.put(
            (
                "ok",
                worker_id,
                None if claimed is None else (claimed.id, claimed.lease_generation),
            )
        )
    except Exception as error:
        result_queue.put(("error", worker_id, repr(error)))
    finally:
        database.dispose()


class AtomicJobQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = prepare_web_test_data_root(self.temporary.name).database
        self.database = Database(self.database_path)
        self.queue = JobQueue(self.database)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _thread_claim(self, barrier: threading.Barrier, worker_id: str):
        database = Database(self.database_path)
        try:
            barrier.wait(timeout=30)
            claimed = JobQueue(database).claim(worker_id)
            return None if claimed is None else (claimed.id, claimed.lease_generation)
        finally:
            database.dispose()

    def test_exactly_one_of_2_10_and_50_independent_engine_claimers_succeeds(self):
        for count in (2, 10, 50):
            with self.subTest(claimers=count):
                job = self.queue.enqueue("noop")
                barrier = threading.Barrier(count)
                with ThreadPoolExecutor(max_workers=count) as executor:
                    results = list(
                        executor.map(
                            lambda index: self._thread_claim(
                                barrier,
                                f"thread-worker-{count}-{index}",
                            ),
                            range(count),
                        )
                    )

                winners = [result for result in results if result is not None]
                self.assertEqual([(job.id, 1)], winners)
                current = self.queue.get(job.id)
                self.assertEqual("running", current.status)
                self.assertEqual(1, current.attempts)

                # Keep each subtest independent without deleting durable history.
                self.queue.request_cancel(job.id)

    def test_exactly_one_cross_process_claimer_succeeds(self):
        job = self.queue.enqueue("noop")
        context = multiprocessing.get_context("spawn")
        ready_queue = context.Queue()
        result_queue = context.Queue()
        start_event = context.Event()
        processes = [
            context.Process(
                target=_process_claim,
                args=(
                    str(self.database_path),
                    f"process-worker-{index}",
                    ready_queue,
                    start_event,
                    result_queue,
                ),
            )
            for index in range(10)
        ]
        results = []
        try:
            for process in processes:
                process.start()
            for _ in processes:
                ready_queue.get(timeout=45)
            start_event.set()
            for process in processes:
                process.join(timeout=45)
            results = [result_queue.get(timeout=10) for _ in processes]
        finally:
            start_event.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            ready_queue.close()
            result_queue.close()

        self.assertTrue(all(process.exitcode == 0 for process in processes))
        errors = [result for result in results if result[0] == "error"]
        self.assertEqual([], errors)
        winners = [result[2] for result in results if result[2] is not None]
        self.assertEqual([(job.id, 1)], winners)

    def test_multi_key_contention_is_all_or_nothing_and_skips_blocked_job(self):
        holder = self.queue.enqueue("noop", resource_keys=["gpu:0", "voice:alice"])
        blocked = self.queue.enqueue("noop", resource_keys=["free:key", "gpu:0"])
        unrelated = self.queue.enqueue("noop")
        holder_claim = self.queue.claim("holder")
        blocked_claim = self.queue.claim("blocked")

        self.assertTrue(
            self.queue.acquire_resources(
                holder.id,
                "holder",
                holder.resource_keys_json,
                lease_generation=holder_claim.lease_generation,
            )
        )
        self.assertFalse(
            self.queue.acquire_resources(
                blocked.id,
                "blocked",
                blocked.resource_keys_json,
                lease_generation=blocked_claim.lease_generation,
            )
        )
        self.assertTrue(
            self.queue.defer_for_resources(
                blocked.id,
                "blocked",
                lease_generation=blocked_claim.lease_generation,
                retry_delay_seconds=1.0,
            )
        )

        with self.database.session() as session:
            claims = list(
                session.scalars(
                    select(ResourceClaim).order_by(ResourceClaim.resource_key)
                ).all()
            )
        self.assertEqual(
            ["gpu:0", "voice:alice"], [claim.resource_key for claim in claims]
        )
        self.assertTrue(all(claim.job_id == holder.id for claim in claims))

        next_job = self.queue.claim("unrelated-worker")
        self.assertEqual(unrelated.id, next_job.id)
        deferred = self.queue.get(blocked.id)
        self.assertEqual("queued", deferred.status)
        self.assertIsNotNone(deferred.available_at)

    def test_50_resource_contenders_do_not_leak_partial_claims_or_errors(self):
        count = 50
        jobs = [
            self.queue.enqueue(
                "noop",
                resource_keys=["shared:gpu", f"unique:{index}"],
            )
            for index in range(count)
        ]
        claimed = [
            self.queue.claim(f"resource-worker-{index}") for index in range(count)
        ]
        barrier = threading.Barrier(count)

        def acquire(index: int):
            database = Database(self.database_path)
            try:
                barrier.wait(timeout=30)
                return JobQueue(database).acquire_resources(
                    jobs[index].id,
                    f"resource-worker-{index}",
                    jobs[index].resource_keys_json,
                    lease_generation=claimed[index].lease_generation,
                )
            finally:
                database.dispose()

        with ThreadPoolExecutor(max_workers=count) as executor:
            results = list(executor.map(acquire, range(count)))

        self.assertEqual(1, sum(results))
        winner = results.index(True)
        with self.database.session() as session:
            claims = list(session.scalars(select(ResourceClaim)).all())
        self.assertEqual(
            {"shared:gpu", f"unique:{winner}"},
            {claim.resource_key for claim in claims},
        )
        self.assertTrue(all(claim.job_id == jobs[winner].id for claim in claims))

    def test_reclaim_fences_every_stale_worker_mutation(self):
        job = self.queue.enqueue(
            "noop",
            max_attempts=2,
            resource_keys=["gpu:0"],
        )
        stale = self.queue.claim("reused-worker-id")
        self.assertTrue(
            self.queue.acquire_resources(
                job.id,
                "reused-worker-id",
                job.resource_keys_json,
                lease_generation=stale.lease_generation,
            )
        )
        with self.database.immediate_session() as session:
            record = session.get(Job, job.id)
            record.lease_expires_at = utcnow() - timedelta(seconds=1)
            claim = session.get(ResourceClaim, "gpu:0")
            claim.expires_at = utcnow() - timedelta(seconds=1)

        current = self.queue.claim("reused-worker-id")
        self.assertEqual(stale.lease_generation + 1, current.lease_generation)
        self.assertTrue(
            self.queue.acquire_resources(
                job.id,
                "reused-worker-id",
                job.resource_keys_json,
                lease_generation=current.lease_generation,
            )
        )
        self.assertTrue(
            self.queue.heartbeat_resources(
                job.id,
                "reused-worker-id",
                lease_generation=current.lease_generation,
            )
        )

        event_count = len(self.queue.events_for(job.id))
        self.queue.log(
            job.id,
            "INFO",
            "stale log",
            worker_id="reused-worker-id",
            lease_generation=stale.lease_generation,
        )
        self.assertEqual(event_count, len(self.queue.events_for(job.id)))
        self.assertFalse(
            self.queue.heartbeat(
                job.id,
                "reused-worker-id",
                lease_generation=stale.lease_generation,
            )
        )
        self.assertFalse(
            self.queue.heartbeat_resources(
                job.id,
                "reused-worker-id",
                lease_generation=stale.lease_generation,
            )
        )
        self.assertFalse(
            self.queue.fail(
                job.id,
                "reused-worker-id",
                "late_failure",
                "stale failure",
                lease_generation=stale.lease_generation,
            )
        )
        self.assertFalse(
            self.queue.cancel_owned(
                job.id,
                "reused-worker-id",
                lease_generation=stale.lease_generation,
            )
        )
        with self.assertRaises(RuntimeError):
            self.queue.complete(
                job.id,
                "reused-worker-id",
                {"late": True},
                lease_generation=stale.lease_generation,
            )
        self.queue.release_resources(
            job.id,
            "reused-worker-id",
            lease_generation=stale.lease_generation,
        )

        with self.database.session() as session:
            claim = session.get(ResourceClaim, "gpu:0")
            self.assertIsNotNone(claim)
            self.assertEqual(current.lease_generation, claim.lease_generation)
            self.assertEqual("reused-worker-id", claim.lease_owner)
        self.queue.complete(
            job.id,
            "reused-worker-id",
            {"current": True},
            lease_generation=current.lease_generation,
        )
        self.assertEqual("succeeded", self.queue.get(job.id).status)

    def test_cancel_and_complete_are_linearized(self):
        job = self.queue.enqueue("noop")
        claimed = self.queue.claim("worker")
        barrier = threading.Barrier(2)

        def cancel():
            database = Database(self.database_path)
            try:
                barrier.wait(timeout=10)
                return ("cancel", JobQueue(database).request_cancel(job.id).status)
            finally:
                database.dispose()

        def complete():
            database = Database(self.database_path)
            try:
                barrier.wait(timeout=10)
                try:
                    JobQueue(database).complete(
                        job.id,
                        "worker",
                        {"ok": True},
                        lease_generation=claimed.lease_generation,
                    )
                    return ("complete", True)
                except RuntimeError:
                    return ("complete", False)
            finally:
                database.dispose()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda action: action(), (cancel, complete)))

        current = self.queue.get(job.id)
        self.assertIn(current.status, {"cancel_requested", "succeeded"})
        complete_succeeded = dict(outcomes)["complete"]
        self.assertEqual(current.status == "succeeded", complete_succeeded)
        if current.status == "cancel_requested":
            self.assertTrue(
                self.queue.cancel_owned(
                    job.id,
                    "worker",
                    lease_generation=claimed.lease_generation,
                )
            )
            self.assertEqual("canceled", self.queue.get(job.id).status)


class WorkerResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = prepare_web_test_data_root(self.temporary.name).database
        self.database = Database(self.database_path)
        self.queue = JobQueue(self.database)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def test_claim_failure_and_malformed_payload_do_not_stop_later_work(self):
        malformed = self.queue.enqueue("noop", ["not-an-object"])
        healthy = self.queue.enqueue("noop", {"echo": "healthy"})
        worker = Worker(self.queue, "worker", {"noop": noop_handler})
        original_claim = self.queue.claim
        calls = 0

        def fail_first_claim(worker_id, lease_seconds=30):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated claim failure")
            return original_claim(worker_id, lease_seconds)

        with patch.object(self.queue, "claim", side_effect=fail_first_claim):
            self.assertFalse(worker.run_once())
            self.assertTrue(worker.run_once())
            self.assertTrue(worker.run_once())

        self.assertEqual("failed", self.queue.get(malformed.id).status)
        self.assertEqual("TypeError", self.queue.get(malformed.id).error_code)
        self.assertEqual("succeeded", self.queue.get(healthy.id).status)

    def test_resource_acquisition_and_cleanup_failures_are_contained(self):
        first = self.queue.enqueue("noop", {"echo": "first"}, resource_keys=["gpu:0"])
        second = self.queue.enqueue("noop", {"echo": "second"})
        worker = Worker(self.queue, "worker", {"noop": noop_handler})
        original_acquire = self.queue.acquire_resources
        acquire_calls = 0

        def fail_first_acquire(*args, **kwargs):
            nonlocal acquire_calls
            acquire_calls += 1
            if acquire_calls == 1:
                raise RuntimeError("simulated acquire failure")
            return original_acquire(*args, **kwargs)

        with patch.object(
            self.queue, "acquire_resources", side_effect=fail_first_acquire
        ):
            self.assertTrue(worker.run_once())

        deferred = self.queue.get(first.id)
        self.assertEqual("queued", deferred.status)
        self.assertEqual(0, deferred.attempts)
        with self.database.session() as session:
            session.get(Job, first.id).available_at = utcnow() - timedelta(seconds=1)

        original_release = self.queue.release_resources
        release_calls = 0

        def fail_first_release(*args, **kwargs):
            nonlocal release_calls
            release_calls += 1
            if release_calls == 1:
                raise RuntimeError("simulated cleanup failure")
            return original_release(*args, **kwargs)

        with patch.object(
            self.queue, "release_resources", side_effect=fail_first_release
        ):
            self.assertTrue(worker.run_once())
            self.assertTrue(worker.run_once())

        self.assertEqual("succeeded", self.queue.get(first.id).status)
        self.assertEqual("succeeded", self.queue.get(second.id).status)

    def test_renewal_failure_fails_one_job_but_worker_runs_the_next(self):
        def wait_for_monitor(_payload, _progress, cancel_event):
            cancel_event.wait(3)
            return {"stopped": cancel_event.is_set()}

        interrupted = self.queue.enqueue("wait")
        healthy = self.queue.enqueue("noop", {"echo": "next"})
        worker = Worker(
            self.queue,
            "worker",
            {"wait": wait_for_monitor, "noop": noop_handler},
        )
        original_heartbeat = self.queue.heartbeat
        heartbeat_calls = 0

        def fail_first_heartbeat(*args, **kwargs):
            nonlocal heartbeat_calls
            heartbeat_calls += 1
            if heartbeat_calls == 1:
                raise RuntimeError("simulated renewal failure")
            return original_heartbeat(*args, **kwargs)

        with patch.object(self.queue, "heartbeat", side_effect=fail_first_heartbeat):
            self.assertTrue(worker.run_once())
            self.assertTrue(worker.run_once())

        failed = self.queue.get(interrupted.id)
        self.assertEqual("failed", failed.status)
        self.assertEqual("worker_lease_lost", failed.error_code)
        self.assertEqual("succeeded", self.queue.get(healthy.id).status)


if __name__ == "__main__":
    unittest.main()
