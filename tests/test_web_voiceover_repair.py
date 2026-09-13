import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from pydub.generators import Sine
from sqlalchemy import select

from pandrator.logic.dubbing.audio_sync import align_audio_blocks
from pandrator.logic.dubbing.models import AudioAlignmentBlock
from pandrator.web.voiceover_repair import _load_groups, advance_timing

from pandrator.web.database import Database
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
                plan_revision_id=self.revision_id,
                status="queued",
                settings_snapshot_json={
                    "tts": {
                        "service": "XTTS",
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

    def test_replacement_overrun_does_not_activate_new_plan(self):
        self.plan()

        def synth(text, *_args, **_kwargs):
            is_child = text in (self.first, self.second)
            return Sine(440).to_audio_segment(duration=16000 if is_child else 2000)

        result, calls = self.generate(synth)
        self.assertEqual(0, result.get("repaired_blocks"))
        self.assertEqual(4, calls)
        self.assertEqual(self.revision_id, self.active())

    def test_failure_keeps_original_plan_and_successful_original_run(self):
        self.plan()

        def synth(text, *_args, **_kwargs):
            if text == self.first:
                raise RuntimeError("simulated repair synthesis failure")
            return Sine(440).to_audio_segment(duration=2000)

        result, _ = self.generate(synth)
        self.assertEqual("completed", result["status"])
        self.assertEqual(self.revision_id, self.active())

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

    def test_multiple_original_blocks_are_repaired_without_recursing_on_children(self):
        self.plan(groups=2)
        result, calls = self.generate()
        self.assertEqual(2, result.get("repaired_blocks"))
        self.assertEqual(7, calls)

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
                        group, cursor, end, settings, root, threading.Event()
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


if __name__ == "__main__":
    unittest.main()
