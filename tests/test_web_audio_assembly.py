import tempfile
import threading
import unittest
import json
import subprocess
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from PIL import Image
from pydub import AudioSegment
from pydub.generators import Sine
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.logic.audio_processor import _save_metadata_and_cover
from pandrator.web.artifacts import ArtifactService
from pandrator.web.audio_assembly import compose_audio, export_audio
from pandrator.web.database import Database, upgrade_database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, ArtifactEdge, AudioTake, Document, DocumentRevision, GenerationRun, GenerationSegment, OutputAssembly, Segment, SessionRecord
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workspace import GenerationService, WorkspaceSettingsService


class AudioCompositionTests(unittest.TestCase):
    def test_composition_applies_inter_segment_silence_without_trailing_padding(self):
        first = Sine(440).to_audio_segment(duration=100)
        second = Sine(660).to_audio_segment(duration=150)
        result = compose_audio([(first, 200), (second, 999)], {"fade_enabled": False})
        self.assertEqual(450, len(result))
        self.assertLess(result[120:280].max, first.max // 20)

    def test_composition_applies_bounded_fades(self):
        tone = Sine(440).to_audio_segment(duration=100)
        result = compose_audio([(tone, 0)], {"fade_enabled": True, "fade_in_ms": 30, "fade_out_ms": 30})
        self.assertEqual(100, len(result))
        self.assertLess(result[:5].max, result[40:60].max)
        self.assertLess(result[-5:].max, result[40:60].max)

    def test_wav_export_is_pcm_and_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "assembled.wav"
            export_audio(Sine(440).to_audio_segment(duration=120), destination, "wav")
            decoded = AudioSegment.from_file(destination)
            self.assertEqual(120, len(decoded))
            self.assertEqual(2, decoded.sample_width)

    def test_supported_tagged_containers_receive_metadata_and_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cover = root / "cover.png"
            Image.new("RGB", (64, 64), color=(92, 52, 35)).save(cover)
            metadata = {
                "title": "Container title",
                "artist": "Narrator",
                "album": "Container album",
                "genre": "Audiobook",
                "language": "en",
            }
            for output_format, reader in (("m4b", MP4), ("opus", OggOpus), ("flac", FLAC)):
                with self.subTest(output_format=output_format):
                    destination = root / f"tagged.{output_format}"
                    export_audio(Sine(440).to_audio_segment(duration=120), destination, output_format, "128k")
                    self.assertTrue(
                        _save_metadata_and_cover(
                            str(destination),
                            output_format,
                            metadata,
                            str(cover),
                            raise_on_error=True,
                        )
                    )
                    tags = reader(destination)
                    if output_format == "m4b":
                        self.assertEqual(["Container title"], tags["\xa9nam"])
                        self.assertTrue(tags["covr"])
                    else:
                        self.assertEqual(["Container title"], tags["title"])
                        if output_format == "opus":
                            self.assertTrue(tags["metadata_block_picture"])
                        else:
                            self.assertTrue(tags.pictures)


class DurableOutputAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = DataPaths.from_value(self.temporary.name).ensure()
        upgrade_database(self.paths.database)
        self.database = Database(self.paths.database)
        self.jobs = JobQueue(self.database)
        self.settings = WorkspaceSettingsService(self.database)
        self.generation = GenerationService(self.database, self.jobs, self.settings)
        self.record = SessionService(self.database).create("Assembly", workflow_kind="audiobook")
        self.session_dir = self.paths.sessions / self.record.storage_key
        self.session_dir.mkdir(parents=True)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _plan_with_takes(self):
        plan = self.generation.create_plan(
            self.record.id,
            source_revision_id=None,
            segments=[
                {"text": "First", "node_kind": "chapter_marker", "silence_after_ms": 180},
                {"text": "Second", "silence_after_ms": 900},
            ],
        )
        artifacts = ArtifactService(self.database, self.paths)
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == plan["active_revision_id"])
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            segment_ids = [segment.id for segment in segments]
        for index, (segment_id, duration) in enumerate(zip(segment_ids, (100, 140))):
            path = self.session_dir / f"take-{index}.wav"
            Sine(440 + index * 110).to_audio_segment(duration=duration).export(path, format="wav").close()
            artifact = artifacts.register(path, kind="audio", role="generation_take", session_id=self.record.id)
            with self.database.session() as session:
                segment = session.get(GenerationSegment, segment_id)
                segment.status = "completed"
                session.add(
                    AudioTake(
                        generation_segment_id=segment_id,
                        artifact_id=artifact.id,
                        kind="tts",
                        status="completed",
                        duration_ms=duration,
                        is_active=True,
                    )
                )
        return segment_ids

    def test_m4b_assembly_preserves_generation_chapters(self):
        self._plan_with_takes()
        current = self.settings.get(self.record.id, "output")
        self.settings.update(
            self.record.id,
            "output",
            current["revision"],
            {"format": "m4b", "bitrate": "128k", "title": "Chaptered book"},
        )
        queued = self.generation.create_assembly(self.record.id)
        result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": queued["id"]},
            lambda *_args: None,
            threading.Event(),
        )
        artifact, output_path = ArtifactService(self.database, self.paths).resolve(result["artifact_id"])
        self.assertEqual([{"start_ms": 0, "title": "First"}], artifact.metadata_json["chapters"])
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(output_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        chapters = json.loads(probe.stdout)["chapters"]
        self.assertEqual("First", chapters[0]["tags"]["title"])
        self.assertAlmostEqual(0.42, float(chapters[0]["end_time"]), places=2)

    def test_selected_takes_are_assembled_and_upstream_edits_mark_output_stale(self):
        segment_ids = self._plan_with_takes()
        queued = self.generation.create_assembly(self.record.id)
        result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": queued["id"]},
            lambda *_args: None,
            threading.Event(),
        )
        self.assertEqual(420, result["duration_ms"])
        latest = self.generation.latest_assembly(self.record.id)
        self.assertEqual("completed", latest["status"])
        artifact, path = ArtifactService(self.database, self.paths).resolve(latest["artifact_id"])
        self.assertEqual(420, len(AudioSegment.from_file(path)))
        self.assertEqual("assembled_audio", artifact.role)

        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_ids[0])
            revision = segment.revision
        self.generation.update_segment(segment_ids[0], revision, {"silence_after_ms": 250})
        self.assertEqual("stale", self.generation.latest_assembly(self.record.id)["status"])
        with self.database.session() as session:
            self.assertEqual("stale", session.get(Artifact, artifact.id).state)

    def test_subtitle_generation_assembly_uses_source_timestamps_without_added_pauses(self):
        with self.database.session() as session:
            document = Document(session_id=self.record.id, stage="translation", language="pl")
            session.add(document)
            session.flush()
            revision = DocumentRevision(document_id=document.id, revision_number=1, content_hash="timed")
            session.add(revision)
            session.flush()
            session.add_all(
                [
                    Segment(revision_id=revision.id, ordinal=0, start_ms=500, end_ms=1000, text="Pierwszy."),
                    Segment(revision_id=revision.id, ordinal=1, start_ms=2000, end_ms=2500, text="Drugi."),
                ]
            )
            document.active_revision_id = revision.id
            revision_id = revision.id
        plan = self.generation.create_plan(
            self.record.id,
            source_revision_id=revision_id,
            segments=[
                {"text": "Pierwszy.", "node_kind": "subtitle_cue", "source_segment_ids": [1], "silence_after_ms": 0},
                {"text": "Drugi.", "node_kind": "subtitle_cue", "source_segment_ids": [2], "silence_after_ms": 0},
            ],
        )
        artifacts = ArtifactService(self.database, self.paths)
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == plan["active_revision_id"])
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            segment_ids = [segment.id for segment in segments]
        for index, (segment_id, duration) in enumerate(zip(segment_ids, (100, 150))):
            path = self.session_dir / f"subtitle-take-{index}.wav"
            Sine(440 + index * 110).to_audio_segment(duration=duration).export(path, format="wav").close()
            artifact = artifacts.register(path, kind="audio", role="generation_take", session_id=self.record.id)
            with self.database.session() as session:
                segment = session.get(GenerationSegment, segment_id)
                segment.status = "completed"
                session.add(AudioTake(generation_segment_id=segment_id, artifact_id=artifact.id, kind="tts", status="completed", duration_ms=duration, is_active=True))

        queued = self.generation.create_assembly(self.record.id)
        result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": queued["id"]}, lambda *_args: None, threading.Event()
        )

        self.assertEqual(2500, result["duration_ms"])
        artifact, output_path = artifacts.resolve(result["artifact_id"])
        self.assertEqual(2500, len(AudioSegment.from_file(output_path)))
        self.assertEqual([500, 2000], [item["target_start_ms"] for item in artifact.metadata_json["takes"]])
        self.assertTrue(all(item["silence_after_ms"] == 0 for item in artifact.metadata_json["takes"]))

    def test_selected_run_assembles_its_cumulative_take_snapshot(self):
        segment_ids = self._plan_with_takes()
        with self.database.session() as session:
            segments = list(session.scalars(select(GenerationSegment).order_by(GenerationSegment.ordinal)).all())
            first_run = GenerationRun(
                session_id=self.record.id,
                plan_revision_id=segments[0].plan_revision_id,
                sequence_number=1,
                status="completed",
                settings_snapshot_json={"tts": {"service": "Kokoro", "voice": "Ada"}},
            )
            second_run = GenerationRun(
                session_id=self.record.id,
                plan_revision_id=segments[0].plan_revision_id,
                sequence_number=2,
                status="completed",
                settings_snapshot_json={"tts": {"service": "Kokoro", "voice": "Bob"}},
            )
            session.add_all([first_run, second_run])
            session.flush()
            for take in session.scalars(select(AudioTake).order_by(AudioTake.created_at)).all():
                take.generation_run_id = first_run.id
            first_run_id = first_run.id
            second_run_id = second_run.id

        replacement_path = self.session_dir / "take-replacement.wav"
        Sine(880).to_audio_segment(duration=200).export(replacement_path, format="wav").close()
        replacement_artifact = ArtifactService(self.database, self.paths).register(
            replacement_path,
            kind="audio",
            role="generation_take",
            session_id=self.record.id,
        )
        with self.database.session() as session:
            for take in session.scalars(select(AudioTake).where(AudioTake.generation_segment_id == segment_ids[0])).all():
                take.is_active = False
            session.add(
                AudioTake(
                    generation_segment_id=segment_ids[0],
                    generation_run_id=second_run_id,
                    artifact_id=replacement_artifact.id,
                    kind="tts",
                    status="completed",
                    duration_ms=200,
                    is_active=True,
                )
            )

        first_assembly = self.generation.create_assembly(self.record.id, generation_run_id=first_run_id)
        first_result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": first_assembly["id"]}, lambda *_args: None, threading.Event()
        )
        second_assembly = self.generation.create_assembly(self.record.id, generation_run_id=second_run_id)
        second_result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": second_assembly["id"]}, lambda *_args: None, threading.Event()
        )

        self.assertEqual(420, first_result["duration_ms"])
        self.assertEqual(520, second_result["duration_ms"])

    def test_chapter_edit_stales_only_the_assembly_and_preserves_the_audio_take(self):
        segment_ids = self._plan_with_takes()
        queued = self.generation.create_assembly(self.record.id)
        WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": queued["id"]},
            lambda *_args: None,
            threading.Event(),
        )
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_ids[1])
            revision = segment.revision
            take = session.scalar(select(AudioTake).where(AudioTake.generation_segment_id == segment.id))
            take_id = take.id

        updated = self.generation.update_segment(segment_ids[1], revision, {"node_kind": "chapter_marker"})

        self.assertEqual("completed", updated["status"])
        self.assertEqual("stale", self.generation.latest_assembly(self.record.id)["status"])
        with self.database.session() as session:
            self.assertEqual("completed", session.get(AudioTake, take_id).status)

    def test_stale_selected_take_is_rejected_and_failure_is_persisted(self):
        segment_ids = self._plan_with_takes()
        with self.database.session() as session:
            take = session.scalar(select(AudioTake).where(AudioTake.generation_segment_id == segment_ids[0]))
            take.status = "stale"
        queued = self.generation.create_assembly(self.record.id)
        with self.assertRaisesRegex(ValueError, "no current completed audio take"):
            WorkflowHandlers(self.database, self.paths).assemble_generation_output(
                {"output_assembly_id": queued["id"]},
                lambda *_args: None,
                threading.Event(),
            )
        with self.database.session() as session:
            assembly = session.get(OutputAssembly, queued["id"])
            self.assertEqual("failed", assembly.status)
            self.assertIn("Segment 1", assembly.error_message)

    def test_mp3_assembly_embeds_metadata_and_cover(self):
        self._plan_with_takes()
        cover_path = self.session_dir / "cover.png"
        Image.new("RGB", (64, 64), color=(92, 52, 35)).save(cover_path)
        cover = ArtifactService(self.database, self.paths).register(
            cover_path,
            kind="image",
            role="cover",
            session_id=self.record.id,
        )
        current = self.settings.get(self.record.id, "output")
        self.settings.update(
            self.record.id,
            "output",
            current["revision"],
            {
                "format": "mp3",
                "bitrate": "128k",
                "title": "Assembly title",
                "artist": "Narrator",
                "album": "Assembly album",
                "genre": "Audiobook",
                "language": "en",
                "cover_artifact_id": cover.id,
            },
        )
        queued = self.generation.create_assembly(self.record.id)
        result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": queued["id"]},
            lambda *_args: None,
            threading.Event(),
        )
        artifact, output_path = ArtifactService(self.database, self.paths).resolve(result["artifact_id"])
        tags = ID3(output_path)
        self.assertEqual("Assembly title", str(tags["TIT2"]))
        self.assertEqual("Narrator", str(tags["TPE1"]))
        self.assertEqual("en", str(tags["TLAN"]))
        self.assertTrue(tags.getall("APIC"))
        with self.database.session() as session:
            edge = session.get(ArtifactEdge, (cover.id, artifact.id))
            self.assertIsNotNone(edge)

    def test_voiceover_assembly_ignores_audiobook_metadata_and_cover_overrides(self):
        self._plan_with_takes()
        with self.database.session() as session:
            session.get(SessionRecord, self.record.id).workflow_kind = "voiceover"
        cover_path = self.session_dir / "irrelevant-cover.png"
        Image.new("RGB", (64, 64), color=(92, 52, 35)).save(cover_path)
        cover = ArtifactService(self.database, self.paths).register(
            cover_path,
            kind="image",
            role="cover",
            session_id=self.record.id,
        )
        current = self.settings.get(self.record.id, "output")
        self.settings.update(
            self.record.id,
            "output",
            current["revision"],
            {
                "format": "mp3",
                "title": "Must not be embedded",
                "album": "Not an audiobook",
                "genre": "Audiobook",
                "cover_artifact_id": cover.id,
            },
        )

        queued = self.generation.create_assembly(self.record.id)
        result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": queued["id"]},
            lambda *_args: None,
            threading.Event(),
        )
        artifact, output_path = ArtifactService(self.database, self.paths).resolve(result["artifact_id"])

        self.assertEqual({}, artifact.metadata_json["metadata"])
        self.assertIsNone(artifact.metadata_json["cover_artifact_id"])
        self.assertNotIn("TIT2", ID3(output_path))
        with self.database.session() as session:
            self.assertIsNone(session.get(ArtifactEdge, (cover.id, artifact.id)))

    def test_cancellation_is_persisted_on_the_assembly(self):
        self._plan_with_takes()
        queued = self.generation.create_assembly(self.record.id)
        canceled = threading.Event()
        canceled.set()
        result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": queued["id"]},
            lambda *_args: None,
            canceled,
        )
        self.assertEqual({}, result)
        with self.database.session() as session:
            self.assertEqual("canceled", session.get(OutputAssembly, queued["id"]).status)


if __name__ == "__main__":
    unittest.main()
