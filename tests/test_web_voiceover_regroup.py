"""Optional second-pass regroup on the real generation run path.

End-to-end through WorkflowHandlers.run_generation with patched TTS
synthesis (cheap sine audio, no model or provider calls). Covers accepted
and rejected regenerated groups, failure fallback with originals retained,
cancellation, stale take selection, and the two-pass ceiling.
"""

import threading
import unittest
from unittest.mock import patch

from pydub.generators import Sine
from sqlalchemy import func, select

from pandrator.web.database import Database
from pandrator.web.generation_history_reads import GenerationHistoryReader
from pandrator.web.models import (
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
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workspace import GenerationService, WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root

FIRST = "First short passage."
SECOND = "Second short passage."
THIRD = "Third short passage."
FOURTH = "Fourth short passage."
FIFTH = "Fifth short passage."
TEXTS = (FIRST, SECOND, THIRD, FOURTH, FIFTH)


class VoiceoverRegroupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = __import__("tempfile").TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.record = SessionService(self.database).create(
            "Passage regroup", workflow_kind="voiceover"
        )
        (self.paths.sessions / self.record.storage_key).mkdir()

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def plan(self, *, enabled=True, mode="passage", turn_ids=None, passages=3):
        cues = [
            (index * 3200, index * 3200 + 3000, text)
            for index, text in enumerate(TEXTS[:passages])
        ]
        with self.database.session() as session:
            document = Document(
                session_id=self.record.id, stage="translation", language="en"
            )
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id,
                revision_number=1,
                content_hash="regroup-fixture",
            )
            session.add(revision)
            session.flush()
            source_id = revision.id
            for ordinal, (start, end, text) in enumerate(cues):
                session.add(
                    Segment(
                        revision_id=source_id,
                        ordinal=ordinal,
                        node_kind=("logical_passage" if turn_ids else "subtitle_cue"),
                        start_ms=start,
                        end_ms=end,
                        text=text,
                        metadata_json=(
                            {"logical_passage": {"turn_id": turn_ids[ordinal]}}
                            if turn_ids
                            else {}
                        ),
                    )
                )
            document.active_revision_id = source_id
        records = []
        for offset, (start, end, text) in enumerate(cues):
            record = {
                    "text": text,
                    "node_kind": "subtitle_cue",
                    "source_segment_ids": [offset + 1],
                    "alignment_group": f"a{offset:04d}",
                    "speech_block_provenance": {
                        **({"turn_id": turn_ids[offset]} if turn_ids else {}),
                        "source_cues": [
                            {
                                "reference": offset + 1,
                                "start_ms": start,
                                "end_ms": end,
                                "display_text": text,
                                "speech_text": text,
                                "display_spans": [[0, len(text)]],
                                "speech_spans": [[0, len(text)]],
                            }
                        ],
                        "source_reference_namespace": "subtitle_ordinal",
                    },
                }
            if turn_ids:
                record["turn_id"] = turn_ids[offset]
            records.append(record)
        self.revision_id, self.segment_ids = self.handlers._store_generation_plan(
            self.record.id,
            records,
            settings={},
            source_revision_id=source_id,
        )
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.record.id,
                sequence_number=int(
                    session.scalar(
                        select(func.max(GenerationRun.sequence_number)).where(
                            GenerationRun.session_id == self.record.id
                        )
                    )
                    or 0
                )
                + 1,
                plan_revision_id=self.revision_id,
                status="queued",
                settings_snapshot_json={
                    "tts": {
                        "service": "XTTS",
                        "speech_block_generation_mode": mode,
                        "speech_block_regroup_enabled": enabled,
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
        def ordinary(text, *_args, **_kwargs):
            if text == f"{FIRST} {SECOND} {THIRD}":
                return Sine(440).to_audio_segment(duration=9200)
            return Sine(440).to_audio_segment(duration=3000)

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

    def regroup_attempts(self):
        with self.database.session() as session:
            plan = session.scalar(
                select(GenerationPlan).where(
                    GenerationPlan.session_id == self.record.id
                )
            )
            rows = session.scalars(
                select(GenerationPlanRevision).where(
                    GenerationPlanRevision.plan_id == plan.id
                )
            ).all()
            return [
                {
                    "id": row.id,
                    "reason": (row.operation_json or {}).get("reason"),
                    "repair_status": (row.operation_json or {}).get("repair_status"),
                    "repair_reason": (row.operation_json or {}).get("repair_reason"),
                }
                for row in rows
            ]

    def active_texts(self):
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(GenerationSegment.text)
                    .where(GenerationSegment.plan_revision_id == self.active())
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )

    def test_accepted_group_regenerates_once_and_preserves_originals(self):
        self.plan()
        result, calls = self.generate()
        self.assertEqual(1, result.get("regrouped_groups"))
        # Three first-pass takes plus exactly one grouped generation: no
        # recursive regrouping of the regenerated audio.
        self.assertEqual(4, calls)
        self.assertNotEqual(self.revision_id, self.active())
        self.assertNotEqual(self.run_id, result["generation_run_id"])
        self.assertEqual([f"{FIRST} {SECOND} {THIRD}"], self.active_texts())
        attempts = [
            item
            for item in self.regroup_attempts()
            if item["reason"] == "passage_regroup"
        ]
        self.assertTrue(attempts)
        self.assertEqual("applied", attempts[-1]["repair_status"])
        with self.database.session() as session:
            applied = session.get(GenerationPlanRevision, self.active())
            mapping = (applied.operation_json or {}).get("mapping") or []
            # Triple lineage: every original passage ID maps to the merged
            # segment, so the group stays inspectable and revertible.
            self.assertEqual(
                sorted(item["source_segment_id"] for item in mapping),
                sorted(self.segment_ids),
            )
            self.assertTrue(all(item["new_segments"] == ["merged"] for item in mapping))
        with self.database.session() as session:
            original = list(
                session.scalars(
                    select(AudioTake).where(AudioTake.generation_run_id == self.run_id)
                )
            )
            self.assertEqual(3, len(original))
            self.assertTrue(all(take.is_active for take in original))
            staged = session.get(GenerationRun, result["generation_run_id"])
            self.assertEqual(
                self.run_id,
                (staged.settings_snapshot_json or {}).get("regroup_parent_run_id"),
            )

    def _generate_with_identity_probe(self, probe):
        """Generate, calling ``probe`` while a staged group's audio identities are computed."""
        from pandrator.web import voiceover_regroup

        original = voiceover_regroup.plan_audio_identities

        def plan_audio_identities(session, revision_id, *args, **kwargs):
            probe(revision_id)
            return original(session, revision_id, *args, **kwargs)

        self.plan()
        with patch.object(
            voiceover_regroup, "plan_audio_identities", side_effect=plan_audio_identities
        ):
            return self.generate()

    def test_staged_group_computes_audio_identities_outside_the_writer(self):
        probes = []

        def take_the_writer(_revision_id):
            # A held writer would make this fail at once with "database is locked".
            with self.database.immediate_session(busy_timeout_ms=0):
                probes.append("writer free")

        result, calls = self._generate_with_identity_probe(take_the_writer)

        self.assertEqual(["writer free"], probes)
        self.assertEqual(1, result.get("regrouped_groups"))
        self.assertEqual(4, calls)

    def test_inputs_changed_while_staged_identities_are_computed_are_not_applied(self):
        from pandrator.web.models import SessionRecord

        def change_the_session(_revision_id):
            with self.database.immediate_session() as session:
                session.get(SessionRecord, self.record.id).revision += 1

        result, calls = self._generate_with_identity_probe(change_the_session)

        self.assertEqual(0, result.get("regrouped_groups"))
        # The three first-pass takes only: the staged group is never generated.
        self.assertEqual(3, calls)
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual(
            [("not_applied", "selection_changed")],
            [
                (item["repair_status"], item["repair_reason"])
                for item in self.regroup_attempts()
                if item["reason"] == "passage_regroup"
            ],
        )

    def test_active_revision_changed_while_staged_identities_are_computed_is_not_generated(self):
        def activate_another_revision(revision_id):
            with self.database.immediate_session() as session:
                plan = session.scalar(
                    select(GenerationPlan).where(GenerationPlan.session_id == self.record.id)
                )
                plan.active_revision_id = revision_id

        result, calls = self._generate_with_identity_probe(activate_another_revision)

        self.assertEqual(0, result.get("regrouped_groups"))
        # The three first-pass takes only: the staged group is never generated.
        self.assertEqual(3, calls)
        self.assertEqual(
            [("not_applied", "selection_changed")],
            [
                (item["repair_status"], item["repair_reason"])
                for item in self.regroup_attempts()
                if item["reason"] == "passage_regroup"
            ],
        )

    def test_distinct_turn_ids_keep_first_pass_segments_separate(self):
        self.plan(turn_ids=["turn-a", "turn-b", "turn-c"])
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == self.revision_id)
                    .order_by(GenerationSegment.ordinal)
                )
            )
            self.assertEqual(
                [
                    (item.speech_block_provenance_json or {}).get("turn_id")
                    for item in segments
                ],
                ["turn-a", "turn-b", "turn-c"],
            )

        result, calls = self.generate()

        self.assertEqual(0, result.get("regrouped_groups"))
        self.assertEqual(3, calls)
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual([FIRST, SECOND, THIRD], self.active_texts())

    def test_duration_misfit_retains_originals(self):
        self.plan()

        def synth(text, *_args, **_kwargs):
            if text == f"{FIRST} {SECOND} {THIRD}":
                return Sine(440).to_audio_segment(duration=8600)
            return Sine(440).to_audio_segment(duration=3000)

        result, calls = self.generate(synth)
        self.assertEqual(0, result.get("regrouped_groups"))
        self.assertEqual(4, calls)
        self.assertEqual(self.revision_id, self.active())
        self.assertEqual([FIRST, SECOND, THIRD], self.active_texts())
        attempts = [
            item
            for item in self.regroup_attempts()
            if item["reason"] == "passage_regroup"
        ]
        self.assertEqual("not_applied", attempts[-1]["repair_status"])
        self.assertEqual("duration_misfit", attempts[-1]["repair_reason"])

    def test_generation_failure_keeps_original_plan(self):
        self.plan()

        def synth(text, *_args, **_kwargs):
            if text == f"{FIRST} {SECOND} {THIRD}":
                raise RuntimeError("simulated regroup synthesis failure")
            return Sine(440).to_audio_segment(duration=3000)

        result, _ = self.generate(synth)
        self.assertEqual("completed", result["status"])
        self.assertEqual(0, result.get("regrouped_groups"))
        self.assertEqual(self.revision_id, self.active())
        attempts = [
            item
            for item in self.regroup_attempts()
            if item["reason"] == "passage_regroup"
        ]
        self.assertEqual("failed", attempts[-1]["repair_status"])

    def test_rejected_only_regroup_history_is_rejected_not_completed(self):
        self.plan()

        def synth(text, *_args, **_kwargs):
            return Sine(440).to_audio_segment(
                duration=8600 if text == f"{FIRST} {SECOND} {THIRD}" else 3000
            )

        result, _ = self.generate(synth)
        self.assertEqual(0, result.get("regrouped_groups"))
        self.assertEqual("completed", result.get("regroup_status"))
        job = self.handlers.jobs.enqueue(
            "generation.run",
            {"generation_run_id": self.run_id, "operation": "generate"},
            session_id=self.record.id,
        )
        with self.database.immediate_session() as session:
            session.get(GenerationRun, self.run_id).job_id = job.id
        claimed = self.handlers.jobs.claim("regroup-history-fixture")
        self.assertEqual(job.id, claimed.id)
        self.handlers.jobs.complete(
            job.id,
            "regroup-history-fixture",
            result,
            lease_generation=claimed.lease_generation,
        )

        listed = next(
            item
            for item in GenerationHistoryReader(self.database).list_runs(
                self.record.id
            )
            if item["id"] == self.run_id
        )
        repair = listed["timing_repair"]
        self.assertEqual("regroup", repair["kind"])
        self.assertEqual("rejected", repair["status"])
        self.assertEqual(0, repair["applied_count"])
        self.assertEqual(1, repair["attempt_count"])
        self.assertFalse(repair["no_effect"])

    def test_cancel_during_regroup_keeps_original_selected(self):
        self.plan()
        event = threading.Event()

        def synth(text, *_args, **_kwargs):
            if text == f"{FIRST} {SECOND} {THIRD}":
                event.set()
            return Sine(440).to_audio_segment(duration=3000)

        result, _ = self.generate(synth, event)
        self.assertEqual(0, result.get("regrouped_groups"))
        # The source generation still succeeds, but the second pass stopped
        # before examining every selected group and must say so.
        self.assertEqual("completed", result.get("regroup_status"))
        self.assertFalse(result.get("regroup_completed"))
        self.assertEqual(self.revision_id, self.active())

    def completed_receipt_history(self, result):
        """Attach ``result`` as the root generation job receipt and read history."""

        job = self.handlers.jobs.enqueue(
            "generation.run",
            {"generation_run_id": self.run_id, "operation": "generate"},
            session_id=self.record.id,
        )
        with self.database.immediate_session() as session:
            session.get(GenerationRun, self.run_id).job_id = job.id
        claimed = self.handlers.jobs.claim("regroup-history-fixture")
        self.assertEqual(job.id, claimed.id)
        self.handlers.jobs.complete(
            job.id,
            "regroup-history-fixture",
            result,
            lease_generation=claimed.lease_generation,
        )
        return next(
            item
            for item in GenerationHistoryReader(self.database).list_runs(
                self.record.id
            )
            if item["id"] == self.run_id
        )

    def test_interrupted_regroup_after_no_merged_group_is_not_completed(self):
        self.plan()
        event = threading.Event()

        def synth(text, *_args, **_kwargs):
            if text == f"{FIRST} {SECOND} {THIRD}":
                event.set()
            return Sine(440).to_audio_segment(duration=3000)

        result, _ = self.generate(synth, event)
        listed = self.completed_receipt_history(result)
        repair = listed["timing_repair"]
        self.assertEqual("regroup", repair["kind"])
        self.assertNotEqual("completed", repair["status"])
        self.assertEqual("stopped", repair["status"])
        self.assertFalse(repair["pass_completed"])

    def test_interrupted_regroup_after_applied_group_is_partial(self):
        from pandrator.web import voiceover_regroup as regroup_module

        self.plan(enabled=False, passages=5)
        _result, calls = self.generate()
        self.assertEqual(5, calls)
        self.assertEqual(self.revision_id, self.active())

        event = threading.Event()
        original_mark = regroup_module.mark_output_assemblies_stale

        def mark_then_stop(*args, **kwargs):
            # Simulate a stop that arrives only after the first group has been
            # accepted: the next iteration must not stage an unexamined group.
            event.set()
            return original_mark(*args, **kwargs)

        def synth(text, *_args, **_kwargs):
            return Sine(440).to_audio_segment(
                duration=9200 if text == f"{FIRST} {SECOND} {THIRD}" else 3000
            )

        with patch(
            "pandrator.logic.tts_handler.text_to_audio", side_effect=synth
        ), patch.object(
            regroup_module,
            "mark_output_assemblies_stale",
            side_effect=mark_then_stop,
        ):
            result = regroup_module.repair_regroup_blocks(
                self.handlers, self.run_id, lambda *_args: None, event
            )

        self.assertEqual(1, result["regrouped_groups"])
        self.assertEqual(2, result["regroup_attempted_groups"])
        self.assertFalse(result["regroup_completed"])
        self.assertEqual(
            [f"{FIRST} {SECOND} {THIRD}", FOURTH, FIFTH], self.active_texts()
        )
        listed = self.completed_receipt_history(result)
        repair = listed["timing_repair"]
        self.assertEqual("regroup", repair["kind"])
        self.assertEqual("partial", repair["status"])
        self.assertFalse(repair["pass_completed"])
        self.assertEqual(1, repair["applied_count"])

    def test_take_selection_during_regroup_is_not_overwritten(self):
        self.plan()
        alternate_id = None

        def synth(text, *_args, **_kwargs):
            nonlocal alternate_id
            if text == f"{FIRST} {SECOND} {THIRD}":
                with self.database.session() as session:
                    segment = session.get(GenerationSegment, self.segment_ids[-1])
                    expected_revision = segment.revision
                    original = session.scalar(
                        select(AudioTake).where(
                            AudioTake.generation_segment_id == self.segment_ids[-1],
                            AudioTake.is_active.is_(True),
                        )
                    )
                    alternate = AudioTake(
                        generation_segment_id=self.segment_ids[-1],
                        artifact_id=original.artifact_id,
                        settings_hash=original.settings_hash,
                        duration_ms=original.duration_ms,
                        status="completed",
                    )
                    session.add(alternate)
                    session.flush()
                    alternate_id = alternate.id
                service = GenerationService(
                    self.database,
                    self.handlers.jobs,
                    WorkspaceSettingsService(self.database),
                )
                service.select_take(
                    self.segment_ids[-1], alternate_id, expected_revision
                )
            return Sine(440).to_audio_segment(
                duration=9200 if text == f"{FIRST} {SECOND} {THIRD}" else 3000
            )

        result, _ = self.generate(synth)
        self.assertEqual(0, result.get("regrouped_groups"))
        self.assertEqual(self.revision_id, self.active())
        attempts = [
            item
            for item in self.regroup_attempts()
            if item["reason"] == "passage_regroup"
        ]
        self.assertEqual("not_applied", attempts[-1]["repair_status"])
        self.assertEqual("selection_changed", attempts[-1]["repair_reason"])
        with self.database.session() as session:
            selected = list(
                session.scalars(
                    select(AudioTake.id).where(
                        AudioTake.generation_segment_id == self.segment_ids[-1],
                        AudioTake.is_active.is_(True),
                    )
                )
            )
        self.assertEqual([alternate_id], selected)

    def test_replay_does_not_stage_duplicate_revisions(self):
        import threading

        from pandrator.web.voiceover_regroup import repair_regroup_blocks

        self.plan()

        def synth(text, *_args, **_kwargs):
            if text == f"{FIRST} {SECOND} {THIRD}":
                return Sine(440).to_audio_segment(duration=8600)
            return Sine(440).to_audio_segment(duration=3000)

        result, _ = self.generate(synth)
        self.assertEqual(0, result.get("regrouped_groups"))
        # The rejected attempt left the original plan active but staged one
        # child revision: replaying the same run must not stage another.
        self.assertEqual(self.revision_id, self.active())

        def count_revisions():
            with self.database.session() as session:
                plan = session.scalar(
                    select(GenerationPlan).where(
                        GenerationPlan.session_id == self.record.id
                    )
                )
                return session.scalar(
                    select(func.count())
                    .select_from(GenerationPlanRevision)
                    .where(GenerationPlanRevision.plan_id == plan.id)
                )

        before = count_revisions()
        repeat = repair_regroup_blocks(
            self.handlers, self.run_id, lambda *_args: None, threading.Event()
        )
        self.assertEqual(0, repeat.get("regrouped_groups"))
        self.assertEqual("already_attempted", repeat.get("regroup_status"))
        self.assertEqual(before, count_revisions())
        # The original passages and their takes are untouched by the replay.
        self.assertEqual([FIRST, SECOND, THIRD], self.active_texts())

    def test_disabled_regroup_generates_each_passage_once(self):
        self.plan(enabled=False)
        result, calls = self.generate()
        self.assertEqual(3, calls)
        self.assertEqual(self.revision_id, self.active())
        self.assertNotIn("regrouped_groups", result)

    def test_legacy_mode_ignores_regroup_flag(self):
        self.plan(enabled=True, mode="legacy")
        result, calls = self.generate()
        self.assertNotIn("regrouped_groups", result)
        self.assertEqual(self.revision_id, self.active())

    def test_passage_mode_checks_early_repair_when_enabled(self):
        self.plan(enabled=False)
        with self.database.session() as session:
            run = session.get(GenerationRun, self.run_id)
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {
                    **run.settings_snapshot_json["tts"],
                    "speech_block_early_repair_enabled": True,
                },
            }
        result, calls = self.generate()
        self.assertEqual(3, calls)
        self.assertEqual(0, result["repaired_blocks"])
        self.assertEqual(3, result["repair_checked_blocks"])
        self.assertEqual(0, result["repair_candidate_blocks"])
        self.assertEqual(self.revision_id, self.active())


if __name__ == "__main__":
    unittest.main()
