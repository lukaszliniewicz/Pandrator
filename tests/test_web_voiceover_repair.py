import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pydub.generators import Sine
from sqlalchemy import func, select

from pandrator.logic.dubbing.audio_sync import align_audio_blocks
from pandrator.logic.dubbing.models import AudioAlignmentBlock
from pandrator.web.database import Database
from pandrator.web.generation_review import revision_history
from pandrator.web.models import (
    Artifact,
    AudioTake,
    Document,
    DocumentRevision,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
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

    def plan(self, *, enabled=True, previous=False, groups=1):
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
            if stage == "inspection" and detail == "Checking voiceover timing 1 of 2":
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

    def test_context_neighbour_duration_growth_rejects_optional_repair(self):
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

        closing_calls = 0

        def longer_neighbour(text, *_args, **_kwargs):
            nonlocal closing_calls
            if text == "The unchanged closing sentence.":
                closing_calls += 1
                duration = 1000 if closing_calls == 1 else 13000
            else:
                duration = (
                    2000
                    if text.startswith(self.first) and self.second in text
                    else 1000
                )
            return Sine(440).to_audio_segment(duration=duration)

        result, calls = self.generate(synth=longer_neighbour)
        self.assertEqual(5, calls)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(self.revision_id, self.active())
        history = self.repairs()
        self.assertEqual("not_applied", history[0]["repair_status"])
        self.assertEqual(
            "context_neighbour_added_duration", history[0]["repair_reason"]
        )

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


if __name__ == "__main__":
    unittest.main()
