import tempfile
import threading
import unittest
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
from pandrator.web.models import Artifact, ArtifactEdge, AudioTake, GenerationSegment, OutputAssembly
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
                {"text": "First", "silence_after_ms": 180},
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
