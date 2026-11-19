import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pydub import AudioSegment
from pydub.generators import Sine
from sqlalchemy import func, select

from pandrator.logic.dubbing.audio_sync import align_audio_blocks
from pandrator.logic.dubbing.models import AudioAlignmentBlock
from pandrator.web.database import Database
from pandrator.web.generation_review import revision_history
from pandrator.web.job_identity import job_creation_ownership
from pandrator.web.models import (
    Artifact,
    AudioTake,
    Document,
    DocumentRevision,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    Segment,
)
from pandrator.web.sessions import SessionService
from pandrator.web.voiceover_repair import _load_groups, advance_timing
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workspace import GenerationService, WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root


class VoiceoverRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.record = SessionService(self.database).create(
            "Early repair", workflow_kind="voiceover"
        )
        (self.paths.sessions / self.record.storage_key).mkdir()
        self.first = "The first complete sentence."
        self.second = "The second complete sentence."

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def plan(self, *, enabled=True, previous=False, groups=1, tail=()):
        cues = []
        if previous:
            cues.append((0, 2000, "An earlier complete sentence."))
        for index in range(groups):
            start = (3000 if previous else 0) + index * 10000
            cues.extend(
                [
                    (start, start + 3000, self.first),
                    (start + 5000, start + 8000, self.second),
                ]
            )
        cues.append(
            (cues[-1][1] + 2000, cues[-1][1] + 4000, "The unchanged closing sentence.")
        )
        cues.extend(tail)
        with self.database.session() as session:
            document = Document(
                session_id=self.record.id, stage="translation", language="en"
            )
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id,
                revision_number=1,
                content_hash="repair-fixture",
            )
            session.add(revision)
            session.flush()
            source_id = revision.id
            for ordinal, (start, end, text) in enumerate(cues):
                session.add(
                    Segment(
                        revision_id=source_id,
                        ordinal=ordinal,
                        start_ms=start,
                        end_ms=end,
                        text=text,
                    )
                )
            document.active_revision_id = source_id
        records = []
        offset = 0
        while offset < len(cues):
            count = 2 if cues[offset][2] == self.first else 1
            selected = cues[offset : offset + count]
            text = " ".join(cue[2] for cue in selected)
            spans = []
            cursor = 0
            for index, (start, end, cue_text) in enumerate(selected):
                spans.append(
                    {
                        "reference": offset + index + 1,
                        "start_ms": start,
                        "end_ms": end,
                        "display_text": cue_text,
                        "speech_text": cue_text,
                        "display_spans": [[cursor, cursor + len(cue_text)]],
                        "speech_spans": [[cursor, cursor + len(cue_text)]],
                    }
                )
                cursor += len(cue_text) + 1
            records.append(
                {
                    "text": text,
                    "node_kind": "subtitle_cue",
                    "source_segment_ids": list(range(offset + 1, offset + count + 1)),
                    "alignment_group": f"a{offset}",
                    "speech_block_provenance": {
                        "source_cues": spans,
                        "source_reference_namespace": "subtitle_ordinal",
                    },
                }
            )
            offset += count
        self.revision_id, self.segment_ids = self.handlers._store_generation_plan(
            self.record.id,
            records,
            settings={},
            source_revision_id=source_id,
        )
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.record.id,
                sequence_number=int(session.scalar(select(func.max(GenerationRun.sequence_number)).where(GenerationRun.session_id == self.record.id)) or 0) + 1,
                plan_revision_id=self.revision_id,
                status="queued",
                settings_snapshot_json={
                    "tts": {
                        "service": "XTTS",
                        "speech_block_generation_mode": "legacy",
                        "speech_block_early_repair_enabled": enabled,
                    },
                    "audio": {
                        "synchronization_speed": 1.2,
                        "synchronization_delay_ms": 0,
                    },
                    "text": {"llm_tts_optimization": False},
                },
            )
            session.add(run)
            session.flush()
            self.run_id = run.id

    def generate(self, synth=None, event=None):
        from pandrator.web.generation_performance_snapshot import (
            performance_runtime_settings,
        )
        from pandrator.web.speech_plan_workspace import freeze_speech_snapshot

        # Real generation admission freezes the complete semantic window
        # before TTS starts. These tests invoke run_generation directly, so
        # reproduce that admission step for contextual runs. Tests for
        # intentionally legacy/unfrozen runs may still create one with
        # tts_context_mode=off and mutate it after generation.
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            snapshot = dict(run.settings_snapshot_json or {})
            settings = performance_runtime_settings(snapshot)
            if (
                settings.get("tts_context_mode", "off") != "off"
                and not snapshot.get("semantic_context_snapshot")
            ):
                freeze_speech_snapshot(
                    session, run.plan_revision_id, snapshot, explicit=True
                )
                run.settings_snapshot_json = snapshot

        def ordinary(text, *_args, **_kwargs):
            return Sine(440).to_audio_segment(
                duration=2000
                if text.startswith(self.first) and self.second in text
                else 1000
            )

        with patch(
            "pandrator.logic.tts_handler.text_to_audio", side_effect=synth or ordinary
        ) as mocked:
            result = self.handlers.run_generation(
                {"generation_run_id": self.run_id, "operation": "generate"},
                lambda *_args: None,
                event or threading.Event(),
            )
        return result, mocked.call_count

    def active(self):
        with self.database.session() as session:
            return session.scalar(
                select(GenerationPlan.active_revision_id).where(
                    GenerationPlan.session_id == self.record.id
                )
            )

    def repairs(self):
        return [
            item
            for item in revision_history(self.database, self.record.id)["items"]
            if item["reason"] == "early_timing_repair"
        ]

    def select_alternate_closing_take(self):
        segment_id = self.segment_ids[-1]
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            expected_revision = segment.revision
            original = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_segment_id == segment_id,
                    AudioTake.is_active.is_(True),
                )
            )
            alternate = AudioTake(
                generation_segment_id=segment_id,
                artifact_id=original.artifact_id,
                settings_hash=original.settings_hash,
                duration_ms=original.duration_ms,
                status="completed",
            )
            session.add(alternate)
            session.flush()
            alternate_id = alternate.id
        service = GenerationService(
            self.database, self.handlers.jobs, WorkspaceSettingsService(self.database)
        )
        service.select_take(segment_id, alternate_id, expected_revision)
        return alternate_id

    def assert_closing_take_selected(self, take_id):
        with self.database.session() as session:
            selected = list(
                session.scalars(
                    select(AudioTake.id).where(
                        AudioTake.generation_segment_id == self.segment_ids[-1],
                        AudioTake.is_active.is_(True),
                    )
                )
            )
        self.assertEqual([take_id], selected)

    def _check_native_repair_pause(self, stage):
        self.plan()
        service = GenerationService(
            self.database, self.handlers.jobs, WorkspaceSettingsService(self.database)
        )
        job = self.handlers.jobs.enqueue(
            "generation.run", {"generation_run_id": self.run_id}, session_id=self.record.id
        )
        with self.database.session() as session:
            session.get(GenerationRun, self.run_id).job_id = job.id
        claimed = self.handlers.jobs.claim("repair-pause-fixture")
        self.assertEqual(job.id, claimed.id)
        observed = {}
        calls = 0

        def request_pause():
            self.assertEqual("pausing", service.request_pause(self.run_id)["status"])
            with self.database.session() as session:
                takes = list(
                    session.scalars(
                        select(AudioTake).where(AudioTake.generation_run_id == self.run_id)
                    )
                )
                observed["takes"] = [(take.id, take.artifact_id, take.created_at) for take in takes]
                observed["files"] = {
                    take.artifact_id: self.paths.managed_path(
                        session.get(Artifact, take.artifact_id).relative_path
                    ).read_bytes()
                    for take in takes
                }
            self.assertEqual(2, len(observed["takes"]))

        def synth(text, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            if stage == "child" and calls == 3:
                request_pause()
            return Sine(440).to_audio_segment(
                duration=(2000 if text.startswith(self.first) and self.second in text else 1000)
            )

        def progress(_value, detail=None):
            if stage == "inspection" and detail == "Repairing timing: 1 of 2 blocks":
                request_pause()

        with patch("pandrator.logic.tts_handler.text_to_audio", side_effect=synth):
            result = self.handlers.run_generation(
                {"generation_run_id": self.run_id}, progress, threading.Event()
            )
        self.assertEqual("paused", result["status"])
        self.assertEqual(0, result["repaired_blocks"])
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual(2 if stage == "inspection" else 3, calls)
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            self.assertEqual("paused", run.status)
            self.assertTrue(run.pause_requested)
        self.handlers.jobs.complete(
            job.id, "repair-pause-fixture", result, lease_generation=claimed.lease_generation
        )
        with self.database.session() as session:
            self.assertEqual("paused", session.get(GenerationRun, self.run_id).status)
        resumed = service.resume(self.run_id)
        self.assertEqual("queued", resumed["status"])
        resumed_job = self.handlers.jobs.claim("repair-resume-fixture")
        self.assertEqual(resumed["job_id"], resumed_job.id)
        with patch(
            "pandrator.logic.tts_handler.text_to_audio",
            return_value=(Sine(440).to_audio_segment(duration=1000)),
        ) as synthesize:
            result = self.handlers.run_generation(
                resumed_job.payload_json, lambda *_args: None, threading.Event()
            )
        resume_calls = synthesize.call_count
        self.handlers.jobs.complete(
            resumed_job.id,
            "repair-resume-fixture",
            result,
            lease_generation=resumed_job.lease_generation,
        )
        self.assertEqual("completed", result["status"])
        self.assertEqual((0, 2), (result["generated"], result["skipped"]))
        self.assertEqual(2, resume_calls)
        self.assertEqual(1, result["repaired_blocks"])
        with self.database.session() as session:
            for take_id, artifact_id, created_at in observed["takes"]:
                take = session.get(AudioTake, take_id)
                self.assertEqual(artifact_id, take.artifact_id)
                self.assertEqual(created_at, take.created_at)
                self.assertTrue(take.is_active)
                self.assertEqual("completed", take.status)
                path = self.paths.managed_path(session.get(Artifact, artifact_id).relative_path)
                self.assertEqual(observed["files"][artifact_id], path.read_bytes())

    def test_native_pause_during_repair_inspection_preserves_checkpoint_and_resume(self):
        self._check_native_repair_pause("inspection")

    def test_native_pause_during_repair_child_preserves_checkpoint_and_resume(self):
        self._check_native_repair_pause("child")

    def test_disabled_keeps_original_plan_and_number_of_generations(self):
        self.plan(enabled=False)
        result, calls = self.generate()
        self.assertEqual(2, calls)
        self.assertEqual(self.revision_id, self.active())
        self.assertNotIn("repaired_blocks", result)

    def test_repairs_once_reuses_unchanged_takes_and_preserves_original(self):
        self.plan()
        result, calls = self.generate()
        self.assertEqual(1, result.get("repaired_blocks"))
        self.assertEqual(4, calls)
        self.assertNotEqual(self.revision_id, self.active())
        self.assertNotEqual(self.run_id, result["generation_run_id"])
        self.assertEqual("automatic", self.repairs()[0]["origin"])
        self.assertEqual("applied", self.repairs()[0]["repair_status"])
        with self.database.session() as session:
            original = list(
                session.scalars(
                    select(AudioTake).where(AudioTake.generation_run_id == self.run_id)
                )
            )
            self.assertEqual(2, len(original))
            self.assertTrue(all(take.is_active for take in original))
            new = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == self.active())
                    .order_by(GenerationSegment.ordinal)
                )
            )
            self.assertEqual(
                [self.first, self.second, "The unchanged closing sentence."],
                [item.text for item in new],
            )
            clone = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_segment_id == new[-1].id,
                    AudioTake.generation_run_id == result["generation_run_id"],
                )
            )
            self.assertIn(clone.parent_take_id, [take.id for take in original])
            self.assertEqual(
                self.revision_id,
                session.get(GenerationPlanRevision, self.active()).parent_revision_id,
            )
            from pandrator.web.repair_batches import GUARD_KEY, repair_state_hash
            accepted = session.get(GenerationPlanRevision, self.active())
            guard = accepted.operation_json[GUARD_KEY]
            self.assertEqual(guard["base_state_hash"], repair_state_hash(session, self.revision_id))
            self.assertEqual(guard["result_state_hash"], repair_state_hash(session, self.active()))

    def _generate_with_identity_probe(self, probe):
        """Generate, calling ``probe`` while a staged split's audio identities are computed."""
        from pandrator.web import voiceover_repair

        original = voiceover_repair.plan_audio_identities

        def plan_audio_identities(session, revision_id, *args, **kwargs):
            probe(revision_id)
            return original(session, revision_id, *args, **kwargs)

        self.plan()
        with patch.object(
            voiceover_repair, "plan_audio_identities", side_effect=plan_audio_identities
        ):
            return self.generate()

    def test_staged_split_computes_audio_identities_outside_the_writer(self):
        probes = []

        def take_the_writer(_revision_id):
            # A held writer would make this fail at once with "database is locked".
            with self.database.immediate_session(busy_timeout_ms=0):
                probes.append("writer free")

        result, calls = self._generate_with_identity_probe(take_the_writer)

        self.assertEqual(["writer free"], probes)
        self.assertEqual(1, result.get("repaired_blocks"))
        self.assertEqual(4, calls)

    def test_inputs_changed_while_staged_identities_are_computed_are_not_applied(self):
        from pandrator.web.models import SessionRecord

        def change_the_session(_revision_id):
            with self.database.immediate_session() as session:
                session.get(SessionRecord, self.record.id).revision += 1

        result, calls = self._generate_with_identity_probe(change_the_session)

        self.assertEqual(0, result.get("repaired_blocks"))
        # The two first-pass takes only: the staged split is never generated.
        self.assertEqual(2, calls)
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual(
            1, result.get("repair_rejection_reasons", {}).get("selection_changed")
        )
        self.assertEqual(
            [("not_applied", "selection_changed")],
            [(item["repair_status"], item["repair_reason"]) for item in self.repairs()],
        )

    def test_active_revision_changed_while_staged_identities_are_computed_is_not_generated(self):
        def activate_another_revision(revision_id):
            with self.database.immediate_session() as session:
                plan = session.scalar(
                    select(GenerationPlan).where(GenerationPlan.session_id == self.record.id)
                )
                plan.active_revision_id = revision_id

        result, calls = self._generate_with_identity_probe(activate_another_revision)

        self.assertEqual(0, result.get("repaired_blocks"))
        # The two first-pass takes only: the staged split is never generated.
        self.assertEqual(2, calls)
        self.assertEqual(
            1, result.get("repair_rejection_reasons", {}).get("selection_changed")
        )
        self.assertEqual(
            [("not_applied", "selection_changed")],
            [(item["repair_status"], item["repair_reason"]) for item in self.repairs()],
        )

    def test_partial_timing_inspection_is_not_reused_as_completed(self):
        self.plan()
        import pandrator.web.voiceover_repair as repair_module

        real_advance = repair_module.advance_timing
        inspected = []

        def inspect(group, cursor, slot_end, settings, work, cancel_event, **kwargs):
            inspected.append(group.start_ms)
            if group.start_ms == 10000:
                raise RuntimeError("synthetic timing inspection failure")
            return real_advance(
                group, cursor, slot_end, settings, work, cancel_event, **kwargs
            )

        with (
            patch.object(repair_module, "advance_timing", side_effect=inspect),
            patch.object(repair_module, "find_repair_boundary", return_value=None),
        ):
            result, _calls = self.generate()
        self.assertEqual(1, result["repair_checked_blocks"])
        self.assertEqual(0, result["repaired_blocks"])
        self.assertFalse(result["repair_completed"])
        self.assertTrue(result["repair_inspection_failed"])
        self.assertEqual(
            {"timing_inspection_failed": 1}, result["repair_rejection_reasons"]
        )
        self.assertEqual(self.revision_id, self.active())

        job = self.handlers.jobs.enqueue(
            "generation.repair-existing",
            {
                "session_id": self.record.id,
                "generation_run_id": self.run_id,
            },
            session_id=self.record.id,
        )
        claimed = self.handlers.jobs.claim("partial-inspection-fixture")
        self.assertEqual(job.id, claimed.id)
        self.handlers.jobs.complete(
            job.id,
            "partial-inspection-fixture",
            result,
            lease_generation=claimed.lease_generation,
        )
        service = GenerationService(
            self.database, self.handlers.jobs, WorkspaceSettingsService(self.database)
        )
        retry = service.request_timing_repair(self.run_id)
        self.assertFalse(retry["reused_job"])
        self.assertNotEqual(job.id, retry["job_id"])
        repeat = service.request_timing_repair(self.run_id)
        self.assertTrue(repeat["reused_job"])
        self.assertEqual(retry["job_id"], repeat["job_id"])

    def test_partial_repair_keeps_applied_child_but_rejects_repeated_repair(self):
        self.plan(groups=2)
        import pandrator.web.voiceover_repair as repair_module

        job = self.handlers.jobs.enqueue(
            "generation.run",
            {"generation_run_id": self.run_id, "operation": "generate"},
            session_id=self.record.id,
        )
        with self.database.session() as session:
            session.get(GenerationRun, self.run_id).job_id = job.id
        claimed = self.handlers.jobs.claim("partial-applied-generation")
        self.assertEqual(job.id, claimed.id)
        real_advance = repair_module.advance_timing

        def inspect(group, cursor, slot_end, settings, work, cancel_event, **kwargs):
            if group.start_ms == 10000:
                raise RuntimeError("synthetic timing inspection failure")
            return real_advance(
                group, cursor, slot_end, settings, work, cancel_event, **kwargs
            )

        with patch.object(repair_module, "advance_timing", side_effect=inspect):
            result, _calls = self.generate()
        self.handlers.jobs.complete(
            job.id,
            "partial-applied-generation",
            result,
            lease_generation=claimed.lease_generation,
        )
        self.assertEqual(1, result["repaired_blocks"])
        self.assertFalse(result["repair_completed"])
        self.assertEqual(1, result["repair_checked_blocks"])
        applied_revision = result["repaired_plan_revision_id"]
        applied_run = result["repaired_generation_run_id"]
        self.assertNotEqual(self.revision_id, applied_revision)
        self.assertEqual(applied_revision, self.active())
        with self.database.session() as session:
            original = list(
                session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_run_id == self.run_id
                    )
                )
            )
            self.assertEqual(3, len(original))
            self.assertTrue(all(take.is_active for take in original))
            child = session.get(GenerationRun, applied_run)
            self.assertEqual("completed", child.status)
        from pandrator.web.generation_history_reads import GenerationHistoryReader

        listed = next(
            item
            for item in GenerationHistoryReader(self.database).list_runs(
                self.record.id
            )
            if item["id"] == self.run_id
        )
        self.assertEqual("partial", listed["timing_repair"]["status"])
        self.assertFalse(listed["timing_repair"]["pass_completed"])
        service = GenerationService(
            self.database, self.handlers.jobs, WorkspaceSettingsService(self.database)
        )
        with self.assertRaisesRegex(ValueError, "active speech plan"):
            service.request_timing_repair(self.run_id)
        with self.assertRaisesRegex(ValueError, "at most once"):
            service.request_timing_repair(applied_run)
        with self.database.session() as session:
            self.assertEqual(
                0,
                session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(
                        Job.session_id == self.record.id,
                        Job.kind == "generation.repair-existing",
                    )
                ),
            )

    def test_passage_repair_rebinds_one_before_one_after_semantic_context(self):
        from pandrator.web.generation_performance_snapshot import (
            frozen_semantic_contexts,
        )

        self.plan()
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_generation_mode": "passage",
                    "tts_context_mode": "both",
                    "performance_context_before": 1,
                    "performance_context_after": 1,
                },
            }
            first = session.get(GenerationSegment, self.segment_ids[0])
            first.speech_block_provenance_json = {
                **dict(first.speech_block_provenance_json or {}),
                "section_id": "religious-history",
            }
            closing = session.get(GenerationSegment, self.segment_ids[-1])
            closing.speech_block_provenance_json = {
                **dict(closing.speech_block_provenance_json or {}),
                "section_id": "discussion",
            }

        result, calls = self.generate()
        self.assertEqual(1, result.get("repaired_blocks"))
        self.assertNotEqual(self.revision_id, self.active())
        self.assertGreaterEqual(calls, 4)

        with self.database.session() as session:
            accepted = session.get(GenerationRun, result["generation_run_id"])
            snapshot = accepted.settings_snapshot_json
            frozen = snapshot["semantic_context_snapshot"]
            self.assertEqual(self.active(), frozen["plan_revision_id"])
            rows = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == self.active())
                    .order_by(GenerationSegment.ordinal)
                )
            )
            self.assertEqual(
                [self.first, self.second, "The unchanged closing sentence."],
                [row.text for row in rows],
            )
            self.assertEqual(
                ["religious-history", "religious-history"],
                [row.speech_block_provenance_json.get("section_id") for row in rows[:2]],
            )
            contexts = frozen_semantic_contexts(snapshot)
            left, right, closing = rows
            self.assertEqual(self.second, contexts[left.id]["after"])
            self.assertEqual(self.first, contexts[right.id]["before"])
            self.assertEqual("", contexts[closing.id]["before"])
            self.assertTrue(all(row.id in contexts for row in rows))
            self.assertTrue(all(row.id in {unit["id"] for unit in frozen["units"]} for row in rows))
            original = session.get(GenerationRun, self.run_id)
            self.assertEqual(
                original.settings_snapshot_json["tts"]["tts_context_mode"],
                snapshot["tts"]["tts_context_mode"],
            )

    def test_audio_section_context_mode_refreshes_neighbours_as_tts_mode(self):
        from pandrator.web.generation_performance_snapshot import (
            frozen_semantic_contexts,
        )

        self.plan()
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_generation_mode": "passage",
                },
                "audio": {
                    **run.settings_snapshot_json["audio"],
                    "tts_context_mode": "both",
                    "performance_context_before": 1,
                    "performance_context_after": 1,
                },
            }

        result, calls = self.generate()
        self.assertEqual(1, result.get("repaired_blocks"))
        self.assertEqual(5, calls)
        with self.database.session() as session:
            child = session.get(GenerationRun, result["generation_run_id"])
            contexts = frozen_semantic_contexts(child.settings_snapshot_json)
            self.assertEqual(3, len(contexts))
            self.assertEqual(
                self.active(),
                child.settings_snapshot_json["semantic_context_snapshot"][
                    "plan_revision_id"
                ],
            )

    def test_passage_repair_replaces_neighbour_with_changed_semantic_context(self):
        from pandrator.web.generation_performance_snapshot import (
            frozen_semantic_contexts,
        )

        self.plan()
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_generation_mode": "passage",
                    "tts_context_mode": "both",
                    "performance_context_before": 1,
                    "performance_context_after": 1,
                },
            }

        result, calls = self.generate()
        self.assertEqual(1, result.get("repaired_blocks"))
        # Two original takes, two split children, and the immediately
        # following block whose frozen preceding context has changed.
        self.assertEqual(5, calls)
        with self.database.session() as session:
            accepted = session.get(GenerationRun, result["generation_run_id"])
            rows = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == self.active())
                    .order_by(GenerationSegment.ordinal)
                )
            )
            contexts = frozen_semantic_contexts(accepted.settings_snapshot_json)
            self.assertEqual(self.second, contexts[rows[-1].id]["before"])
            take = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_segment_id == rows[-1].id,
                    AudioTake.generation_run_id == accepted.id,
                    AudioTake.status == "completed",
                )
            )
            self.assertIsNotNone(take)
            self.assertIsNone(take.parent_take_id)
            self.assertTrue(take.is_active)
            active_takes = list(
                session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_segment_id == rows[-1].id,
                        AudioTake.status == "completed",
                        AudioTake.is_active.is_(True),
                    )
                )
            )
            self.assertEqual([take.id], [item.id for item in active_takes])

    def _contextual_plan(self, *, previous: bool = False) -> None:
        """Frozen 1/1 semantic context, so a split refreshes its neighbours."""

        self.plan(previous=previous)
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_generation_mode": "passage",
                    "tts_context_mode": "both",
                    "performance_context_before": 1,
                    "performance_context_after": 1,
                },
            }

    def _neighbour_durations(self, calls: dict[str, int], target: str, lengths):
        """Return a synth that changes one neighbour's take on regeneration."""

        def synth(text, *_args, **_kwargs):
            if text == target:
                calls["seen"] = calls.get("seen", 0) + 1
                duration = lengths[0] if calls["seen"] == 1 else lengths[1]
            else:
                duration = (
                    2000
                    if text.startswith(self.first) and self.second in text
                    else 1000
                )
            return Sine(440).to_audio_segment(duration=duration)

        return synth

    def test_following_neighbour_growth_within_its_window_is_applied(self):
        """A longer refreshed follower that still fits its cue is adopted."""

        self._contextual_plan()
        result, calls = self.generate(
            synth=self._neighbour_durations(
                {}, "The unchanged closing sentence.", (1000, 1500)
            )
        )
        self.assertEqual(5, calls)
        self.assertEqual(1, result.get("repaired_blocks"))
        self.assertNotEqual(self.revision_id, self.active())
        self.assertEqual("applied", self.repairs()[0]["repair_status"])

    def test_following_neighbour_growth_that_pushes_later_speech_is_rejected(self):
        self._contextual_plan()
        result, calls = self.generate(
            synth=self._neighbour_durations(
                {}, "The unchanged closing sentence.", (1000, 3000)
            )
        )
        self.assertEqual(5, calls)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(self.revision_id, self.active())
        history = self.repairs()
        self.assertEqual("not_applied", history[0]["repair_status"])
        self.assertEqual("context_neighbour_added_delay", history[0]["repair_reason"])

    def test_preceding_neighbour_growth_within_its_window_is_applied(self):
        """A longer refreshed predecessor that keeps the cue anchor is adopted."""

        self._contextual_plan(previous=True)
        result, calls = self.generate(
            synth=self._neighbour_durations(
                {}, "An earlier complete sentence.", (1000, 2500)
            )
        )
        self.assertEqual(1, result.get("repaired_blocks"))
        self.assertEqual("applied", self.repairs()[0]["repair_status"])

    def test_preceding_neighbour_growth_that_pushes_the_candidate_is_rejected(self):
        self._contextual_plan(previous=True)
        result, calls = self.generate(
            synth=self._neighbour_durations(
                {}, "An earlier complete sentence.", (1000, 4000)
            )
        )
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(self.revision_id, self.active())
        history = self.repairs()
        self.assertEqual("not_applied", history[0]["repair_status"])
        self.assertEqual("context_neighbour_added_delay", history[0]["repair_reason"])

    def test_unchanged_neighbour_growth_path_takes_no_extra_calls(self):
        """Without neighbour growth the pass keeps its unchanged measurement."""

        self._contextual_plan()
        result, calls = self.generate()
        self.assertEqual(5, calls)
        self.assertEqual(1, result.get("repaired_blocks"))
        self.assertEqual("applied", self.repairs()[0]["repair_status"])

    def test_take_speech_spans_removes_its_measurement_files(self):
        from pandrator.web.voiceover_repair import _take_speech_spans

        work = Path(self.temporary.name) / "repair-work"
        work.mkdir()
        source = Path(self.temporary.name) / "take.wav"
        Sine(440).to_audio_segment(duration=250).export(source, format="wav")

        duration, lead, tail = _take_speech_spans(source, work, threading.Event())

        self.assertGreater(duration, 0)
        self.assertGreaterEqual(lead, 0)
        self.assertGreaterEqual(tail, 0)
        # The decoded and trimmed copies belong to the measurement alone, so
        # nothing named after a neighbour survives it.
        self.assertEqual([], list(work.iterdir()))

    def test_german_clause_conjunction_regenerates_children_in_target_language(self):
        self.first = "Wir lassen den ganzen Gedanken zunächst zusammen"
        self.second = "weil natürliche Sprache hier wichtiger ist."
        self.plan()
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {**run.settings_snapshot_json["tts"], "language": "de"},
            }
        result, calls = self.generate()
        self.assertEqual(1, result.get("repaired_blocks"))
        self.assertEqual(4, calls)
        self.assertNotEqual(self.revision_id, self.active())
        self.assertEqual("applied", self.repairs()[0]["repair_status"])

    def test_segment_language_overrides_generic_tts_language_during_repair(self):
        self.first = "Wir lassen den ganzen Gedanken zunächst zusammen"
        self.second = "weil natürliche Sprache hier wichtiger ist."
        self.plan()
        with self.database.session() as session:
            segment = session.get(GenerationSegment, self.segment_ids[0])
            segment.language = "de"
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {**run.settings_snapshot_json["tts"], "language": "en"},
            }
        result, calls = self.generate()
        self.assertEqual(1, result.get("repaired_blocks"))
        self.assertEqual(4, calls)

    def test_bare_german_noun_coordination_does_not_trigger_timing_repair(self):
        self.first = "Die Sammlung enthält eine große Zahl gedruckter Bücher"
        self.second = "und handgeschriebene Briefe aus mehreren Jahrhunderten."
        self.plan()
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {**run.settings_snapshot_json["tts"], "language": "de"},
            }
        result, calls = self.generate()
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(2, calls)
        self.assertEqual(self.revision_id, self.active())

    def test_carried_delay_preserves_short_block_for_catchup(self):
        self.plan(previous=True)

        def synth(text, *_args, **_kwargs):
            return Sine(440).to_audio_segment(
                duration=8000 if text.startswith("An earlier") else 2000
            )

        result, calls = self.generate(synth)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(3, calls)
        self.assertEqual(self.revision_id, self.active())

    def test_slowdown_can_resolve_shortfall_without_splitting(self):
        self.plan()

        def synth(text, *_args, **_kwargs):
            return Sine(440).to_audio_segment(
                duration=6000 if self.first in text and self.second in text else 1000
            )

        result, calls = self.generate(synth)
        # 6 s in an 8 s cue exceeds the raw 20% shortfall threshold, but
        # gentle slowdown leaves less than 20%: do not regenerate good speech.
        self.assertEqual(2, calls)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(self.revision_id, self.active())

    def test_replacement_must_reach_its_new_internal_anchor(self):
        self.plan()

        def synth(text, *_args, **_kwargs):
            return Sine(440).to_audio_segment(
                duration=7000 if text == self.first else 2000
                if self.first in text and self.second in text else 1000
            )

        result, calls = self.generate(synth)
        # Both children finish before the next original block, but the first
        # overruns the new second-child anchor even at the speed cap.
        self.assertEqual(4, calls)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual("missed_repair_anchor", self.repairs()[0]["repair_reason"])

    def test_estimated_internal_timing_is_not_a_trusted_repair_anchor(self):
        self.plan()
        with self.database.session() as session:
            segment = session.get(GenerationSegment, self.segment_ids[0])
            segment.speech_block_provenance_json = {
                **segment.speech_block_provenance_json,
                "risk_flags": ["estimated_internal_timing"],
            }
        result, calls = self.generate()
        self.assertEqual(2, calls)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(self.revision_id, self.active())

    def test_replacement_overrun_does_not_activate_new_plan(self):
        self.plan()

        def synth(text, *_args, **_kwargs):
            is_child = text in (self.first, self.second)
            return Sine(440).to_audio_segment(duration=16000 if is_child else 2000)

        result, calls = self.generate(synth)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(4, calls)
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual("not_applied", self.repairs()[0]["repair_status"])
        self.assertEqual("added_delay", self.repairs()[0]["repair_reason"])

    def test_failure_keeps_original_plan_and_successful_original_run(self):
        self.plan()

        def synth(text, *_args, **_kwargs):
            if text == self.first:
                raise RuntimeError("simulated repair synthesis failure")
            return Sine(440).to_audio_segment(duration=2000)

        result, _ = self.generate(synth)
        self.assertEqual("completed", result["status"])
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual("failed", self.repairs()[0]["repair_status"])

    def test_cancel_during_repair_keeps_original_selected(self):
        self.plan()
        event = threading.Event()

        def synth(text, *_args, **_kwargs):
            if text == self.first:
                event.set()
            return Sine(440).to_audio_segment(duration=2000)

        result, _ = self.generate(synth, event)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual("stopped", self.repairs()[0]["repair_status"])

    def test_multiple_original_blocks_are_repaired_without_recursing_on_children(self):
        self.plan(groups=2)
        result, calls = self.generate()
        self.assertEqual(2, result.get("repaired_blocks"))
        self.assertEqual(7, calls)
        self.assertEqual(
            ["applied", "applied"], [item["repair_status"] for item in self.repairs()]
        )

    def test_multiple_repaired_blocks_keep_unique_contextual_active_takes(self):
        from pandrator.web.generation_performance_snapshot import (
            frozen_semantic_contexts,
        )

        self.plan(groups=2)
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_generation_mode": "passage",
                    "tts_context_mode": "both",
                    "performance_context_before": 1,
                    "performance_context_after": 1,
                },
            }
        result, calls = self.generate()
        self.assertEqual(2, result.get("repaired_blocks"))
        self.assertGreaterEqual(calls, 7)
        self.assertEqual(
            ["applied", "applied"], [row["repair_status"] for row in self.repairs()]
        )
        with self.database.session() as session:
            active_revision = self.active()
            final_run = session.get(GenerationRun, result["generation_run_id"])
            self.assertEqual(active_revision, final_run.plan_revision_id)
            contexts = frozen_semantic_contexts(final_run.settings_snapshot_json)
            segments = list(
                session.scalars(
                    select(GenerationSegment).where(
                        GenerationSegment.plan_revision_id == active_revision
                    )
                )
            )
            self.assertEqual(5, len(segments))
            self.assertEqual({item.id for item in segments}, set(contexts))
            for segment in segments:
                takes = list(
                    session.scalars(
                        select(AudioTake).where(
                            AudioTake.generation_segment_id == segment.id,
                            AudioTake.is_active.is_(True),
                            AudioTake.status == "completed",
                        )
                    )
                )
                self.assertEqual(1, len(takes), segment.text)

    def test_timing_preview_matches_real_forward_assembly_with_slowdown(self):
        self.plan(enabled=False, previous=True, groups=2)

        def synth(text, *_args, **_kwargs):
            return Sine(440).to_audio_segment(
                duration=8000 if text.startswith("An earlier") else 2000
            )

        self.generate(synth)
        groups, _ = _load_groups(self.handlers, self.run_id)
        for slowdown in (False, True):
            with (
                self.subTest(slowdown=slowdown),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                settings = {
                    "synchronization_speed": 1.2,
                    "synchronization_delay_ms": 800,
                    "synchronization_slowdown_enabled": slowdown,
                }
                diagnostics = {}
                align_audio_blocks(
                    [
                        AudioAlignmentBlock(
                            number=str(index),
                            text="fixture",
                            start_ms=group.start_ms,
                            end_ms=group.end_ms,
                            audio_files=group.paths,
                            subtitles=group.references,
                        )
                        for index, group in enumerate(groups)
                    ],
                    root,
                    delay_start_ms=800,
                    speed_up_percent=120,
                    allow_slowdown=slowdown,
                    diagnostics=diagnostics,
                    backend="streaming",
                )
                cursor = 0
                for index, group in enumerate(groups):
                    end = (
                        groups[index + 1].start_ms
                        if index + 1 < len(groups)
                        else group.end_ms
                    )
                    cursor, duration, delay = advance_timing(
                        group,
                        cursor,
                        end,
                        settings,
                        root,
                        threading.Event(),
                        has_next_block=index + 1 < len(groups),
                    )
                    actual = diagnostics["blocks"][index]
                    self.assertEqual(actual["original_audio_ms"], duration)
                    self.assertEqual(actual["start_delay_ms"], delay)
                    self.assertEqual(end + actual["drift_after_ms"], cursor)

    def test_timing_preview_measures_the_trimmed_block(self):
        self.plan(enabled=False)

        def synth(text, *_args, **_kwargs):
            return (
                AudioSegment.silent(duration=300, frame_rate=24_000)
                + Sine(440, sample_rate=24_000).to_audio_segment(duration=270)
                + AudioSegment.silent(duration=400, frame_rate=24_000)
            )

        self.generate(synth)
        groups, _ = _load_groups(self.handlers, self.run_id)
        group = groups[0]
        for trim, expected in ((True, 390), (False, 970)):
            with (
                self.subTest(trim=trim),
                tempfile.TemporaryDirectory() as temporary,
            ):
                settings = {
                    "synchronization_speed": 1.0,
                    "synchronization_delay_ms": 0,
                    "synchronization_slowdown_enabled": False,
                    "synchronization_trim_silence": trim,
                }
                diagnostics = {}
                _cursor, duration, _delay = advance_timing(
                    group,
                    0,
                    group.end_ms,
                    settings,
                    Path(temporary),
                    threading.Event(),
                    diagnostics=diagnostics,
                )
                self.assertEqual(expected, duration)
                self.assertEqual(expected, diagnostics["original_audio_ms"])

    def test_plan_change_during_repair_is_not_overwritten(self):
        self.plan()
        other_revision = None

        def synth(text, *_args, **_kwargs):
            nonlocal other_revision
            if text == self.second:
                other_revision, _ = self.handlers._store_generation_plan(
                    self.record.id,
                    [{"text": "Another plan selected while generation runs."}],
                    settings={},
                    force_new=True,
                )
            return Sine(440).to_audio_segment(duration=2000)

        result, _ = self.generate(synth)
        self.assertIsNotNone(other_revision)
        self.assertEqual(other_revision, self.active())
        self.assertEqual(0, result.get("repaired_blocks"))

    def test_queued_explicit_repair_is_visible_without_an_enabling_snapshot(self):
        from pandrator.web.generation_history_reads import GenerationHistoryReader

        self.plan(enabled=False)
        initial, _ = self.generate()
        self.assertEqual("completed", initial["status"])
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            self.assertFalse(
                run.settings_snapshot_json["tts"]["speech_block_early_repair_enabled"]
            )
            # A root receipt that reports an inline repair must not stand in
            # for the queued explicit pass, which has staged nothing yet.
            ownership = job_creation_ownership(session, session_id=self.record.id)
            root_job = Job(
                session_id=self.record.id,
                kind="generation.run",
                status="succeeded",
                payload_json={"generation_run_id": self.run_id},
                result_json={
                    "repair_checked_blocks": 7,
                    "repair_candidate_blocks": 3,
                    "repair_attempted_blocks": 1,
                    "repaired_blocks": 1,
                    "repair_completed": True,
                },
            )
            root_job.workspace_id = ownership.workspace_id
            root_job.project_id = ownership.project_id
            session.add(root_job)
            session.flush()
            run.job_id = root_job.id

        service = GenerationService(
            self.database, self.handlers.jobs, WorkspaceSettingsService(self.database)
        )
        queued = service.request_timing_repair(self.run_id)
        listed = next(
            item
            for item in GenerationHistoryReader(self.database).list_runs(self.record.id)
            if item["id"] == self.run_id
        )
        self.assertEqual("queued", queued["status"])
        self.assertEqual("running", listed["status"])
        self.assertEqual("repairing_timing", listed["phase"])
        self.assertEqual("running", listed["timing_repair"]["status"])
        self.assertIsNone(listed["timing_repair"]["checked_count"])
        self.assertIsNone(listed["timing_repair"]["candidate_count"])
        self.assertIsNone(listed["timing_repair"]["attempted_count"])
        self.assertEqual(0, listed["timing_repair"]["attempt_count"])

    def test_completed_root_can_queue_and_execute_timing_only_job(self):
        from unittest.mock import patch

        from pandrator.web.models import Job

        # Simulate a root completed by an older engine: repair was configured
        # in its frozen settings but not invoked during original generation.
        # The original synthesis must never be invoked again for this job.
        self.plan(enabled=False)
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_generation_mode": "passage",
                    "tts_context_mode": "both",
                    "performance_context_before": 1,
                    "performance_context_after": 1,
                },
            }
        initial, calls = self.generate()
        self.assertEqual(2, calls)
        self.assertEqual("completed", initial["status"])
        self.assertEqual(self.revision_id, self.active())
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_early_repair_enabled": True,
                },
            }

        service = GenerationService(
            self.database, self.handlers.jobs, WorkspaceSettingsService(self.database)
        )
        first = service.request_timing_repair(self.run_id)
        repeat = service.request_timing_repair(self.run_id)
        self.assertFalse(first["reused_job"])
        self.assertTrue(repeat["reused_job"])
        self.assertEqual(first["job_id"], repeat["job_id"])
        with self.database.session() as session:
            job = session.get(Job, first["job_id"])
            self.assertEqual("generation.repair-existing", job.kind)
            self.assertEqual(self.record.id, job.payload_json["session_id"])
            self.assertEqual(self.run_id, job.payload_json["generation_run_id"])

        def synth(text, *_args, **_kwargs):
            return Sine(440).to_audio_segment(duration=1000)

        from pandrator.web.job_execution import current_job_lease

        claimed = self.handlers.jobs.claim("repair-only-fixture", lease_seconds=300)
        self.assertIsNotNone(claimed)
        self.assertEqual(first["job_id"], claimed.id)
        lease_token = current_job_lease.set({
            "job_id": claimed.id,
            "owner_id": claimed.lease_owner,
            "lease_generation": claimed.lease_generation,
        })
        try:
            with patch("pandrator.logic.tts_handler.text_to_audio", side_effect=synth) as mocked:
                result = self.handlers.run_repair_existing_generation(
                    {
                        "generation_run_id": self.run_id,
                        "session_id": self.record.id,
                        "_job_id": claimed.id,
                        "_lease_generation": claimed.lease_generation,
                    },
                    lambda *_args: None,
                    threading.Event(),
                )
        finally:
            current_job_lease.reset(lease_token)
        self.assertEqual(1, result["repaired_blocks"])
        self.assertEqual(3, mocked.call_count)
        self.assertNotEqual(self.revision_id, self.active())
        with self.database.session() as session:
            root = session.get(GenerationRun, self.run_id)
            self.assertEqual("completed", root.status)
            original_count = session.scalar(
                select(func.count(AudioTake.id)).where(
                    AudioTake.generation_run_id == self.run_id,
                    AudioTake.status == "completed",
                )
            )
            self.assertEqual(2, original_count)
            children = list(
                session.scalars(
                    select(GenerationRun).where(
                        GenerationRun.session_id == self.record.id,
                        GenerationRun.id != self.run_id,
                    )
                )
            )
            self.assertTrue(children)
            self.assertTrue(
                all(child.job_id == first["job_id"] for child in children)
            )

    def test_contextual_legacy_run_without_frozen_context_fails_closed(self):
        from pandrator.web.voiceover_repair import repair_early_blocks

        # A previous engine version may have generated a complete lecture
        # without recording the neighbours that accompanied each take.
        self.plan(enabled=False)
        result, calls = self.generate()
        self.assertEqual(2, calls)
        self.assertEqual(self.revision_id, self.active())
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_generation_mode": "passage",
                    "speech_block_early_repair_enabled": True,
                    "tts_context_mode": "both",
                    "performance_context_before": 1,
                    "performance_context_after": 1,
                },
            }
            self.assertFalse(run.settings_snapshot_json.get("semantic_context_snapshot"))

        checked = repair_early_blocks(
            self.handlers, self.run_id, lambda *_args: None, threading.Event()
        )
        self.assertEqual(0, checked["repaired_blocks"])
        self.assertEqual(0, checked["repair_checked_blocks"])
        self.assertEqual(
            {"missing_source_semantic_context": 1},
            checked["repair_rejection_reasons"],
        )
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual([], self.repairs())

    def test_unsupported_context_snapshot_schema_declines_repair_without_crashing(self):
        from pandrator.web.voiceover_repair import repair_early_blocks

        self.plan(enabled=False)
        result, calls = self.generate()
        self.assertEqual(2, calls)
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_generation_mode": "passage",
                    "speech_block_early_repair_enabled": True,
                    "tts_context_mode": "both",
                    "performance_context_before": 1,
                    "performance_context_after": 1,
                },
                "semantic_context_snapshot": {
                    "schema_version": 2,
                    "plan_revision_id": self.revision_id,
                    "units": [{"id": self.segment_ids[0], "text": "stale"}],
                },
            }
        checked = repair_early_blocks(
            self.handlers, self.run_id, lambda *_args: None, threading.Event()
        )
        self.assertEqual(0, checked["repaired_blocks"])
        self.assertEqual(0, checked["repair_checked_blocks"])
        self.assertEqual(
            {"missing_source_semantic_context": 1},
            checked["repair_rejection_reasons"],
        )
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual([], self.repairs())

    def test_take_selection_before_staging_preserves_choice_without_regeneration(self):
        self.plan()
        alternate_id = None

        def preview(*args, **kwargs):
            nonlocal alternate_id
            result = advance_timing(*args, **kwargs)
            if alternate_id is None:
                alternate_id = self.select_alternate_closing_take()
            return result

        with patch(
            "pandrator.web.voiceover_repair.advance_timing", side_effect=preview
        ):
            result, calls = self.generate()
        self.assertEqual(2, calls)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertGreater(
            result.get("repair_rejection_reasons", {}).get("selection_changed", 0), 0
        )
        self.assertEqual(self.revision_id, self.active())
        self.assert_closing_take_selected(alternate_id)

    def test_take_selection_during_child_generation_is_not_overwritten(self):
        self.plan()
        alternate_id = None

        def synth(text, *_args, **_kwargs):
            nonlocal alternate_id
            if text == self.first:
                alternate_id = self.select_alternate_closing_take()
            return Sine(440).to_audio_segment(duration=2000)

        result, calls = self.generate(synth)
        self.assertEqual(4, calls)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(self.revision_id, self.active())
        self.assert_closing_take_selected(alternate_id)
        self.assertEqual("not_applied", self.repairs()[0]["repair_status"])
        self.assertEqual("selection_changed", self.repairs()[0]["repair_reason"])

    def test_thresholds_from_generation_snapshot_control_the_pass(self):
        for suffix, value in (
            ("min_shortfall_ms", 7000),
            ("min_shortfall_percent", 80),
            ("min_advance_ms", 5000),
            ("min_child_span_ms", 4000),
        ):
            with self.subTest(threshold=suffix):
                self.plan()
                with self.database.session() as session:
                    run = session.get(GenerationRun, self.run_id)
                    run.settings_snapshot_json = {
                        **run.settings_snapshot_json,
                        "tts": {
                            **run.settings_snapshot_json["tts"],
                            f"speech_block_early_repair_{suffix}": value,
                        },
                    }
                result, calls = self.generate()
                self.assertEqual(2, calls)
                self.assertEqual(0, result["repaired_blocks"])
                self.assertEqual(self.revision_id, self.active())

    def test_legacy_repair_history_does_not_invent_an_outcome(self):
        self.plan()
        self.generate()
        with self.database.session() as session:
            revision = session.get(GenerationPlanRevision, self.active())
            operation = dict(revision.operation_json)
            operation.pop("repair_status")
            revision.operation_json = operation
        self.assertEqual("automatic", self.repairs()[0]["origin"])
        self.assertEqual("unknown", self.repairs()[0]["repair_status"])

    def run_repair_job(self, job_id: str, synth=None):
        from pandrator.web.job_execution import current_job_lease

        claimed = self.handlers.jobs.claim("explicit-repair-fixture", lease_seconds=300)
        self.assertIsNotNone(claimed)
        self.assertEqual(job_id, claimed.id)
        lease_token = current_job_lease.set(
            {
                "job_id": claimed.id,
                "owner_id": claimed.lease_owner,
                "lease_generation": claimed.lease_generation,
            }
        )
        try:
            with patch(
                "pandrator.logic.tts_handler.text_to_audio",
                side_effect=synth
                or (
                    lambda _text, *_args, **_kwargs: Sine(440).to_audio_segment(
                        duration=1000
                    )
                ),
            ) as mocked:
                result = self.handlers.run_repair_existing_generation(
                    {
                        **dict(claimed.payload_json),
                        "_job_id": claimed.id,
                        "_lease_generation": claimed.lease_generation,
                    },
                    lambda *_args: None,
                    threading.Event(),
                )
        finally:
            current_job_lease.reset(lease_token)
        return result, mocked.call_count

    def test_automatic_pass_stays_off_when_the_frozen_flag_is_off(self):
        self.plan(enabled=False)
        result, calls = self.generate()
        self.assertEqual(2, calls)
        self.assertEqual(0, result.get("repaired_blocks", 0))
        self.assertNotIn("repair_checked_blocks", result)
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual([], self.repairs())

    def test_explicit_repair_uses_current_thresholds_and_frozen_identity(self):
        self.plan(enabled=False)
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    # The frozen thresholds would never select this block.
                    "speech_block_early_repair_min_shortfall_ms": 7000,
                },
            }
        initial, calls = self.generate()
        self.assertEqual(2, calls)
        self.assertEqual(self.revision_id, self.active())
        with self.database.session() as session:
            original_hashes = {
                take.id: take.settings_hash
                for take in session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_run_id == self.run_id,
                        AudioTake.status == "completed",
                    )
                )
            }
        self.assertTrue(original_hashes)

        settings = WorkspaceSettingsService(self.database)
        current = settings.get(self.record.id, "tts")
        settings.patch(
            self.record.id,
            "tts",
            current["revision"],
            {"speech_block_early_repair_min_shortfall_ms": 1200},
        )
        service = GenerationService(self.database, self.handlers.jobs, settings)
        queued = service.request_timing_repair(self.run_id)
        with self.database.session() as session:
            job = session.get(Job, queued["job_id"])
            self.assertEqual(1200, job.payload_json["repair_thresholds"]["min_shortfall_ms"])
            self.assertEqual(
                {
                    "min_shortfall_ms",
                    "min_shortfall_percent",
                    "min_advance_ms",
                    "min_child_span_ms",
                },
                set(job.payload_json["repair_thresholds"]),
            )

        result, repair_calls = self.run_repair_job(queued["job_id"])
        self.assertEqual(1, result["repaired_blocks"])
        self.assertGreaterEqual(repair_calls, 2)
        self.assertNotEqual(self.revision_id, self.active())
        with self.database.session() as session:
            child_takes = list(
                session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_run_id
                        == result["repaired_generation_run_id"],
                        AudioTake.status == "completed",
                    )
                )
            )
        self.assertTrue(child_takes)
        # Synthesis still uses the run's frozen TTS settings, so a new take's
        # audio identity matches the original ones.
        self.assertEqual(
            set(original_hashes.values()),
            {take.settings_hash for take in child_takes},
        )

    def test_preview_lists_candidates_and_call_estimate_without_queueing(self):
        from pandrator.web.voiceover_repair import preview_early_blocks

        self.plan(enabled=False)
        self.generate()
        thresholds = {
            "min_shortfall_ms": 1000,
            "min_shortfall_percent": 20,
            "min_advance_ms": 1000,
            "min_child_span_ms": 1000,
        }
        preview = preview_early_blocks(
            self.database, self.paths, self.run_id, thresholds=thresholds
        )
        self.assertTrue(preview["preview"])
        self.assertEqual(2, preview["planned_blocks"])
        self.assertEqual(2, preview["checked_count"])
        self.assertEqual(1, preview["candidate_count"])
        candidate = preview["candidates"][0]
        self.assertEqual(0, candidate["ordinal"])
        self.assertEqual(self.segment_ids[0], candidate["segment_id"])
        # The fixture's 2000 ms take renders as 2218 ms at its 1.2 sync speed,
        # so the 8000 ms cue span leaves 5782 ms of shortfall.
        self.assertEqual(8000 - 2218, candidate["shortfall_ms"])
        self.assertEqual(5000, candidate["proposed_split"]["boundary_ms"])
        # Two halves; no neighbours are regenerated with context off.
        self.assertEqual(2, candidate["estimated_synthesis_calls"])
        self.assertEqual(2, preview["estimated_synthesis_calls"])
        self.assertIsNone(candidate["skipped_reason"])

        with self.database.session() as session:
            self.assertEqual(
                0,
                int(
                    session.scalar(
                        select(func.count())
                        .select_from(Job)
                        .where(
                            Job.session_id == self.record.id,
                            Job.kind == "generation.repair-existing",
                        )
                    )
                    or 0
                ),
            )
        self.assertEqual(self.revision_id, self.active())

        # A threshold the current settings exceed hides the candidate.
        self.assertEqual(
            0,
            preview_early_blocks(
                self.database,
                self.paths,
                self.run_id,
                thresholds={**thresholds, "min_shortfall_ms": 7000},
            )["candidate_count"],
        )

    def test_preview_counts_the_neighbour_it_would_regenerate(self):
        from pandrator.web.voiceover_repair import preview_early_blocks

        self.plan(enabled=False)
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_generation_mode": "passage",
                    "tts_context_mode": "both",
                    "performance_context_before": 1,
                    "performance_context_after": 1,
                },
            }
        self.generate()
        preview = preview_early_blocks(self.database, self.paths, self.run_id)
        self.assertEqual(1, preview["candidate_count"])
        # Two halves plus the following block whose frozen context changes.
        self.assertEqual(3, preview["candidates"][0]["estimated_synthesis_calls"])
        self.assertEqual(3, preview["estimated_synthesis_calls"])
        self.assertIsNone(preview["candidates"][0]["skipped_reason"])

    def test_neighbour_growth_precheck_skips_a_split_that_cannot_gain(self):
        """Only the split's own pause makes a candidate a certain loser."""

        self.plan(enabled=False)
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "audio": {
                    **dict(run.settings_snapshot_json.get("audio") or {}),
                    # The halves are placed as separate blocks, so the pause
                    # between them is the block gap. The modelled advance
                    # (about 3.9 s) cannot cover it, so the second half could
                    # never start earlier.
                    "synchronization_min_block_gap_ms": 5000,
                },
            }

        initial, calls = self.generate()
        self.assertEqual(2, calls)
        service = GenerationService(
            self.database, self.handlers.jobs, WorkspaceSettingsService(self.database)
        )
        queued = service.request_timing_repair(self.run_id)
        result, repair_calls = self.run_repair_job(queued["job_id"])
        self.assertEqual(0, repair_calls)
        self.assertEqual(0, result["repaired_blocks"])
        self.assertEqual(1, result["repair_candidate_blocks"])
        self.assertEqual(0, result["repair_attempted_blocks"])
        self.assertEqual(
            {"neighbour_growth_precheck": 1}, result["repair_rejection_reasons"]
        )
        self.assertEqual(self.revision_id, self.active())

    def test_neighbour_growth_precheck_ignores_cue_window_slack(self):
        from pandrator.web.voiceover_repair import neighbour_growth_precheck

        self.assertIsNone(
            neighbour_growth_precheck(
                estimated_advance_ms=3000, pause_ms=250
            )
        )
        # Growth is verified by placement now, so window slack never skips;
        # only an advance at or below the pause does.
        self.assertIsNone(
            neighbour_growth_precheck(
                estimated_advance_ms=1000, pause_ms=999
            )
        )
        self.assertEqual(
            "neighbour_growth_precheck",
            neighbour_growth_precheck(
                estimated_advance_ms=249, pause_ms=250
            ),
        )

    def test_repair_effect_reports_late_cues_before_and_after(self):
        self.plan()
        result, _calls = self.generate()
        self.assertEqual(1, result["repaired_blocks"])
        effect = result["repair_effect"]
        self.assertEqual(1, effect["blocks"])
        # Before the split the second cue starts after the single take ended.
        self.assertGreater(effect["late_cues_before"], 0)
        self.assertEqual(0, effect["late_cues_after"])
        self.assertGreater(effect["worst_lag_ms_before"], 0)
        self.assertEqual(0, effect["worst_lag_ms_after"])

    def test_accepted_repair_adopts_the_refreshed_take_for_later_measurement(self):
        from pandrator.web.voiceover_repair import TimingGroup, _adopt_refreshed_takes

        groups = [
            TimingGroup([], [Path("a.wav")], 0, 1000, [1]),
            TimingGroup([], [Path("b.wav")], 2000, 3000, [2]),
        ]
        # A segment-count mismatch cannot be aligned safely and is ignored.
        _adopt_refreshed_takes(
            groups,
            {1: TimingGroup([], [Path("new.wav"), Path("x.wav")], 2000, 3000, [2])},
        )
        self.assertEqual([Path("a.wav")], groups[0].paths)
        self.assertEqual([Path("b.wav")], groups[1].paths)
        _adopt_refreshed_takes(
            groups, {1: TimingGroup([], [Path("new.wav")], 2000, 3000, [2])}
        )
        self.assertEqual([Path("a.wav")], groups[0].paths)
        self.assertEqual([Path("new.wav")], groups[1].paths)

    def test_preview_refuses_when_the_selection_moved_off_the_run(self):
        from pandrator.web.voiceover_repair import preview_early_blocks

        self.plan(enabled=False)
        self.generate()
        with self.database.session() as session:
            take = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_run_id == self.run_id,
                    AudioTake.generation_segment_id == self.segment_ids[0],
                )
            )
            take.is_active = False
        preview = preview_early_blocks(self.database, self.paths, self.run_id)
        # The pass would stop with selection_changed before staging, so the
        # preview quotes no candidates and no synthesis calls.
        self.assertEqual("selection_changed", preview["skipped_reason"])
        self.assertEqual(0, preview["candidate_count"])
        self.assertEqual(0, preview["estimated_synthesis_calls"])

    def test_context_neighbour_count_counts_segments_of_multi_segment_blocks(self):
        from types import SimpleNamespace

        from pandrator.web.voiceover_repair import TimingGroup, _context_neighbour_count

        def block(*ids: str) -> TimingGroup:
            return TimingGroup(
                [SimpleNamespace(id=item) for item in ids],  # pyright: ignore[reportArgumentType]
                [Path(f"{item}.wav") for item in ids],
                0,
                1000,
                [1],
            )

        groups = [block("a"), block("b"), block("c1", "c2"), block("d")]
        settings = {"performance_context_before": 2, "performance_context_after": 1}
        # Splitting block 1 can refresh block 0 and blocks 2 and 3; block 2
        # holds two segments, and the pass re-synthesises each changed one.
        self.assertEqual(4, _context_neighbour_count(groups, 1, settings))

    def test_a_split_block_is_never_located_as_a_single_slot(self):
        from types import SimpleNamespace

        from pandrator.web.voiceover_repair import TimingGroup, _refreshed_neighbour_groups

        def block(ids: list[str], start: int) -> TimingGroup:
            return TimingGroup(
                [SimpleNamespace(id=item) for item in ids],  # pyright: ignore[reportArgumentType]
                [Path(f"{item}.wav") for item in ids],
                start,
                start + 1000,
                [1],
            )

        groups = [block(["orig-a"], 0), block(["orig-b"], 2000)]
        # orig-a was split earlier; its first child "a1" carries the mapping.
        current_ids = {"orig-a": "a1", "orig-b": "b"}
        staged = [block(["new-a1"], 0), block(["new-b"], 2000)]
        refreshed = {"new-a1": "a1", "new-b": "b"}
        located = _refreshed_neighbour_groups(
            groups, current_ids, refreshed, staged, {"orig-a"}
        )
        self.assertNotIn("a1", located)
        self.assertEqual(1, located["b"][0])
        # Without the split record the first child would stand in for both.
        self.assertIn("a1", _refreshed_neighbour_groups(groups, current_ids, refreshed, staged))

    def test_a_block_pushed_late_mid_run_counts_as_added_delay(self):
        from pandrator.web.voiceover_repair import TimingGroup, _starts_later

        groups = [
            TimingGroup([], [Path("a.wav")], 0, 1000, [1]),
            TimingGroup([], [Path("b.wav")], 1000, 2000, [2]),
            TimingGroup([], [Path("c.wav")], 3000, 4000, [3]),
        ]
        baseline = {1: 1000, 2: 2800}
        # Block 1 now enters 200 ms late, although block 2 still enters on time.
        self.assertTrue(_starts_later(groups, {1: 1200, 2: 2800}, baseline))
        # Entering earlier than a block's own start never counts as late.
        self.assertFalse(_starts_later(groups, {1: 900, 2: 2900}, baseline))

    def test_a_shorter_take_with_more_speech_counts_as_grown(self):
        from pandrator.web.voiceover_repair import _may_have_grown

        # (duration, leading silence, trailing silence) in ms. The original
        # 970 ms take holds 390 ms of speech; the refreshed one is shorter
        # overall but holds 850 ms, so trimmed placement can push later blocks.
        self.assertTrue(_may_have_grown((970, 300, 280), (850, 0, 0)))
        # Shorter and no more speech at either edge cannot outgrow its slot.
        self.assertFalse(_may_have_grown((970, 300, 280), (900, 300, 280)))
        # Speech that only grows once one edge is trimmed still counts.
        self.assertTrue(_may_have_grown((900, 500, 100), (850, 0, 600)))

    def _following_overrun_plan(self, *, overrun: bool) -> dict:
        """Candidate, a non-grown follower, then a follower that grows a little."""

        self.plan(tail=((13000, 15000, "A final single sentence."),))
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_generation_mode": "passage",
                    "tts_context_mode": "both",
                    # Two units back reaches the split, so both followers are
                    # refreshed even though only the second one grows.
                    "performance_context_before": 2,
                    "performance_context_after": 1,
                },
            }
        seen: dict[str, int] = {}

        def synth(text, *_args, **_kwargs):
            if text == "The unchanged closing sentence.":
                # Same length before and after: not grown. Long enough to
                # overrun its slot and push the next block when entered early.
                return Sine(440).to_audio_segment(duration=4800 if overrun else 1000)
            if text == "A final single sentence.":
                seen["final"] = seen.get("final", 0) + 1
                # Grows by 800 ms inside a 2000 ms window: below its nominal
                # 1000 ms slack, but enough to overflow once the follower has
                # pushed the cursor.
                return Sine(440).to_audio_segment(
                    duration=1000 if seen["final"] == 1 else 1800
                )
            return Sine(440).to_audio_segment(
                duration=2000
                if text.startswith(self.first) and self.second in text
                else 1000
            )

        return self.generate(synth=synth)

    def test_grown_follower_is_measured_after_a_non_grown_overrunning_block(self):
        result, _calls = self._following_overrun_plan(overrun=True)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(self.revision_id, self.active())
        history = self.repairs()
        self.assertEqual("not_applied", history[0]["repair_status"])
        self.assertEqual("context_neighbour_added_delay", history[0]["repair_reason"])

    def test_same_followers_without_the_overrun_are_applied(self):
        result, _calls = self._following_overrun_plan(overrun=False)
        self.assertEqual(1, result.get("repaired_blocks"))
        self.assertNotEqual(self.revision_id, self.active())
        self.assertEqual("applied", self.repairs()[0]["repair_status"])

    def test_repair_usage_counts_rejected_attempts(self):
        from pandrator.web.models import UsageEvent
        from pandrator.web.usage_identity import bind_usage_ownership

        self._contextual_plan()
        result, _calls = self.generate(
            synth=self._neighbour_durations(
                {}, "The unchanged closing sentence.", (1000, 3000)
            )
        )
        self.assertEqual(0, result["repaired_blocks"])
        service = GenerationService(
            self.database, self.handlers.jobs, WorkspaceSettingsService(self.database)
        )
        root = next(
            item
            for item in service.list_runs(self.record.id)
            if item["id"] == self.run_id
        )
        rejected_id = root["timing_repair"]["versions"][0]["generation_run_id"]
        with self.database.session() as session:
            for run_id, cost in ((self.run_id, 0.73), (rejected_id, 0.14)):
                event = UsageEvent(
                    session_id=self.record.id,
                    generation_run_id=run_id,
                    provider_key="fixture",
                    model_id="fixture-model",
                    input_tokens=1,
                    cost_usd=cost,
                )
                bind_usage_ownership(session, event)
                session.add(event)
        root = next(
            item
            for item in service.list_runs(self.record.id)
            if item["id"] == self.run_id
        )
        # A rejected attempt's calls belong to the repair pass, not the run.
        self.assertEqual(1, root["timing_repair"]["usage"]["event_count"])
        self.assertAlmostEqual(
            0.14, root["timing_repair"]["usage"]["total_cost_usd"]
        )
        self.assertEqual(2, root["timing_repair"]["usage_total"]["event_count"])

    def test_progress_detail_names_each_pass(self):
        self.plan()
        details: list[str] = []

        def progress(value, detail=None):
            details.append(str(detail))

        with patch(
            "pandrator.logic.tts_handler.text_to_audio",
            side_effect=lambda *_args, **_kwargs: Sine(440).to_audio_segment(
                duration=1000
            ),
        ):
            self.handlers.run_generation(
                {"generation_run_id": self.run_id, "operation": "generate"},
                progress,
                threading.Event(),
            )
        self.assertTrue(
            any(item.startswith("Generating speech: ") for item in details), details
        )
        self.assertTrue(
            any(item.startswith("Repairing timing: ") for item in details), details
        )


if __name__ == "__main__":
    unittest.main()
