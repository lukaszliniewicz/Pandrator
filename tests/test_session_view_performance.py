"""Session-view performance guards: query budgets, no global caches, row-set equivalence.

These tests pin the profiled session-view bottlenecks without wall-clock
assertions:
- revision_history must not issue per-segment SQL (deferred-column N+1),
  including with performance/casting enabled;
- provider catalogue builds are request-scoped (a second identical read
  rebuilds) and bounded by distinct catalogue inputs, not by segments;
- the segment-driven takes/artifacts split counts exactly the inner-join
  row set, including adversarial take/artifact states;
- repair_state_hash stays deterministic across calls.
"""

import inspect
import tempfile
import unittest
import uuid
import wave
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import event, func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_audio_identity import (
    IDENTITY_KEY,
    AudioIdentityContext,
)
from pandrator.web.generation_history_reads import GenerationHistoryReader
from pandrator.web.generation_review import revision_history
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationRun,
    GenerationSegment,
    Job,
    OutputAssembly,
    utcnow,
)


class SessionViewPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.services = self.app.extensions["pandrator"]
        self.database = self.services["database"]
        self.generation = self.services["generation"]
        self.addCleanup(self.database.dispose)

    def _create_session(self, name="Session-view perf"):
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": f"{name} {uuid.uuid4().hex[:8]}", "workflow_kind": "audiobook"},
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        return created.get_json()["id"]

    def _wave_path(self, label):
        directory = self.services["paths"].uploads / "session-view-perf"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{label}-{uuid.uuid4().hex}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 1600)
        return path

    def _plan(self, session_id, count, *, voices=("v-a", "v-b", None)):
        segments = []
        for index in range(count):
            voice = voices[index % len(voices)]
            segments.append(
                {
                    "text": f"Speech block number {index} with enough words.",
                    "language": "de" if index % 2 else "en",
                    **({"voice": voice} if voice else {}),
                    "speech_block_provenance": {
                        "schema_version": 1,
                        "source_reference_namespace": "subtitle_ordinal",
                        "source_cues": [
                            {
                                "reference": index,
                                "start_ms": index * 1000,
                                "end_ms": index * 1000 + 900,
                                "display_spans": [[0, 10]],
                                "speech_spans": [[0, 10]],
                            }
                        ],
                    },
                }
            )
        return self.generation.create_plan(
            session_id, source_revision_id=None, settings={}, segments=segments
        )

    def _seed_reusable_takes(self, session_id, revision_id, override):
        snapshot, _ = self.services["workspace_settings"].resolve(
            session_id, run_override=override
        )
        with self.database.session() as session:
            context = AudioIdentityContext(session, snapshot)
            segment_ids = list(
                session.scalars(
                    select(GenerationSegment.id)
                    .where(GenerationSegment.plan_revision_id == revision_id)
                    .order_by(GenerationSegment.ordinal)
                )
            )
            expected = {
                segment.id: context.for_segment(segment)
                for segment in session.scalars(
                    select(GenerationSegment).where(
                        GenerationSegment.id.in_(segment_ids)
                    )
                )
            }
        for segment_id in segment_ids:
            artifact = self.services["artifacts"].register(
                self._wave_path(segment_id),
                kind="audio",
                role="generation_take",
                session_id=session_id,
                metadata={IDENTITY_KEY: expected[segment_id]},
            )
            with self.database.session() as session:
                segment = session.get(GenerationSegment, segment_id)
                segment.status = "completed"
                session.add(
                    AudioTake(
                        generation_segment_id=segment_id,
                        artifact_id=artifact.id,
                        status="completed",
                        is_active=True,
                        duration_ms=100,
                    )
                )
        return segment_ids

    def _statement_log(self):
        log = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            log.append(statement)

        event.listen(self.database.engine, "before_cursor_execute", _record)
        self.addCleanup(
            event.remove, self.database.engine, "before_cursor_execute", _record
        )
        return log

    def test_segment_reads_batch_takes_and_artifacts_at_constant_query_budget(self):
        log = self._statement_log()
        counts_by_read = {}
        for count in (1, 10, 50):
            session_id = self._create_session("Segment-read perf")
            plan = self._plan(session_id, count)
            segment_ids = self._seed_reusable_takes(
                session_id, plan["active_revision_id"], {}
            )
            for name, options in (
                ("full", {}),
                ("compact", {"view": "compact"}),
                ("provenance", {"view": "provenance"}),
                ("search", {"q": "Speech", "text_field": "spoken"}),
                ("source-cue", {"source_cue_id": "0", "radius": 2}),
            ):
                with self.subTest(count=count, read=name):
                    log.clear()
                    payload = self.generation.list_segments(
                        session_id, limit=250, **options
                    )
                    selects = [sql for sql in log if sql.lstrip().upper().startswith("SELECT")]
                    self.assertLessEqual(len(selects), 80)
                    counts_by_read.setdefault(name, []).append(len(selects))
                    self.assertEqual(1, sum("FROM audio_takes" in sql for sql in selects))
                    # Full settings resolution also scans the voice inventory;
                    # this guard isolates take-artifact retrieval by primary key.
                    self.assertLessEqual(sum("FROM artifacts" in sql and "artifacts.id IN" in sql for sql in selects), 1)
                    expected_count = min(count, 3) if name == "source-cue" else count
                    self.assertEqual(expected_count, len(payload["items"]))
                    self.assertEqual(segment_ids[:expected_count], [item["id"] for item in payload["items"]])
                    self.assertEqual(plan["active_revision_id"], payload["plan_revision_id"])
                    for item in payload["items"]:
                        self.assertNotIn("plan_revision_id", item)
                        if name == "provenance":
                            self.assertNotIn("takes", item)
                        else:
                            self.assertTrue(item["has_reusable_take"])
                            self.assertEqual("reusable", item["audio_reuse_reason"])
                        if name == "compact":
                            self.assertEqual(1, item["take_count"])
                            self.assertTrue(item["active_take_id"])
                            self.assertTrue(item["has_usable_take"])
                            self.assertNotIn("speech_block_provenance", item)
                        else:
                            cue = item["speech_block_provenance"]["source_cues"][0]
                            self.assertEqual(item["ordinal"] * 1000, cue["start_ms"])
                            self.assertEqual(item["ordinal"] * 1000 + 900, cue["end_ms"])
                            self.assertEqual([[0, 10]], cue["display_spans"])
                            self.assertEqual([[0, 10]], cue["speech_spans"])
                            if name != "provenance":
                                self.assertTrue(item["takes"][0]["is_active"])
                                self.assertEqual(100, item["takes"][0]["duration_ms"])
                        if name == "search":
                            self.assertEqual([{"start": 0, "end": 6}], item["search_matches"])
        for name, counts in counts_by_read.items():
            self.assertEqual(1, len(set(counts)), (name, counts))

    def test_standalone_segment_reader_matches_facade_and_forwards_every_keyword(self):
        from pandrator.web.generation_segment_reads import GenerationSegmentReader

        session_id = self._create_session()
        self._plan(session_id, 2)
        reader = GenerationSegmentReader(self.database, self.generation.settings)
        self.assertEqual(
            inspect.signature(type(self.generation).list_segments),
            inspect.signature(GenerationSegmentReader.list_segments),
        )
        self.assertEqual(reader.list_segments(session_id), self.generation.list_segments(session_id))
        options = {
            "cursor": 3, "limit": 17, "status": "completed", "marked": True,
            "verification": "issues", "generation_run_id": "run", "plan_revision_id": "revision",
            "view": "provenance", "fields": ["text"], "end_ordinal": 9,
            "around_ordinal": 5, "source_cue_id": "cue", "radius": 4,
            "q": "needle", "match_case": True, "whole_word": True,
            "text_field": "spoken", "boundary_flags": False,
        }
        replacement_database, replacement_settings = object(), object()
        with patch.object(self.generation, "database", replacement_database), patch.object(
            self.generation, "settings", replacement_settings
        ), patch("pandrator.web.workspace.GenerationSegmentReader") as reader_class:
            expected = {"items": []}
            reader_class.return_value.list_segments.return_value = expected
            self.assertIs(expected, self.generation.list_segments(session_id, **options))
            reader_class.assert_called_once_with(replacement_database, replacement_settings)
            reader_class.return_value.list_segments.assert_called_once_with(session_id, **options)

    def test_updated_segment_projection_reads_uncommitted_caller_state_without_session(self):
        from pandrator.web.generation_segment_reads import updated_segment_payload

        session_id = self._create_session()
        plan = self._plan(session_id, 1)
        with self.database.session() as session:
            segment = session.scalar(select(GenerationSegment).where(
                GenerationSegment.plan_revision_id == plan["active_revision_id"]
            ))
            segment.text = "Uncommitted caller text"
            segment.marked = True
            session.flush()
            with patch.object(self.database, "session", side_effect=AssertionError("nested session")):
                payload = updated_segment_payload(segment)
                self.assertEqual(payload, self.generation._updated_segment_payload(segment))
            self.assertEqual("Uncommitted caller text", payload["text"])
            self.assertTrue(payload["marked"])
            self.assertEqual(plan["active_revision_id"], payload["plan_revision_id"])

    def test_segment_take_metadata_defaults_do_not_bleed_between_takes(self):
        session_id = self._create_session()
        plan = self._plan(session_id, 1)
        segment_id = self._seed_reusable_takes(session_id, plan["active_revision_id"], {})[0]
        rich = {
            "generation_task_run_id": "task-rich", "source_text": "source-rich",
            "synthesized_text": "spoken-rich", "llm_optimized": "yes",
            "llm_model": "model-rich", "audio_verification": {"status": "warning"},
        }
        with self.database.session() as session:
            active = session.scalar(select(AudioTake).where(AudioTake.generation_segment_id == segment_id))
            artifact = session.get(Artifact, active.artifact_id)
            artifact.metadata_json = {**artifact.metadata_json, **rich}
            empty = Artifact(session_id=session_id, kind="audio", role="generation_take", relative_path=f"empty-{uuid.uuid4().hex}.wav", metadata_json={})
            session.add(empty)
            session.flush()
            active.created_at = datetime(2025, 1, 2, 3, 4, 5)
            session.add_all([
                AudioTake(generation_segment_id=segment_id, artifact_id=artifact.id, created_at=active.created_at - timedelta(seconds=1)),
                AudioTake(generation_segment_id=segment_id, artifact_id=empty.id, created_at=active.created_at - timedelta(seconds=2)),
                AudioTake(generation_segment_id=segment_id, created_at=active.created_at - timedelta(seconds=3)),
            ])
        takes = self.generation.list_segments(session_id)["items"][0]["takes"]
        self.assertEqual(4, len(takes))
        for take in takes[:2]:
            for key, value in rich.items():
                self.assertEqual(True if key == "llm_optimized" else value, take[key])
        for take in takes[2:]:
            for key in rich:
                self.assertEqual(False if key == "llm_optimized" else None, take[key])
        self.assertTrue(takes[0]["is_active"])
        self.assertIsNone(takes[-1]["artifact_id"])
        self.assertEqual([
            "2025-01-02T03:04:05", "2025-01-02T03:04:04",
            "2025-01-02T03:04:03", "2025-01-02T03:04:02",
        ], [take["created_at"] for take in takes])
        for index, take in enumerate(takes):
            self.assertEqual("tts", take["kind"])
            self.assertEqual("completed" if index == 0 else "queued", take["status"])
            self.assertEqual(100 if index == 0 else None, take["duration_ms"])
            self.assertEqual(index == 0, take["is_active"])
            self.assertEqual(1, take["revision"])
            self.assertIsNone(take["generation_run_id"])
            self.assertIsNone(take["parent_take_id"])
            self.assertTrue(take["id"])
        self.assertEqual(takes[0]["artifact_id"], takes[1]["artifact_id"])
        self.assertNotEqual(takes[1]["artifact_id"], takes[2]["artifact_id"])

    def _run_history_fixture(self, count, *, run_status, job_status):
        session_id = self._create_session("Run-history perf")
        plan = self._plan(session_id, 1)
        run_ids = []
        with self.database.session() as session:
            for sequence in range(1, count + 1):
                job = Job(
                    session_id=session_id,
                    kind="generation",
                    status=job_status,
                    payload_json={"segment_ids": [f"segment-{sequence}"]},
                )
                session.add(job)
                session.flush()
                run = GenerationRun(
                    session_id=session_id,
                    plan_revision_id=plan["active_revision_id"],
                    job_id=job.id,
                    sequence_number=sequence,
                    status=run_status,
                    settings_snapshot_json={"tts": {"voice": "history-voice"}},
                )
                session.add(run)
                session.flush()
                run_ids.append(run.id)
        return session_id, run_ids

    @staticmethod
    def _history_selects(log):
        selects = [item for item in log if item.lstrip().upper().startswith("SELECT")]
        blockers = [
            item for item in selects
            if "FROM jobs" in item and "jobs.lease_expires_at >" in item
        ]
        return selects, blockers

    def _history_assembly(self, session, session_id, run_id, job, *, created_at):
        assembly = OutputAssembly(
            session_id=session_id,
            generation_run_id=run_id,
            job_id=job.id if job else None,
            status="running",
            settings_json={"history_run": run_id},
            created_at=created_at,
        )
        session.add(assembly)
        session.flush()
        session.refresh(assembly)
        return self.generation._assembly_payload(assembly, job)

    def _run_history_assembly_fixture(self, count, *, run_status, job_status):
        session_id, run_ids = self._run_history_fixture(
            count, run_status=run_status, job_status=job_status
        )
        expected = {}
        now = utcnow()
        with self.database.session() as session:
            for index, run_id in enumerate(run_ids, start=1):
                old_job = Job(session_id=session_id, kind="output.assembly", status="queued")
                latest_job = Job(
                    session_id=session_id,
                    kind="output.assembly",
                    status="running",
                    progress=0.25,
                    progress_detail=f"assembly-{index}-running",
                )
                session.add_all([old_job, latest_job])
                session.flush()
                self._history_assembly(
                    session, session_id, run_id, old_job,
                    created_at=now - timedelta(seconds=1),
                )
                expected[run_id] = self._history_assembly(
                    session, session_id, run_id, latest_job, created_at=now,
                )
        return session_id, run_ids, expected

    def test_run_history_batches_latest_assembly_jobs_with_constant_queries(self):
        log = self._statement_log()
        for run_status, job_status in (("completed", "succeeded"), ("queued", "queued")):
            for count in (1, 10, 50):
                session_id, run_ids, expected = self._run_history_assembly_fixture(
                    count, run_status=run_status, job_status=job_status
                )
                for read_name, read in (
                    ("full", lambda session_id=session_id: self.generation.list_runs(session_id)),
                    ("limit", lambda session_id=session_id: self.generation.list_runs(session_id, limit=1)),
                    ("latest", lambda session_id=session_id: [self.generation.latest_run(session_id)]),
                ):
                    with self.subTest(run_status=run_status, count=count, read=read_name):
                        log.clear()
                        items = read()
                        selects, blockers = self._history_selects(log)
                        self.assertEqual(count if read_name == "full" else 1, len(items))
                        self.assertEqual(run_ids[-1], items[0]["id"])
                        for item in items:
                            self.assertEqual(expected[item["id"]], item["assembly"])
                            self.assertEqual(0.25, item["assembly"]["progress"])
                            self.assertEqual(
                                f"assembly-{item['sequence_number']}-running",
                                item["assembly"]["progress_detail"],
                            )
                        self.assertEqual(
                            [],
                            [sql for sql in selects if "FROM jobs" in sql and "WHERE jobs.id =" in sql],
                        )
                        self.assertLessEqual(len(selects), 9 if job_status == "queued" else 8)
                        self.assertEqual(1 if job_status == "queued" else 0, len(blockers))

    def test_assembly_job_batch_skips_missing_reused_and_older_jobs(self):
        session_id, run_ids = self._run_history_fixture(
            3, run_status="completed", job_status="succeeded"
        )
        expected = {}
        generation_job_ids = set()
        now = utcnow()
        with self.database.session() as session:
            for run_id in run_ids:
                generation_job_ids.add(session.get(GenerationRun, run_id).job_id)
            reused_job = session.get(Job, session.get(GenerationRun, run_ids[1]).job_id)
            reused_job.progress = 0.6
            reused_job.progress_detail = "reused-generation-job"
            selected_job = Job(
                session_id=session_id, kind="output.assembly", status="queued",
                progress=0.25, progress_detail="selected-queued-assembly-job",
            )
            session.add(selected_job)
            session.flush()
            selected_job_id = selected_job.id
            for run_id, selected in zip(run_ids, (None, reused_job, selected_job), strict=True):
                older_job = Job(
                    session_id=session_id, kind="output.assembly", status="queued",
                    progress_detail="unused-old-assembly-job",
                )
                session.add(older_job)
                session.flush()
                self._history_assembly(
                    session, session_id, run_id, older_job,
                    created_at=now - timedelta(seconds=1),
                )
                expected[run_id] = self._history_assembly(
                    session, session_id, run_id, selected, created_at=now,
                )

        job_batches = []

        def record_job_batch(conn, cursor, statement, parameters, context, executemany):
            if "FROM jobs" in statement and "WHERE jobs.id IN" in statement:
                job_batches.append(set(parameters))

        event.listen(self.database.engine, "before_cursor_execute", record_job_batch)
        self.addCleanup(
            event.remove, self.database.engine, "before_cursor_execute", record_job_batch
        )
        log = self._statement_log()
        items = self.generation.list_runs(session_id)
        selects, blockers = self._history_selects(log)
        self.assertEqual([generation_job_ids, {selected_job_id}], job_batches)
        self.assertEqual([], blockers)
        self.assertLessEqual(len(selects), 8)
        self.assertEqual(
            [], [sql for sql in selects if "FROM jobs" in sql and "WHERE jobs.id =" in sql]
        )
        self.assertEqual(expected, {item["id"]: item["assembly"] for item in items})
        self.assertEqual(0.0, expected[run_ids[0]]["progress"])
        self.assertIsNone(expected[run_ids[0]]["progress_detail"])
        self.assertEqual(0.6, expected[run_ids[1]]["progress"])
        self.assertEqual("reused-generation-job", expected[run_ids[1]]["progress_detail"])
        self.assertEqual(0.25, expected[run_ids[2]]["progress"])
        self.assertEqual("selected-queued-assembly-job", expected[run_ids[2]]["progress_detail"])

    def test_run_history_queries_are_constant_for_completed_and_queued_runs(self):
        log = self._statement_log()
        for run_status, job_status in (("completed", "succeeded"), ("queued", "queued")):
            for count in (1, 10, 50):
                with self.subTest(run_status=run_status, count=count):
                    session_id, run_ids = self._run_history_fixture(
                        count, run_status=run_status, job_status=job_status
                    )
                    log.clear()
                    items = self.generation.list_runs(session_id)
                    selects, blockers = self._history_selects(log)
                    self.assertEqual(count, len(items))
                    self.assertEqual(run_ids[-1], items[0]["id"])
                    self.assertEqual(count, items[0]["sequence_number"])
                    self.assertEqual(
                        {"tts": {"voice": "history-voice"}},
                        items[0]["settings_snapshot"],
                    )
                    self.assertLessEqual(
                        len(selects), 8,
                        f"{len(selects)} SELECTs ({len(blockers)} blockers) for {count} runs",
                    )
                    self.assertEqual(1 if job_status == "queued" else 0, len(blockers))

                    for read in (
                        lambda session_id=session_id: self.generation.list_runs(session_id, limit=1)[0],
                        lambda session_id=session_id: self.generation.latest_run(session_id),
                    ):
                        log.clear()
                        latest = read()
                        selects, blockers = self._history_selects(log)
                        self.assertEqual(run_ids[-1], latest["id"])
                        self.assertEqual(count, latest["sequence_number"])
                        self.assertLessEqual(len(selects), 8)
                        self.assertLessEqual(len(blockers), 1)

    def test_database_only_history_reader_preserves_service_payloads(self):
        session_id, _ = self._run_history_fixture(
            2, run_status="completed", job_status="succeeded"
        )
        reader = GenerationHistoryReader(self.database)
        self.assertEqual(
            self.generation.list_runs(session_id), reader.list_runs(session_id)
        )
        self.assertEqual(
            self.generation.latest_run(session_id), reader.latest_run(session_id)
        )

    def test_history_payload_reads_changes_in_caller_session(self):
        session_id, run_ids = self._run_history_fixture(
            1, run_status="completed", job_status="succeeded"
        )
        reader = GenerationHistoryReader(self.database)
        with self.database.session() as session:
            run = session.get(GenerationRun, run_ids[0])
            run.status = "paused"
            run.settings_snapshot_json = {"tts": {"voice": "uncommitted-voice"}}
            with patch.object(
                self.database, "session", side_effect=AssertionError("nested transaction")
            ):
                projected = reader._run_payload(session, run)
            self.assertEqual(session_id, projected["session_id"])
            self.assertEqual("paused", projected["status"])
            self.assertEqual(
                {"tts": {"voice": "uncommitted-voice"}}, projected["settings_snapshot"]
            )

    def test_queued_run_selects_earliest_live_same_session_blocker(self):
        session_id, run_ids = self._run_history_fixture(
            1, run_status="queued", job_status="queued"
        )
        other_session_id = self._create_session("Other blocker session")
        now = utcnow()
        with self.database.session() as session:
            candidates = []
            for index, (owner, status, lease) in enumerate((
                (other_session_id, "running", now + timedelta(hours=1)),
                (session_id, "running", now - timedelta(seconds=1)),
                (session_id, "running", None),
                (session_id, "succeeded", now + timedelta(hours=1)),
                (session_id, "running", now + timedelta(hours=1)),
                (session_id, "running", now + timedelta(hours=1)),
                (session_id, "running", now + timedelta(hours=1)),
            )):
                candidate = Job(
                    session_id=owner,
                    kind=f"blocker-{index}",
                    status=status,
                    lease_expires_at=lease,
                    created_at=now + timedelta(seconds=index),
                    progress_detail=f"detail-{index}",
                )
                session.add(candidate)
                session.flush()
                candidates.append(candidate.id)
        log = self._statement_log()
        item = self.generation.list_runs(session_id)[0]
        self.assertEqual(
            {"id": candidates[4], "kind": "blocker-4", "progress_detail": "detail-4"},
            item["waiting_for_job"],
        )
        self.assertEqual(["segment-1"], item["queued_segment_ids"])
        self.assertEqual(1, len(self._history_selects(log)[1]))
        with self.database.session() as session:
            context = self.generation._run_history_context(session, session_id)
            self.assertEqual(
                candidates[4:6], [job.id for job in context["blocking_jobs"]]
            )
            run = context["runs_by_id"][run_ids[0]]
            # Simulate a queued Job becoming a running candidate between the
            # batch job read and blocker query: identity still excludes itself.
            own_job = context["jobs_by_id"][run.job_id]
            context["blocking_jobs"] = [own_job, context["blocking_jobs"][0]]
            log.clear()
            projected = self.generation._run_payload(session, run, _context=context)
            self.assertEqual(item["waiting_for_job"], projected["waiting_for_job"])
            self.assertEqual([], self._history_selects(log)[0])
            context["blocking_jobs"].reverse()
            projected = self.generation._run_payload(session, run, _context=context)
            self.assertEqual(item["waiting_for_job"], projected["waiting_for_job"])
            self.assertEqual([], self._history_selects(log)[0])

    def test_running_job_does_not_wait_for_itself_or_query_blockers(self):
        session_id, run_ids = self._run_history_fixture(
            1, run_status="running", job_status="running"
        )
        with self.database.session() as session:
            run = session.get(GenerationRun, run_ids[0])
            job = session.get(Job, run.job_id)
            job.lease_expires_at = utcnow() + timedelta(hours=1)
        log = self._statement_log()
        item = self.generation.list_runs(session_id)[0]
        self.assertIsNone(item["waiting_for_job"])
        self.assertEqual([], item["queued_segment_ids"])
        self.assertEqual([], self._history_selects(log)[1])
        with self.database.session() as session:
            context = self.generation._run_history_context(session, session_id)
            self.assertEqual([], context["blocking_jobs"])

    @staticmethod
    def _override(**tts):
        return {
            "tts": {
                "service": "openai",
                "model": "model-a",
                "voice": "voice-a",
                "language": "en",
                "tts_batch_size": 1,
                **tts,
            }
        }

    def test_revision_history_has_no_per_segment_queries_with_casting(self):
        from pandrator.web.generation_controls import save_generation_controls

        session_id = self._create_session()
        first = self._plan(session_id, 40)
        with self.database.session() as session:
            save_generation_controls(
                session,
                session_id,
                expected_revision=0,
                characters=[],
                cast={"narrator": {"voice": "voice-a"}},
            )
        self._seed_reusable_takes(session_id, first["active_revision_id"], self._override())
        current = self.client.get(
            f"/api/v1/sessions/{session_id}/settings/tts", headers=self.headers
        ).get_json()
        changed = self.client.put(
            f"/api/v1/sessions/{session_id}/settings/tts",
            json={"value": {**current["effective"], "casting_enabled": True}},
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(200, changed.status_code, changed.get_json())

        log = self._statement_log()
        result = revision_history(self.database, session_id, limit=10)
        segment_selects = [item for item in log if "FROM generation_segments" in item]
        take_selects = [item for item in log if "FROM audio_takes" in item]
        artifact_selects = [item for item in log if "FROM artifacts" in item]
        # Statement budget scales with visible revisions (per-revision freeze
        # reads), never with segments: 40 segments must not add ~40 queries.
        revisions = len(result["items"])
        self.assertGreater(revisions, 0)
        self.assertLessEqual(
            len(segment_selects),
            6 + 3 * revisions,
            f"{len(segment_selects)} generation_segments SELECTs for "
            f"{revisions} revisions in {len(log)} statements",
        )
        self.assertLessEqual(len(take_selects), 4)
        # Settings resolution issues one artifact lookup per role (~14);
        # the identity loop itself adds a single IN query.
        self.assertLessEqual(len(artifact_selects), 20)

    def test_catalogue_builds_are_request_scoped_and_bounded(self):
        from pandrator.logic import tts_handler

        session_id = self._create_session()
        plan = self._plan(session_id, 60)
        self._seed_reusable_takes(session_id, plan["active_revision_id"], self._override())
        with patch.object(
            tts_handler, "_default_service_configs", wraps=tts_handler._default_service_configs
        ) as builds:
            revision_history(self.database, session_id, limit=10)
            first_call = builds.call_count
            revision_history(self.database, session_id, limit=10)
            second_call = builds.call_count - first_call
        # Bounded by distinct catalogue inputs (3 voice combos share one
        # snapshot catalogue), not by the 60 segments.
        self.assertLessEqual(first_call, 3, f"catalogue rebuilt {first_call}x")
        # A second identical read rebuilds again: no cross-request global.
        self.assertGreaterEqual(second_call, 1)

    def test_identity_split_matches_join_row_set_on_adversarial_takes(self):
        session_id = self._create_session()
        plan = self._plan(session_id, 4, voices=("v-a",))
        revision_id = plan["active_revision_id"]
        segment_ids = self._seed_reusable_takes(
            session_id, revision_id, self._override()
        )
        # revision_history inspects current session settings, so persist the
        # override the takes were seeded with before counting reuse.
        current = self.client.get(
            f"/api/v1/sessions/{session_id}/settings/tts", headers=self.headers
        ).get_json()
        changed = self.client.put(
            f"/api/v1/sessions/{session_id}/settings/tts",
            json={"value": {**current["effective"], **self._override()["tts"]}},
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(200, changed.status_code, changed.get_json())
        with self.database.session() as session:
            # Inactive duplicate take: inner join row set ignores it.
            session.add(
                AudioTake(
                    generation_segment_id=segment_ids[1],
                    artifact_id=None,
                    status="completed",
                    is_active=False,
                    duration_ms=50,
                )
            )
            # Take whose artifact is deleted: excluded like the join filter.
            doomed = self.services["artifacts"].register(
                self._wave_path("doomed"),
                kind="audio",
                role="generation_take",
                session_id=session_id,
            )
            session.add(
                AudioTake(
                    generation_segment_id=segment_ids[2],
                    artifact_id=doomed.id,
                    status="completed",
                    is_active=True,
                    duration_ms=100,
                )
            )
            session.flush()
            session.get(Artifact, doomed.id).state = "deleted"
            # Removed segment with a completed take: totals keep it, the
            # identity loop skips it exactly like the removed filter.
            removed = session.get(GenerationSegment, segment_ids[3])
            removed.removed = True
        result = revision_history(self.database, session_id, limit=10)
        item = next(row for row in result["items"] if row["id"] == revision_id)
        self.assertEqual(4, item["segment_count"])
        self.assertEqual(3, item["active_segment_count"])
        self.assertEqual(3, item["reusable_segment_count"])
        self.assertEqual(0, item["stale_segment_count"])
        self.assertEqual(0, item["audio_identity_unknown_segment_count"])

    def test_generation_progress_counts_use_revision_removed_status_covering_index(self):
        session_id = self._create_session()
        plan = self._plan(session_id, 1)
        revision_id = plan["active_revision_id"]
        other_session_id = self._create_session("Other progress plan")
        other_plan = self._plan(other_session_id, 1)

        with self.database.session() as session:
            current_segment = session.scalar(
                select(GenerationSegment).where(
                    GenerationSegment.plan_revision_id == revision_id
                )
            )
            current_segment.status = "ready"
            session.add_all(
                [
                    GenerationSegment(
                        plan_revision_id=revision_id,
                        ordinal=2,
                        text="Included completed segment",
                        status="completed",
                    ),
                    GenerationSegment(
                        plan_revision_id=revision_id,
                        ordinal=3,
                        text="Removed completed segment",
                        status="completed",
                        removed=True,
                    ),
                    GenerationSegment(
                        plan_revision_id=revision_id,
                        ordinal=4,
                        text="Removed ready segment",
                        status="ready",
                        removed=True,
                    ),
                ]
            )
            other_segment = session.scalar(
                select(GenerationSegment).where(
                    GenerationSegment.plan_revision_id
                    == other_plan["active_revision_id"]
                )
            )
            other_segment.status = "completed"

        with self.database.session() as session:
            total = session.scalar(
                select(func.count())
                .select_from(GenerationSegment)
                .where(
                    GenerationSegment.plan_revision_id == revision_id,
                    GenerationSegment.removed.is_(False),
                )
            )
            completed = session.scalar(
                select(func.count())
                .select_from(GenerationSegment)
                .where(
                    GenerationSegment.plan_revision_id == revision_id,
                    GenerationSegment.removed.is_(False),
                    GenerationSegment.status == "completed",
                )
            )
            ready = session.scalar(
                select(func.count())
                .select_from(GenerationSegment)
                .where(
                    GenerationSegment.plan_revision_id == revision_id,
                    GenerationSegment.removed.is_(False),
                    GenerationSegment.status == "ready",
                )
            )
        self.assertEqual(2, total)
        self.assertEqual(1, completed)
        self.assertEqual(1, ready)

        with self.database.engine.connect() as connection:
            self.assertIsNone(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'sqlite_stat1'"
                ).scalar_one_or_none()
            )
            query_plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT count(*) FROM generation_segments "
                "WHERE plan_revision_id = ? AND removed IS 0 AND status = ?",
                (revision_id, "completed"),
            ).all()
        details = [str(row[3]).upper() for row in query_plan]
        self.assertTrue(
            any(
                "USING COVERING INDEX IX_GENERATION_SEGMENTS_REVISION_REMOVED_STATUS"
                in detail
                for detail in details
            ),
            details,
        )

    def test_repair_state_hash_is_deterministic(self):
        from pandrator.web import repair_batches as batches

        session_id = self._create_session()
        plan = self._plan(session_id, 6)
        self._seed_reusable_takes(session_id, plan["active_revision_id"], self._override())
        with self.database.session() as session:
            first = batches.repair_state_hash(session, plan["active_revision_id"])
        with self.database.session() as session:
            second = batches.repair_state_hash(session, plan["active_revision_id"])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
