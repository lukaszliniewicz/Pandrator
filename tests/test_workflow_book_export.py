"""Disposable PCM fixtures for frozen book-export contracts."""

import json
import tempfile
import threading
import unittest
import wave
from unittest import mock

from sqlalchemy import event, select

from pandrator.logic.book_timing import AlignedBookText, BookWord, alignment_identity
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.export_inputs import ExportInputs
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, AudioTake, GenerationRun, GenerationSegment
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_book_export import export_book
from pandrator.web.workflow_output_context import OutputWorkflowContext
from pandrator.web.workspace import GenerationService, WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root


class FrozenBookExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.record = SessionService(self.database).create("Frozen Book", workflow_kind="audiobook")
        self.session_dir = self.paths.sessions / self.record.storage_key
        self.session_dir.mkdir(parents=True)
        self.context = OutputWorkflowContext(
            self.database,
            self.paths,
            self.artifacts,
            self.artifacts.resolve,
            lambda _id: self.session_dir,
            lambda _id: self.record,
            lambda _id, operation: self.session_dir / "operations" / operation,
        )
        generation = GenerationService(
            self.database, JobQueue(self.database), WorkspaceSettingsService(self.database)
        )
        plan = generation.create_plan(
            self.record.id,
            source_revision_id=None,
            segments=[{"text": "Call 2."}, {"text": "Done now."}],
        )
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == plan["active_revision_id"])
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
        self.rate = 44100
        self.take_frames = [44101, 35279]
        gap = 11025
        self.manifest = []
        self.take_artifacts = []
        offset = 0
        for index, segment in enumerate(segments):
            path = self.session_dir / f"take-{index}.wav"
            self.write_wav(path, self.take_frames[index])
            artifact = self.artifacts.register(
                path,
                kind="audio",
                role="generation_take",
                session_id=self.record.id,
                metadata={
                    "source_text": segment.text,
                    "synthesized_text": ["Call two.", "Done now."][index],
                },
            )
            self.take_artifacts.append(artifact)
            with self.database.session() as session:
                take = AudioTake(
                    generation_segment_id=segment.id,
                    artifact_id=artifact.id,
                    status="completed",
                    kind="tts",
                    revision=1,
                    is_active=False,
                    duration_ms=round(self.take_frames[index] * 1000 / self.rate),
                )
                session.add(take)
                session.flush()
                self.manifest.append(
                    {
                        "take_id": take.id,
                        "segment_id": segment.id,
                        "take_revision": 1,
                        "segment_revision": segment.revision,
                        "artifact_id": artifact.id,
                        "kind": "tts",
                        "start_frame": offset,
                        "end_frame": offset + self.take_frames[index],
                        "node_kind": "paragraph",
                        "speaker": None,
                        "duration_ms": take.duration_ms,
                        "silence_after_ms": 250 if index == 0 else 0,
                    }
                )
            offset += self.take_frames[index] + (gap if index == 0 else 0)
        self.audio_path = self.session_dir / "assembly.wav"
        self.write_wav(self.audio_path, offset)
        self.audio = self.artifacts.register(
            self.audio_path,
            kind="audio",
            role="assembled_audio",
            session_id=self.record.id,
            metadata={
                "takes": self.manifest,
                "audio_timeline": {
                    "version": 1,
                    "sample_rate_hz": self.rate,
                    "total_frames": offset,
                },
                "chapters": [
                    {"start_ms": 0, "title": "First"},
                    {
                        "start_ms": round(self.manifest[1]["start_frame"] * 1000 / self.rate),
                        "title": "Second",
                    },
                ],
            },
        )
        self.settings = {
            "export_mode": "subtitles",
            "book_style": "reading",
            "book_cue_mode": "passages",
            "book_text_mode": "auto",
            "book_use_native_timings": True,
            "subtitle_format": "srt",
        }
        self.cancel = threading.Event()

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def write_wav(self, path, frames):
        with wave.open(str(path), "wb") as destination:
            destination.setframerate(self.rate)
            destination.setnchannels(1)
            destination.setsampwidth(2)
            destination.writeframes(b"\x01\x00" * frames)

    def inputs(self, **overrides):
        settings = {**self.settings, **overrides}
        return ExportInputs(
            self.record.id,
            settings,
            self.record,
            {"version": 1, "sections": {"output": settings}},
            None,
            [],
            {},
            self.audio,
            (self.audio,),
            None,
            None,
        )

    def alignment(self, path, text, settings, work_dir, cancel_event):
        with wave.open(str(path)) as source:
            duration = round(source.getnframes() * 1000 / source.getframerate())
        first_end = text.index(" ")
        return AlignedBookText(
            text,
            (
                BookWord(text[:first_end], 0, duration // 2, 0, first_end),
                BookWord(text[first_end + 1 :], duration // 2, duration, first_end + 1, len(text)),
            ),
            "fixture",
            {"duration_ms": duration},
        )

    def run_export(self, payload=None, progress=None, **overrides):
        with mock.patch(
            "pandrator.web.workflow_book_export.align_book_text", side_effect=self.alignment
        ) as align:
            result = export_book(
                self.context,
                self.inputs(**overrides),
                self.audio,
                payload or {},
                progress or (lambda *_: None),
                self.cancel,
            )
        return result, align

    def inventory(self, role):
        with self.database.session() as session:
            return list(session.scalars(select(Artifact).where(Artifact.role == role)).all())

    def cues(self, result):
        artifact, _path = self.artifacts.resolve(result["artifact_ids"][0])
        _cue, path = self.artifacts.resolve(artifact.metadata_json["cue_artifact_id"])
        return json.loads(path.read_text())["cues"]

    def test_exact_frames_silence_chapters_and_frozen_mapping(self):
        result, align = self.run_export()
        self.assertEqual(2, align.call_count)
        cues = self.cues(result)
        self.assertEqual("Call 2.", cues[0]["text"])
        self.assertEqual(1250, cues[1]["start_ms"])
        self.assertEqual(2050, cues[-1]["end_ms"])
        self.assertEqual("Second", cues[1]["heading"])
        self.assertEqual(
            "anchored_lexical_replacements",
            result["book_timing_diagnostics"]["takes"][0]["mapping_method"],
        )
        self.assertEqual([], result["subtitle_diagnostics"])
        self.assertFalse((self.session_dir / "exports" / "audio").exists())

    def test_nonactive_pinned_takes_survive_current_segment_edits(self):
        with self.database.session() as session:
            row = session.get(GenerationSegment, self.manifest[0]["segment_id"])
            row.text = "A completely different active segment."
            row.revision += 1
        result, _align = self.run_export(book_cue_mode="segments")
        self.assertEqual("Call 2.", self.cues(result)[0]["text"])

    def test_inherited_take_from_earlier_run_is_valid(self):
        with self.database.session() as session:
            segment = session.get(GenerationSegment, self.manifest[0]["segment_id"])
            earlier = GenerationRun(
                session_id=self.record.id,
                plan_revision_id=segment.plan_revision_id,
                sequence_number=1,
                status="completed",
            )
            selected = GenerationRun(
                session_id=self.record.id,
                plan_revision_id=segment.plan_revision_id,
                sequence_number=2,
                status="completed",
            )
            session.add_all([earlier, selected])
            session.flush()
            session.get(AudioTake, self.manifest[0]["take_id"]).generation_run_id = earlier.id
            selected_id = selected.id
        result, _align = self.run_export(generation_run_id=selected_id)
        self.assertEqual(1, len(result["artifact_ids"]))

    def test_whole_segment_never_aligns_or_builds_model_identity(self):
        with mock.patch(
            "pandrator.web.workflow_book_export.alignment_identity", wraps=alignment_identity
        ) as identity:
            result, align = self.run_export(book_cue_mode="segments")
        align.assert_not_called()
        identity.assert_not_called()
        self.assertEqual([], self.inventory("speech_timing"))
        self.assertEqual("Call 2.", self.cues(result)[0]["text"])

    def test_visual_style_does_not_invalidate_spoken_timing_cache(self):
        first, _align = self.run_export()
        result, align = self.run_export(book_style="captions")
        align.assert_not_called()
        self.assertEqual(2, result["book_timing_diagnostics"]["cache_hits"])
        self.assertEqual(2, len(self.inventory("speech_timing")))
        self.assertNotEqual(first["artifact_ids"], result["artifact_ids"])

    def test_cache_uses_identity_after_first_alignment_installs_model(self):
        installed = False
        original_alignment = self.alignment

        def installing_alignment(*args):
            nonlocal installed
            installed = True
            return original_alignment(*args)

        def identity(_settings, _text):
            return {"version": 1, "model_file": {"hash": "installed"} if installed else None}

        self.alignment = installing_alignment
        with mock.patch(
            "pandrator.web.workflow_book_export.alignment_identity", side_effect=identity
        ):
            self.run_export()
            result, align = self.run_export()
        align.assert_not_called()
        self.assertEqual(2, result["book_timing_diagnostics"]["cache_hits"])
        self.assertEqual(2, len(self.inventory("speech_timing")))

    def change_original(self, text):
        with self.database.session() as session:
            artifact = session.get(Artifact, self.take_artifacts[0].id)
            artifact.metadata_json = {**artifact.metadata_json, "source_text": text}

    def test_unmappable_auto_has_explicit_warning(self):
        self.change_original("Call absolutely two.")
        result, _align = self.run_export()
        diagnostics = result["book_timing_diagnostics"]
        self.assertEqual(1, diagnostics["original_mapping_fallback_count"])
        self.assertEqual([self.manifest[0]["take_id"]], diagnostics["warnings"][0]["take_ids"])
        self.assertEqual("WARNING", diagnostics["warnings"][0]["severity"])
        self.assertEqual("Call two.", self.cues(result)[0]["text"])

    def test_unmappable_original_fails(self):
        self.change_original("Call absolutely two.")
        with self.assertRaisesRegex(ValueError, "cannot be mapped"):
            self.run_export(book_text_mode="original")
        self.assertEqual([], self.inventory("export"))

    def test_one_to_two_pronunciation_mapping_keeps_atomic_interval(self):
        self.change_original("Call twenty two.")
        with self.database.session() as session:
            row = session.get(Artifact, self.take_artifacts[0].id)
            row.metadata_json = {**row.metadata_json, "synthesized_text": "Call 22."}
        result, _align = self.run_export()
        self.assertEqual("Call twenty two.", self.cues(result)[0]["text"])
        self.assertEqual(
            "anchored_lexical_replacements",
            result["book_timing_diagnostics"]["takes"][0]["mapping_method"],
        )

    def test_preview_aligns_only_intersecting_take_and_rebases_to_zero(self):
        result, align = self.run_export(
            book_preview=True, book_preview_start_seconds=1.3, book_preview_duration_seconds=30
        )
        self.assertEqual(1, align.call_count)
        self.assertEqual("Done now.", align.call_args.args[1])
        self.assertEqual(0, self.cues(result)[0]["start_ms"])
        artifact, _path = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertEqual(750, artifact.metadata_json["duration_ms"])
        self.assertIn("-preview", artifact.relative_path)

    def test_preview_does_not_read_corrupted_nonintersecting_take(self):
        nonintersecting = self.paths.managed_path(self.take_artifacts[0].relative_path)
        nonintersecting.write_bytes(b"corrupt PCM outside the selected preview")
        result, align = self.run_export(
            book_preview=True,
            book_preview_start_seconds=1.3,
            book_preview_duration_seconds=25,
        )
        self.assertEqual(1, align.call_count)
        self.assertEqual("Done now.", align.call_args.args[1])
        self.assertEqual("Done now.", self.cues(result)[0]["text"])
        with self.assertRaisesRegex(ValueError, "hash changed"):
            self.run_export()

    def test_preview_start_outside_audio_fails_before_alignment(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.run_export(book_preview=True, book_preview_start_seconds=9)

    def test_legacy_timeline_and_changed_actual_audio_are_rejected(self):
        original = self.audio.metadata_json
        with self.database.session() as session:
            row = session.get(Artifact, self.audio.id)
            row.metadata_json = {"takes": self.manifest}
        self.audio.metadata_json = {"takes": self.manifest}
        with self.assertRaisesRegex(ValueError, "timeline"):
            self.run_export()
        with self.database.session() as session:
            session.get(Artifact, self.audio.id).metadata_json = original
        self.audio.metadata_json = original
        self.audio_path.write_bytes(self.audio_path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "hash changed"):
            self.run_export()

    def test_changed_take_hash_and_revision_are_rejected(self):
        with self.database.session() as session:
            session.get(AudioTake, self.manifest[0]["take_id"]).revision += 1
        with self.assertRaisesRegex(ValueError, "unavailable or changed"):
            self.run_export()
        with self.database.session() as session:
            session.get(AudioTake, self.manifest[0]["take_id"]).revision -= 1
        self.paths.managed_path(self.take_artifacts[0].relative_path).write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "hash changed"):
            self.run_export()

    def test_cancel_after_alignment_leaves_no_incomplete_cache(self):
        original = self.alignment

        def cancelled_alignment(*args):
            aligned = original(*args)
            self.cancel.set()
            return aligned

        self.alignment = cancelled_alignment
        with self.assertRaises(InterruptedError):
            self.run_export()
        self.assertEqual([], self.inventory("speech_timing"))
        self.assertEqual([], list((self.session_dir / "timings").glob("*.json")))
        self.assertEqual([], list((self.session_dir / "operations").iterdir()))

    def test_stale_job_lease_blocks_final_publication_and_keeps_registered_cache(self):
        with self.assertRaisesRegex(InterruptedError, "lease"):
            self.run_export(payload={"_job_id": "nonexistent", "_lease_generation": 1})
        self.assertEqual([], self.inventory("export"))
        self.assertEqual(2, len(self.inventory("speech_timing")))
        for artifact in self.inventory("speech_timing"):
            self.assertTrue(self.paths.managed_path(artifact.relative_path).exists())
        self.assertEqual([], list((self.session_dir / "exports" / "subtitles").iterdir()))

    def test_late_cancellation_retains_committed_final_file(self):
        def progress(fraction, _detail):
            if fraction == 1:
                self.cancel.set()

        result, _align = self.run_export(progress=progress)
        self.assertTrue(self.cancel.is_set())
        _artifact, path = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertTrue(path.exists())
        self.assertEqual(1, len(self.inventory("export")))

    def test_cancel_between_final_publications_preserves_first_committed_export(self):
        def on_commit(connection):
            exported = connection.execute(
                select(Artifact.id).where(
                    Artifact.kind == "export",
                    Artifact.session_id == self.record.id,
                )
            ).first()
            if exported:
                self.cancel.set()

        def render(_audio, _cues, destination, _settings, **_options):
            destination.write_bytes(b"disposable rendered fixture")

        event.listen(self.database.engine, "commit", on_commit)
        try:
            with mock.patch("pandrator.logic.book_video.render_book_video", side_effect=render):
                with self.assertRaises(InterruptedError):
                    self.run_export(export_mode="video_book")
        finally:
            event.remove(self.database.engine, "commit", on_commit)
        exports = self.inventory("export")
        self.assertEqual(1, len(exports))
        self.assertTrue(exports[0].relative_path.endswith(".srt"))
        self.assertTrue(self.paths.managed_path(exports[0].relative_path).exists())
        self.assertEqual([], list((self.session_dir / "operations").iterdir()))

    def test_native_complete_timing_bypasses_alignment(self):
        for artifact in self.take_artifacts:
            path = self.paths.managed_path(artifact.relative_path)
            timing = self.alignment(
                path, artifact.metadata_json["synthesized_text"], {}, None, self.cancel
            ).as_dict()
            with self.database.session() as session:
                row = session.get(Artifact, artifact.id)
                row.metadata_json = {**row.metadata_json, "speech_timing": {"version": 1, **timing}}
        result, align = self.run_export()
        align.assert_not_called()
        self.assertEqual(2, result["book_timing_diagnostics"]["native"])
        result, align = self.run_export(book_style="captions")
        align.assert_not_called()
        self.assertEqual(2, result["book_timing_diagnostics"]["cache_hits"])

    def test_native_render_parts_cover_full_transcript_and_exact_final_pcm(self):
        artifact = self.take_artifacts[0]
        text = artifact.metadata_json["synthesized_text"]
        first_frames = 22050
        parts = []
        for left, right, start, end in [
            (0, 4, 0, first_frames),
            (5, len(text), first_frames, self.take_frames[0]),
        ]:
            duration = round((end - start) * 1000 / self.rate)
            part_text = text[left:right]
            timing = AlignedBookText(
                part_text,
                (BookWord(part_text, 0, duration, 0, len(part_text)),),
                "native",
                {"duration_ms": duration},
            ).as_dict()
            parts.append(
                {
                    "range": [left, right],
                    "start_frame": start,
                    "end_frame": end,
                    "sample_rate_hz": self.rate,
                    "speech_timing": timing,
                }
            )
        with self.database.session() as session:
            row = session.get(Artifact, artifact.id)
            row.metadata_json = {**row.metadata_json, "render_parts": parts}
        result, align = self.run_export()
        self.assertEqual(1, align.call_count)
        self.assertEqual("Done now.", align.call_args.args[1])
        self.assertEqual(1, result["book_timing_diagnostics"]["native"])
        # A missing part invalidates the complete native timing route.
        with self.database.session() as session:
            row = session.get(Artifact, artifact.id)
            row.metadata_json = {**row.metadata_json, "render_parts": parts[1:]}
        result, align = self.run_export()
        self.assertEqual(1, align.call_count)
        self.assertEqual("Call two.", align.call_args.args[1])
        self.assertEqual(0, result["book_timing_diagnostics"]["native"])

    def test_damaged_cache_is_never_reused(self):
        self.run_export()
        cached = self.inventory("speech_timing")[0]
        self.paths.managed_path(cached.relative_path).write_text("{}", encoding="utf-8")
        result, align = self.run_export()
        self.assertEqual(1, align.call_count)
        self.assertEqual(1, result["book_timing_diagnostics"]["cache_hits"])

    def test_mismatched_pcm_timeline_and_missing_frozen_text_fail(self):
        with self.database.session() as session:
            row = session.get(Artifact, self.audio.id)
            timeline = {**row.metadata_json["audio_timeline"], "total_frames": 12}
            row.metadata_json = {**row.metadata_json, "audio_timeline": timeline}
            self.audio.metadata_json = row.metadata_json
        with self.assertRaisesRegex(ValueError, "PCM does not match"):
            self.run_export()
        with self.database.session() as session:
            row = session.get(Artifact, self.audio.id)
            timeline["total_frames"] = sum(self.take_frames) + 11025
            row.metadata_json = {**row.metadata_json, "audio_timeline": timeline}
            self.audio.metadata_json = row.metadata_json
            take_artifact = session.get(Artifact, self.take_artifacts[0].id)
            take_artifact.metadata_json = {"source_text": "Call 2."}
        with self.assertRaisesRegex(ValueError, "frozen source/spoken"):
            self.run_export()

    def test_invalid_native_timing_falls_back_to_known_transcript_alignment(self):
        with self.database.session() as session:
            artifact = session.get(Artifact, self.take_artifacts[0].id)
            artifact.metadata_json = {**artifact.metadata_json, "speech_timing": {"text": "Wrong"}}
        result, align = self.run_export()
        self.assertEqual(2, align.call_count)
        self.assertEqual(0, result["book_timing_diagnostics"]["native"])


if __name__ == "__main__":
    unittest.main()
