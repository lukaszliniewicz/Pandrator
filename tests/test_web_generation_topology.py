import tempfile
import threading
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    GenerationSegmentRevision,
    Job,
    OutputAssembly,
)
from pandrator.web.workspace import GenerationService, RevisionConflict, stable_hash


class GenerationTopologyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
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
        self.database = self.app.extensions["pandrator"]["database"]
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Topology review", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        self.session_id = created.get_json()["id"]
        plan = self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-plan",
            json={
                "settings": {"speech_block_max_chars": 100},
                "segments": [
                    {
                        "text": "A🙂 B",
                        "source_segment_ids": ["source-uuid-1"],
                        "alignment_group": "a0001",
                        "speaker": "Narrator",
                    },
                    {
                        "text": "A second block.",
                        "source_segment_ids": ["source-uuid-2"],
                        "alignment_group": "a0002",
                        "paragraph_break_after": True,
                    },
                ],
            },
            headers=self.headers,
        )
        self.assertEqual(201, plan.status_code, plan.get_json())
        self.initial_revision_id = plan.get_json()["active_revision_id"]
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == self.initial_revision_id
                    )
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            self.initial_segment_ids = [segment.id for segment in segments]
            segments[0].marked = True
            segments[0].status = "completed"
            segments[0].speech_block_provenance_json = {
                "schema_version": 1,
                "origin": "automatic",
                "source_reference_namespace": "document_segment_id",
                "source_cues": [
                    {
                        "reference": "source-uuid-1",
                        "start_ms": 100,
                        "end_ms": 900,
                        "display_text": "A🙂 B",
                        "speech_text": "A🙂 B",
                        "display_spans": [[0, 4]],
                        "speech_spans": [[0, 4]],
                    }
                ],
                "formation_events": [],
                "boundary_before": {
                    "action": "keep_boundary",
                    "reason_code": "document_start",
                    "summary": "Speech block starts the document.",
                    "measurements": {},
                    "source_references": ["source-uuid-1"],
                },
                "risk_flags": [],
            }
            segments[1].status = "completed"
            first_artifact = Artifact(
                session_id=self.session_id,
                kind="audio",
                role="generation_take",
                relative_path="sessions/topology/first.wav",
            )
            second_artifact = Artifact(
                session_id=self.session_id,
                kind="audio",
                role="generation_take",
                relative_path="sessions/topology/second.wav",
            )
            session.add_all([first_artifact, second_artifact])
            session.flush()
            first_take = AudioTake(
                generation_segment_id=segments[0].id,
                artifact_id=first_artifact.id,
                status="completed",
                is_active=True,
            )
            second_take = AudioTake(
                generation_segment_id=segments[1].id,
                artifact_id=second_artifact.id,
                status="completed",
                is_active=True,
            )
            stale_take = AudioTake(
                generation_segment_id=segments[1].id,
                artifact_id=second_artifact.id,
                status="stale",
                is_active=False,
            )
            session.add_all([first_take, second_take, stale_take])
            session.flush()
            self.initial_take_ids = [first_take.id, second_take.id]
            self.stale_take_id = stale_take.id
            run = GenerationRun(
                session_id=self.session_id,
                plan_revision_id=self.initial_revision_id,
                sequence_number=1,
                status="completed",
            )
            session.add(run)
            session.flush()
            self.run_id = run.id
            assembly = OutputAssembly(
                session_id=self.session_id,
                status="completed",
            )
            historical_assembly = OutputAssembly(
                session_id=self.session_id,
                generation_run_id=run.id,
                status="completed",
            )
            queued_job = Job(
                kind="generation.assemble",
                session_id=self.session_id,
                status="queued",
            )
            running_job = Job(
                kind="generation.assemble",
                session_id=self.session_id,
                status="running",
                lease_owner="topology-test-worker",
                lease_generation=1,
            )
            session.add_all([assembly, historical_assembly, queued_job, running_job])
            session.flush()
            queued_assembly = OutputAssembly(
                session_id=self.session_id,
                job_id=queued_job.id,
                status="queued",
                settings_json={"plan_revision_id": self.initial_revision_id},
            )
            running_assembly = OutputAssembly(
                session_id=self.session_id,
                job_id=running_job.id,
                status="running",
                settings_json={"plan_revision_id": self.initial_revision_id},
            )
            session.add_all([queued_assembly, running_assembly])
            session.flush()
            self.assembly_id = assembly.id
            self.historical_assembly_id = historical_assembly.id
            self.queued_job_id = queued_job.id
            self.running_job_id = running_job.id
            self.queued_assembly_id = queued_assembly.id
            self.running_assembly_id = running_assembly.id

    def test_large_inactive_repair_batches_unchanged_audio_copies(self):
        import time

        from sqlalchemy import event

        with self.database.session() as session:
            artifact_id = session.get(AudioTake, self.initial_take_ids[1]).artifact_id
            extras = [GenerationSegment(
                plan_revision_id=self.initial_revision_id, ordinal=i,
                text=f"A complete unchanged sentence number {i}.",
                source_segment_ids_json=[f"source-{i}"], status="completed",
            ) for i in range(2, 542)]
            session.add_all(extras)
            session.flush()
            session.add_all([AudioTake(
                generation_segment_id=segment.id, artifact_id=artifact_id,
                status="completed", is_active=True,
            ) for segment in extras])
        reads = []
        def inspect_sql(_connection, _cursor, statement, _params, _context, _many):
            if statement.lstrip().upper().startswith("SELECT") and "FROM audio_takes" in statement:
                reads.append(statement)
        event.listen(self.database.engine, "before_cursor_execute", inspect_sql)
        started = time.monotonic()
        try:
            service = self.app.extensions["pandrator"]["generation"]
            with self.database.immediate_session() as session:
                result = service.revise_topology_in_session(
                    session, self.session_id, self.initial_revision_id,
                    {"action": "split", "segment_id": self.initial_segment_ids[0],
                     "cursor": 2, "text_layer": "display"}, activate=False,
                )
        finally:
            event.remove(self.database.engine, "before_cursor_execute", inspect_sql)
        print(f"542-block topology staging: {time.monotonic() - started:.3f}s; {len(reads)} take reads")
        self.assertLessEqual(len(reads), 2)
        self.assertEqual(543, len(result["segment_ids"]))
        with self.database.session() as session:
            active = session.scalar(select(GenerationPlan.active_revision_id).where(GenerationPlan.session_id == self.session_id))
            clones = list(session.scalars(select(AudioTake).where(AudioTake.generation_segment_id.in_(result["segment_ids"]))))
            self.assertEqual(self.initial_revision_id, active)
            self.assertEqual(542, len(clones))
            self.assertTrue(all(take.parent_take_id for take in clones))
            self.assertEqual(541, sum(take.is_active for take in clones))
            self.assertEqual(1, sum(take.status == "stale" for take in clones))
            self.assertTrue(all(take.generation_segment_id not in result["affected_segment_ids"] for take in clones))
            self.assertEqual(542, len({take.parent_take_id for take in clones}))

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _topology(self, revision_id, operation, key):
        return self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-plan/topology",
            json={"expected_revision_id": revision_id, **operation},
            headers={
                **self.headers,
                "If-Match": f'"{revision_id}"',
                "Idempotency-Key": key,
            },
        )

    def _segments(self, **query):
        return self.client.get(
            f"/api/v1/sessions/{self.session_id}/generation-segments",
            query_string=query,
        ).get_json()

    def _enable_markup_preview(self):
        from pandrator.web.generation_controls import (
            get_generation_controls,
            save_generation_controls,
        )

        with self.database.session() as session:
            controls = get_generation_controls(session, self.session_id)
            save_generation_controls(
                session,
                self.session_id,
                expected_revision=controls["revision"],
                characters=[
                    {
                        "id": "c-alice",
                        "display_name": "Alice",
                        "voice_category": "female",
                    }
                ],
                cast={
                    "narrator": {"voice": "Kore"},
                    "characters": {"c-alice": {"voice": "Puck"}},
                },
            )
        settings = self.app.extensions["pandrator"]["workspace_settings"]
        tts = settings.get(self.session_id, "tts")
        settings.update(
            self.session_id,
            "tts",
            tts["revision"],
            {
                **tts["effective"],
                "service": "gemini",
                "model": "gemini-2.5-flash-tts",
                "casting_enabled": True,
                "performance_enabled": False,
            },
        )

    def _put_markup(self, segment_id, xml):
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            segment.speech_plan_json = {"speech_xml": xml}

    @staticmethod
    def _rich_markup(segment_id, text):
        return (
            f'<segment id="{segment_id}" boundary_after="scene">'
            '<dialogue><speaker ref="c-alice">'
            f"<ins>Speak softly.</ins>{text}</speaker></dialogue>"
            '<event kind="pause" duration_ms="120"/>'
            "</segment>"
        )

    def _edit_rollback_state(self, segment_ids):
        from sqlalchemy import func

        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.id.in_(segment_ids))
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            active_revision_id = session.scalar(
                select(GenerationPlan.active_revision_id).where(
                    GenerationPlan.session_id == self.session_id
                )
            )
            segment_history = tuple(
                (
                    item.generation_segment_id,
                    item.revision,
                    item.ordinal,
                    item.text,
                    item.optimized_text,
                    item.speech_plan_json,
                )
                for item in session.scalars(
                    select(GenerationSegmentRevision)
                    .where(
                        GenerationSegmentRevision.generation_segment_id.in_(segment_ids)
                    )
                    .order_by(
                        GenerationSegmentRevision.generation_segment_id,
                        GenerationSegmentRevision.revision,
                    )
                ).all()
            )
            return (
                active_revision_id,
                session.scalar(select(func.count(GenerationPlanRevision.id))),
                session.scalar(select(func.count(GenerationSegment.id))),
                session.scalar(select(func.count(GenerationSegmentRevision.id))),
                session.scalar(select(func.count(AudioTake.id))),
                tuple(
                    (
                        row.id,
                        row.text,
                        row.optimized_text,
                        row.speech_plan_json,
                        row.revision,
                    )
                    for row in rows
                ),
                segment_history,
            )

    def _parse_markup(self, segment):
        from pandrator.logic.speech_markup import parse_speech_markup
        from pandrator.web.generation_controls import get_generation_controls

        with self.database.session() as session:
            current = session.get(GenerationSegment, segment["id"])
            controls = get_generation_controls(session, self.session_id)
            return parse_speech_markup(
                current.speech_plan_json["speech_xml"],
                expected_segment_id=current.id,
                expected_text=current.optimized_text or current.text,
                characters=controls["characters"],
            )

    def _assert_preview(self, revision_id, segment):
        from pandrator.web.speech_plan_preview import preview_speech_segment

        preview = preview_speech_segment(
            self.app.extensions["pandrator"],
            self.session_id,
            revision_id=revision_id,
            segment_id=segment["id"],
        )
        self.assertEqual(segment["optimized_text"] or segment["text"], preview["text"])
        return preview

    def test_topology_requires_idempotency_key_for_browser_calls(self):
        response = self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-plan/topology",
            json={
                "expected_revision_id": self.initial_revision_id,
                "action": "split",
                "segment_id": self.initial_segment_ids[0],
                "cursor": 2,
                "text_layer": "display",
            },
            headers={
                **self.headers,
                "If-Match": f'"{self.initial_revision_id}"',
            },
        )

        self.assertEqual(400, response.status_code, response.get_json())
        self.assertEqual(
            "idempotency_key_required", response.get_json()["error"]["code"]
        )

    def _assert_standalone_mutation_serializes_revision_guard(self, mutation):
        services = self.app.extensions["pandrator"]
        generation = services["generation"]
        record = services["sessions"].create(
            f"Concurrent {mutation}", workflow_kind="audiobook"
        )
        segment_count = 2 if mutation == "update_segments" else 1
        plan = generation.create_plan(
            record.id,
            source_revision_id=None,
            segments=[{"text": f"Original {index}."} for index in range(segment_count)],
        )
        with self.database.session() as session:
            segments = list(session.scalars(
                select(GenerationSegment)
                .where(GenerationSegment.plan_revision_id == plan["active_revision_id"])
                .order_by(GenerationSegment.ordinal)
            ))
            segment_ids = [segment.id for segment in segments]
            self.assertEqual([1] * segment_count, [segment.revision for segment in segments])
        wav_path = services["paths"].uploads / f"{mutation}.wav"
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 1600)
        artifact = services["artifacts"].register(
            wav_path, kind="audio", role="generation_take", session_id=record.id
        )
        take_ids = {}
        with self.database.session() as session:
            for segment_id in segment_ids:
                takes = [AudioTake(
                    generation_segment_id=segment_id, artifact_id=artifact.id,
                    status="completed", is_active=(index == 0),
                ) for index in range(2)]
                session.add_all(takes)
                session.flush()
                take_ids[segment_id] = [take.id for take in takes]

        started = [threading.Event(), threading.Event()]
        reached = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        local = threading.local()
        outcomes = {}
        observed_revisions = {}

        def pause_once(segment):
            index = local.index
            if reached[index].is_set():
                return
            observed_revisions[index] = segment.revision
            reached[index].set()
            if not release[index].wait(5):
                raise TimeoutError(f"Writer {index + 1} exceeded the post-guard pause")

        original_apply = generation._apply_segment_changes

        def paused_apply(session, segment, changes, **kwargs):
            pause_once(segment)
            return original_apply(session, segment, changes, **kwargs)

        def before_attach(session, instance):
            if (
                isinstance(instance, GenerationSegmentRevision)
                and instance.generation_segment_id in segment_ids
                and hasattr(local, "index")
            ):
                pause_once(session.get(GenerationSegment, instance.generation_segment_id))

        def writer(index):
            local.index = index
            try:
                started[index].set()
                if mutation == "update_segment":
                    result = generation.update_segment(
                        segment_ids[0], 1, {"text": f"Writer {index + 1} segment 0."}
                    )
                elif mutation == "update_segments":
                    result = generation.update_segments(record.id, [{
                        "id": segment_id, "revision": 1,
                        "changes": {"text": f"Writer {index + 1} segment {ordinal}."},
                    } for ordinal, segment_id in enumerate(segment_ids)])
                else:
                    result = generation.select_take(
                        segment_ids[0], take_ids[segment_ids[0]][1 - index], 1
                    )
                outcomes[index] = result
            except Exception as error:
                outcomes[index] = error

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(2)]
        started_threads = []
        apply_patch = patch.object(generation, "_apply_segment_changes", side_effect=paused_apply)
        if mutation == "select_take":
            event.listen(Session, "before_attach", before_attach)
        else:
            apply_patch.start()
        try:
            threads[0].start()
            started_threads.append(threads[0])
            self.assertTrue(reached[0].wait(5), "First writer did not reach its revision guard")
            threads[1].start()
            started_threads.append(threads[1])
            self.assertTrue(started[1].wait(5), "Second writer did not start")
            # The old deferred transaction lets writer 2 pass its stale guard.
            # A serialized writer waits here, then rejects after writer 1 commits.
            reached[1].wait(1)
            release[0].set()
            threads[0].join(5)
            self.assertFalse(threads[0].is_alive(), "First writer did not finish")
            release[1].set()
            threads[1].join(5)
            self.assertFalse(threads[1].is_alive(), "Second writer did not finish")
        finally:
            for signal in release:
                signal.set()
            for thread in started_threads:
                thread.join(5)
            if mutation == "select_take":
                event.remove(Session, "before_attach", before_attach)
            else:
                apply_patch.stop()
            self.assertFalse(any(thread.is_alive() for thread in started_threads),
                             "A mutation thread survived cleanup")

        self.assertEqual(1, observed_revisions[0])
        self.assertIsInstance(outcomes[0], dict, outcomes)
        self.assertIsInstance(outcomes[1], RevisionConflict, outcomes)
        first_items = outcomes[0]["items"] if mutation == "update_segments" else [outcomes[0]]
        self.assertEqual([2] * segment_count, [item["revision"] for item in first_items])
        with self.database.session() as session:
            for ordinal, segment_id in enumerate(segment_ids):
                segment = session.get(GenerationSegment, segment_id)
                self.assertEqual(2, segment.revision)
                self.assertEqual(
                    f"Original {ordinal}." if mutation == "select_take"
                    else f"Writer 1 segment {ordinal}.", segment.text,
                )
                history = list(session.scalars(select(GenerationSegmentRevision).where(
                    GenerationSegmentRevision.generation_segment_id == segment_id
                )))
                self.assertEqual([1], [row.revision for row in history])
                selected = list(session.scalars(select(AudioTake).where(
                    AudioTake.generation_segment_id == segment_id,
                    AudioTake.is_active.is_(True),
                )))
                expected_take = take_ids[segment_id][1 if mutation == "select_take" else 0]
                self.assertEqual([expected_take], [take.id for take in selected])

    def test_standalone_segment_update_serializes_revision_guard(self):
        self._assert_standalone_mutation_serializes_revision_guard("update_segment")

    def test_standalone_batch_update_serializes_revision_guard(self):
        self._assert_standalone_mutation_serializes_revision_guard("update_segments")

    def test_standalone_take_selection_serializes_revision_guard(self):
        self._assert_standalone_mutation_serializes_revision_guard("select_take")

    def _concurrent_plan_creates(self, session_id):
        generation = self.app.extensions["pandrator"]["generation"]
        barrier = threading.Barrier(2)

        def create(index):
            barrier.wait(timeout=30)
            return generation.create_plan(
                session_id,
                source_revision_id=None,
                segments=[{"text": f"Concurrent plan {index}."}],
                settings={"caller": index},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(create, range(2)))

    def test_concurrent_initial_plan_creates_serialize_plan_allocation(self):
        session = self.app.extensions["pandrator"]["sessions"].create(
            "Concurrent initial plan",
            workflow_kind="voiceover",
        )

        results = self._concurrent_plan_creates(session.id)

        self.assertEqual(2, len({result["active_revision_id"] for result in results}))
        with self.database.session() as database_session:
            plan_rows = list(
                database_session.scalars(
                    select(GenerationPlan).where(GenerationPlan.session_id == session.id)
                ).all()
            )
            self.assertEqual(1, len(plan_rows))
            revisions = list(
                database_session.scalars(
                    select(GenerationPlanRevision)
                    .where(GenerationPlanRevision.plan_id == plan_rows[0].id)
                    .order_by(GenerationPlanRevision.revision_number)
                ).all()
            )
            self.assertEqual([1, 2], [revision.revision_number for revision in revisions])
            self.assertEqual(plan_rows[0].active_revision_id, revisions[-1].id)

    def test_concurrent_revision_creates_serialize_existing_plan_allocation(self):
        results = self._concurrent_plan_creates(self.session_id)

        self.assertEqual(2, len({result["active_revision_id"] for result in results}))
        with self.database.session() as database_session:
            plan = database_session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == self.session_id)
            )
            self.assertIsNotNone(plan)
            revisions = list(
                database_session.scalars(
                    select(GenerationPlanRevision)
                    .where(GenerationPlanRevision.plan_id == plan.id)
                    .order_by(GenerationPlanRevision.revision_number)
                ).all()
            )
            self.assertEqual([1, 2, 3], [revision.revision_number for revision in revisions])
            self.assertEqual(plan.active_revision_id, revisions[-1].id)

    def test_split_merge_restore_preserve_immutable_lineage(self):
        split = self._topology(
            self.initial_revision_id,
            {
                "action": "split",
                "segment_id": self.initial_segment_ids[0],
                "cursor": 2,
                "text_layer": "display",
            },
            "topology-split-1",
        )
        self.assertEqual(201, split.status_code, split.get_json())
        split_result = split.get_json()
        split_revision_id = split_result["plan_revision_id"]
        self.assertEqual(self.initial_revision_id, split_result["parent_revision_id"])

        replay = self._topology(
            self.initial_revision_id,
            {
                "action": "split",
                "segment_id": self.initial_segment_ids[0],
                "cursor": 2,
                "text_layer": "display",
            },
            "topology-split-1",
        )
        self.assertEqual(201, replay.status_code, replay.get_json())
        self.assertEqual(split_result, replay.get_json())
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])

        stale = self._topology(
            self.initial_revision_id,
            {
                "action": "split",
                "segment_id": self.initial_segment_ids[0],
                "cursor": 1,
                "text_layer": "display",
            },
            "topology-stale-1",
        )
        self.assertEqual(409, stale.status_code, stale.get_json())

        split_page = self._segments()
        self.assertEqual(split_revision_id, split_page["plan_revision_id"])
        self.assertEqual(self.initial_revision_id, split_page["parent_revision_id"])
        self.assertEqual("split", split_page["operation_json"]["action"])
        self.assertEqual(
            ["A🙂", "B", "A second block."],
            [item["text"] for item in split_page["items"]],
        )
        left, right, unchanged = split_page["items"]
        for child in (left, right):
            self.assertEqual(["source-uuid-1"], child["source_segment_ids"])
            self.assertEqual(
                "document_segment_id",
                child["speech_block_provenance"]["source_reference_namespace"],
            )
            self.assertEqual(
                "source-uuid-1",
                child["speech_block_provenance"]["source_cues"][0]["reference"],
            )
        self.assertEqual(
            [[0, 2]],
            left["speech_block_provenance"]["source_cues"][0]["display_spans"],
        )
        self.assertEqual(
            [[0, 1]],
            right["speech_block_provenance"]["source_cues"][0]["display_spans"],
        )
        self.assertEqual(
            "manual_split",
            right["speech_block_provenance"]["boundary_before"]["reason_code"],
        )
        self.assertEqual(2, len(unchanged["takes"]))
        selected = next(take for take in unchanged["takes"] if take["is_active"])
        self.assertEqual(self.initial_take_ids[1], selected["parent_take_id"])
        self.assertTrue(any(take["parent_take_id"] == self.stale_take_id
                            and take["status"] == "stale"
                            for take in unchanged["takes"]))

        historical = self._segments(generation_run_id=self.run_id)
        self.assertEqual(self.initial_revision_id, historical["plan_revision_id"])
        self.assertEqual(
            ["A🙂 B", "A second block."], [item["text"] for item in historical["items"]]
        )
        with self.database.session() as session:
            self.assertEqual(
                "stale", session.get(OutputAssembly, self.assembly_id).status
            )
            self.assertEqual(
                self.initial_revision_id,
                session.get(GenerationRun, self.run_id).plan_revision_id,
            )
            self.assertEqual(
                "completed",
                session.get(OutputAssembly, self.historical_assembly_id).status,
            )
            self.assertEqual(
                "canceled", session.get(OutputAssembly, self.queued_assembly_id).status
            )
            self.assertEqual("canceled", session.get(Job, self.queued_job_id).status)
            self.assertEqual(
                "cancel_requested",
                session.get(OutputAssembly, self.running_assembly_id).status,
            )
            self.assertEqual(
                "cancel_requested", session.get(Job, self.running_job_id).status
            )
            split_revision = session.get(GenerationPlanRevision, split_revision_id)
            stored_segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == split_revision_id)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            self.assertEqual(
                stable_hash(
                    {
                        "parent_revision_id": split_revision.parent_revision_id,
                        "operation": split_revision.operation_json,
                        "segments": [
                            GenerationService._segment_copy_values(segment)
                            for segment in stored_segments
                        ],
                    }
                ),
                split_revision.content_hash,
            )

        merged = self._topology(
            split_revision_id,
            {
                "action": "merge",
                "left_segment_id": left["id"],
                "right_segment_id": right["id"],
            },
            "topology-merge-1",
        )
        self.assertEqual(201, merged.status_code, merged.get_json())
        merged_revision_id = merged.get_json()["plan_revision_id"]
        merged_page = self._segments()
        self.assertEqual(
            ["A🙂 B", "A second block."],
            [item["text"] for item in merged_page["items"]],
        )
        merged_first = merged_page["items"][0]
        self.assertEqual("Narrator", merged_first["speaker"])
        self.assertTrue(merged_first["marked"])
        self.assertEqual(["source-uuid-1"], merged_first["source_segment_ids"])

        restored = self._topology(
            merged_revision_id,
            {
                "action": "restore",
                "target_revision_id": self.initial_revision_id,
            },
            "topology-restore-1",
        )
        self.assertEqual(201, restored.status_code, restored.get_json())
        restored_page = self._segments()
        self.assertEqual("restore", restored_page["operation_json"]["action"])
        self.assertEqual(merged_revision_id, restored_page["parent_revision_id"])
        self.assertEqual(
            ["a0001", "a0002"],
            [item["alignment_group"] for item in restored_page["items"]],
        )
        self.assertEqual(
            self.initial_take_ids,
            [next(take for take in item["takes"] if take["is_active"])["parent_take_id"]
             for item in restored_page["items"]],
        )
        self.assertEqual(
            [1, 2], [len(item["takes"]) for item in restored_page["items"]]
        )
        with self.database.session() as session:
            revisions = list(
                session.scalars(
                    select(GenerationPlanRevision).order_by(
                        GenerationPlanRevision.revision_number
                    )
                ).all()
            )
            self.assertEqual([1, 2, 3, 4], [item.revision_number for item in revisions])
            self.assertEqual(4, len({item.content_hash for item in revisions}))

    def test_topology_rebinds_rich_markup_and_reuses_unchanged_annotated_takes(self):
        self._enable_markup_preview()
        with self.database.session() as session:
            first = session.get(GenerationSegment, self.initial_segment_ids[0])
            second = session.get(GenerationSegment, self.initial_segment_ids[1])
            first_xml = (
                f'<segment id="{first.id}" boundary_after="scene">'
                '<dialogue><speaker ref="c-alice">'
                "<ins>Speak softly.</ins>A🙂 B</speaker></dialogue>"
                '<event kind="pause" duration_ms="120"/>'
                "</segment>"
            )
            second_xml = (
                f'<segment id="{second.id}" boundary_after="paragraph">'
                '<dialogue><speaker ref="c-alice">'
                "<ins>Speak clearly.</ins>A second block.</speaker></dialogue>"
                '<event kind="laugh"/>'
                "</segment>"
            )
            first.speech_plan_json = {"speech_xml": first_xml}
            second.speech_plan_json = {"speech_xml": second_xml}

        split = self._topology(
            self.initial_revision_id,
            {
                "action": "split",
                "segment_id": self.initial_segment_ids[0],
                "cursor": 2,
                "text_layer": "display",
            },
            "topology-rich-split-1",
        )
        self.assertEqual(201, split.status_code, split.get_json())
        split_revision_id = split.get_json()["plan_revision_id"]
        split_page = self._segments()
        left, right, untouched = split_page["items"]
        self.assertEqual(
            ["A🙂", "B", "A second block."], [item["text"] for item in split_page["items"]]
        )
        left_markup = self._parse_markup(left)
        right_markup = self._parse_markup(right)
        untouched_markup = self._parse_markup(untouched)
        for item, parsed in zip(
            split_page["items"],
            (left_markup, right_markup, untouched_markup),
            strict=True,
        ):
            self.assertEqual(item["id"], parsed.segment_id)
            self.assertEqual(item["optimized_text"] or item["text"], parsed.transcript)
        self.assertEqual("c-alice", left_markup.spans[0].speaker_id)
        self.assertEqual("c-alice", right_markup.spans[0].speaker_id)
        self.assertTrue(left_markup.spans[0].dialogue)
        self.assertEqual("Speak softly.", left_markup.spans[0].delivery["instruction"])
        self.assertEqual("Speak softly.", right_markup.spans[0].delivery["instruction"])
        self.assertEqual("scene", right_markup.boundary_after)
        self.assertEqual("pause", right_markup.events[0].kind)
        self.assertEqual("c-alice", untouched_markup.spans[0].speaker_id)
        self.assertEqual("laugh", untouched_markup.events[0].kind)
        reused = next(take for take in untouched["takes"] if take["is_active"])
        self.assertEqual(self.initial_take_ids[1], reused["parent_take_id"])
        for item in split_page["items"]:
            self._assert_preview(split_revision_id, item)

        with self.database.session() as session:
            self.assertEqual(
                first_xml,
                session.get(GenerationSegment, self.initial_segment_ids[0]).speech_plan_json[
                    "speech_xml"
                ],
            )
            self.assertEqual(
                second_xml,
                session.get(GenerationSegment, self.initial_segment_ids[1]).speech_plan_json[
                    "speech_xml"
                ],
            )
            split_revision = session.get(GenerationPlanRevision, split_revision_id)
            persisted_segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == split_revision_id)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            self.assertEqual(
                stable_hash(
                    {
                        "parent_revision_id": split_revision.parent_revision_id,
                        "operation": split_revision.operation_json,
                        "segments": [
                            GenerationService._segment_copy_values(segment)
                            for segment in persisted_segments
                        ],
                    }
                ),
                split_revision.content_hash,
            )

        merged = self._topology(
            split_revision_id,
            {
                "action": "merge",
                "left_segment_id": left["id"],
                "right_segment_id": right["id"],
            },
            "topology-rich-merge-1",
        )
        self.assertEqual(201, merged.status_code, merged.get_json())
        merged_revision_id = merged.get_json()["plan_revision_id"]
        merged_items = self._segments()["items"]
        merged_markup = self._parse_markup(merged_items[0])
        self.assertEqual("c-alice", merged_markup.spans[0].speaker_id)
        self.assertEqual("Speak softly.", merged_markup.spans[0].delivery["instruction"])
        self.assertEqual("pause", merged_markup.events[0].kind)
        self._assert_preview(merged_revision_id, merged_items[0])

        repeated = self._topology(
            merged_revision_id,
            {
                "action": "split",
                "segment_id": merged_items[0]["id"],
                "cursor": 2,
                "text_layer": "display",
            },
            "topology-rich-repeat-1",
        )
        self.assertEqual(201, repeated.status_code, repeated.get_json())
        repeated_revision_id = repeated.get_json()["plan_revision_id"]
        repeated_items = self._segments()["items"]
        repeated_markup = [self._parse_markup(item) for item in repeated_items]
        self.assertEqual(
            [item["id"] for item in repeated_items],
            [item.segment_id for item in repeated_markup],
        )
        self.assertEqual("pause", repeated_markup[1].events[0].kind)
        for item in repeated_items:
            self._assert_preview(repeated_revision_id, item)

        restored = self._topology(
            repeated_revision_id,
            {"action": "restore", "target_revision_id": self.initial_revision_id},
            "topology-rich-restore-1",
        )
        self.assertEqual(201, restored.status_code, restored.get_json())
        restored_revision_id = restored.get_json()["plan_revision_id"]
        restored_items = self._segments()["items"]
        restored_markup = [self._parse_markup(item) for item in restored_items]
        self.assertEqual(
            [item["id"] for item in restored_items],
            [item.segment_id for item in restored_markup],
        )
        self.assertEqual("scene", restored_markup[0].boundary_after)
        self.assertEqual("pause", restored_markup[0].events[0].kind)
        for item in restored_items:
            self._assert_preview(restored_revision_id, item)

        from pandrator.web.models import SessionRecord

        with self.database.session() as session:
            session.get(SessionRecord, self.session_id).workflow_kind = "audiobook"
            for job in session.scalars(select(Job).where(Job.session_id == self.session_id)):
                if job.status in {"queued", "running", "cancel_requested"}:
                    job.status = "canceled"

        resegmented = self._topology(
            restored_revision_id,
            {
                "action": "resegment",
                "segment_ids": [restored_items[0]["id"]],
                "boundaries": [2],
            },
            "topology-rich-resegment-1",
        )
        self.assertEqual(201, resegmented.status_code, resegmented.get_json())
        self.assertTrue(resegmented.get_json()["is_draft"])
        draft_revision_id = resegmented.get_json()["plan_revision_id"]
        with self.database.session() as session:
            draft_segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == draft_revision_id)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            self.assertEqual(3, len(draft_segments))
            from pandrator.logic.speech_markup import parse_speech_markup
            from pandrator.web.generation_controls import get_generation_controls

            controls = get_generation_controls(session, self.session_id)
            for segment in draft_segments:
                parsed = parse_speech_markup(
                    segment.speech_plan_json["speech_xml"],
                    expected_segment_id=segment.id,
                    expected_text=segment.optimized_text or segment.text,
                    characters=controls["characters"],
                )
                self.assertEqual(segment.id, parsed.segment_id)
            self.assertEqual(
                "pause",
                parse_speech_markup(
                    draft_segments[1].speech_plan_json["speech_xml"],
                    expected_segment_id=draft_segments[1].id,
                    expected_text=draft_segments[1].text,
                    characters=controls["characters"],
                )
                .events[0]
                .kind,
            )
        adopted = self._topology(
            restored_revision_id,
            {"action": "restore", "target_revision_id": draft_revision_id},
            "topology-rich-resegment-adopt-1",
        )
        self.assertEqual(201, adopted.status_code, adopted.get_json())
        adopted_revision_id = adopted.get_json()["plan_revision_id"]
        adopted_items = self._segments()["items"]
        adopted_markup = [self._parse_markup(item) for item in adopted_items]
        self.assertEqual(
            [item["id"] for item in adopted_items],
            [item.segment_id for item in adopted_markup],
        )
        self.assertEqual("pause", adopted_markup[1].events[0].kind)
        for item in adopted_items:
            self._assert_preview(adopted_revision_id, item)

    def test_invalid_topology_markup_rolls_back(self):
        service = self.app.extensions["pandrator"]["generation"]
        with self.database.session() as session:
            first = session.get(GenerationSegment, self.initial_segment_ids[0])
            malformed = f'<segment id="{first.id}"><speaker>'
            mismatch = f'<segment id="{first.id}">Different text.</segment>'

        from sqlalchemy import func

        for invalid_xml in (malformed, mismatch):
            self._put_markup(self.initial_segment_ids[0], invalid_xml)
            with self.database.session() as session:
                revision_count = session.scalar(select(func.count(GenerationPlanRevision.id)))
                active_revision_id = session.scalar(
                    select(GenerationPlan.active_revision_id).where(
                        GenerationPlan.session_id == self.session_id
                    )
                )
            with self.assertRaises(ValueError):
                service.revise_topology(
                    self.session_id,
                    self.initial_revision_id,
                    {
                        "action": "split",
                        "segment_id": self.initial_segment_ids[0],
                        "cursor": 2,
                        "text_layer": "display",
                    },
                )
            with self.database.session() as session:
                self.assertEqual(
                    revision_count,
                    session.scalar(select(func.count(GenerationPlanRevision.id))),
                )
                self.assertEqual(
                    active_revision_id,
                    session.scalar(
                        select(GenerationPlan.active_revision_id).where(
                            GenerationPlan.session_id == self.session_id
                        )
                    ),
                )
                self.assertEqual(
                    invalid_xml,
                    session.get(GenerationSegment, self.initial_segment_ids[0]).speech_plan_json[
                        "speech_xml"
                    ],
                )

    def test_optimized_text_rebuilds_only_plain_markup_and_keeps_cue_only_markup(self):
        from pandrator.logic.speech_markup import parse_speech_markup

        segment_id = self.initial_segment_ids[0]
        plain_xml = f'<segment id="{segment_id}" boundary_after="scene">A🙂 B</segment>'
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            segment.optimized_text = "A🙂 B"
            segment.speech_plan_json = {"speech_xml": plain_xml}

        service = self.app.extensions["pandrator"]["generation"]
        edited = service.update_segment(
            segment_id,
            1,
            {"optimized_text": "Spoken words."},
        )
        self.assertNotEqual(segment_id, edited["id"])
        edited_xml = edited["speech_plan"]["speech_xml"]
        parsed = parse_speech_markup(
            edited_xml,
            expected_segment_id=edited["id"],
            expected_text="Spoken words.",
        )
        self.assertEqual("scene", parsed.boundary_after)
        with self.database.session() as session:
            self.assertEqual(
                plain_xml,
                session.get(GenerationSegment, segment_id).speech_plan_json["speech_xml"],
            )

        cue_only = service.update_segment(
            edited["id"],
            edited["revision"],
            {"text": "New display cue", "optimized_text": "Spoken words."},
        )
        self.assertEqual(edited["id"], cue_only["id"])
        self.assertEqual(edited_xml, cue_only["speech_plan"]["speech_xml"])
        self.assertEqual("Spoken words.", cue_only["optimized_text"])

        cleared = service.update_segment(
            cue_only["id"], cue_only["revision"], {"optimized_text": ""}
        )
        self.assertEqual({}, cleared["speech_plan"])

    def test_rich_markup_spoken_text_edits_are_rejected_without_frozen_copy(self):
        self._enable_markup_preview()
        segment_id = self.initial_segment_ids[0]
        source_xml = self._rich_markup(segment_id, "A🙂 B")
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            segment.optimized_text = "A🙂 B"
            segment.speech_plan_json = {"speech_xml": source_xml}

        service = self.app.extensions["pandrator"]["generation"]
        before = self._edit_rollback_state([segment_id])
        for changes in (
            {"optimized_text": "Changed spoken words."},
            {"text": "Changed spoken words."},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "cannot be changed by a plain text edit"):
                    service.update_segment(segment_id, 1, changes)
                self.assertEqual(before, self._edit_rollback_state([segment_id]))

        with self.database.session() as session:
            original = session.get(GenerationSegment, segment_id)
            self.assertEqual("A🙂 B", original.optimized_text)
            self.assertEqual(source_xml, original.speech_plan_json["speech_xml"])

    def test_batch_rich_markup_rejection_rolls_back_plain_edit_and_frozen_copies(self):
        self._enable_markup_preview()
        plain_id, rich_id = self.initial_segment_ids
        plain_xml = f'<segment id="{plain_id}" boundary_after="paragraph">A🙂 B</segment>'
        rich_xml = self._rich_markup(rich_id, "A second block.")
        with self.database.session() as session:
            plain = session.get(GenerationSegment, plain_id)
            plain.optimized_text = "A🙂 B"
            plain.speech_plan_json = {"speech_xml": plain_xml}
            rich = session.get(GenerationSegment, rich_id)
            rich.optimized_text = "A second block."
            rich.speech_plan_json = {"speech_xml": rich_xml}

        service = self.app.extensions["pandrator"]["generation"]
        before = self._edit_rollback_state([plain_id, rich_id])
        with self.assertRaisesRegex(ValueError, "cannot be changed by a plain text edit"):
            service.update_segments(
                self.session_id,
                [
                    {
                        "id": plain_id,
                        "revision": 1,
                        "changes": {"optimized_text": "Changed plain words."},
                    },
                    {
                        "id": rich_id,
                        "revision": 1,
                        "changes": {"optimized_text": "Changed rich words."},
                    },
                ],
            )

        self.assertEqual(before, self._edit_rollback_state([plain_id, rich_id]))
        with self.database.session() as session:
            self.assertEqual(
                plain_xml, session.get(GenerationSegment, plain_id).speech_plan_json["speech_xml"]
            )
            self.assertEqual(
                rich_xml, session.get(GenerationSegment, rich_id).speech_plan_json["speech_xml"]
            )

    def test_rich_markup_cue_only_edit_preserves_speech_annotations(self):
        self._enable_markup_preview()
        segment_id = self.initial_segment_ids[0]
        source_xml = self._rich_markup(segment_id, "A🙂 B")
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            segment.optimized_text = "A🙂 B"
            segment.speech_plan_json = {"speech_xml": source_xml}

        service = self.app.extensions["pandrator"]["generation"]
        updated = service.update_segment(
            segment_id,
            1,
            {"text": "Updated display cue", "optimized_text": "A🙂 B"},
        )
        self.assertNotEqual(segment_id, updated["id"])
        self.assertEqual("Updated display cue", updated["text"])
        self.assertEqual("A🙂 B", updated["optimized_text"])
        parsed = self._parse_markup(updated)
        self.assertEqual(updated["id"], parsed.segment_id)
        self.assertEqual("A🙂 B", parsed.transcript)
        self.assertEqual("scene", parsed.boundary_after)
        self.assertEqual("c-alice", parsed.spans[0].speaker_id)
        self.assertTrue(parsed.spans[0].dialogue)
        self.assertEqual("Speak softly.", parsed.spans[0].delivery["instruction"])
        self.assertEqual("pause", parsed.events[0].kind)

        with self.database.session() as session:
            original = session.get(GenerationSegment, segment_id)
            self.assertEqual("A🙂 B", original.text)
            self.assertEqual("A🙂 B", original.optimized_text)
            self.assertEqual(source_xml, original.speech_plan_json["speech_xml"])

    def test_split_preserves_source_references_for_legacy_rows_without_spans(self):
        response = self._topology(
            self.initial_revision_id,
            {
                "action": "split",
                "segment_id": self.initial_segment_ids[1],
                "cursor": 8,
                "text_layer": "display",
            },
            "topology-legacy-source-1",
        )

        self.assertEqual(201, response.status_code, response.get_json())
        page = self._segments()
        children = page["items"][1:3]
        self.assertEqual(["A second", "block."], [item["text"] for item in children])
        for child in children:
            provenance = child["speech_block_provenance"]
            self.assertEqual(
                "generation_source_reference",
                provenance["source_reference_namespace"],
            )
            self.assertEqual(
                ["source-uuid-2"],
                [cue["reference"] for cue in provenance["source_cues"]],
            )
            self.assertEqual([], provenance["source_cues"][0]["display_spans"])


if __name__ == "__main__":
    unittest.main()
