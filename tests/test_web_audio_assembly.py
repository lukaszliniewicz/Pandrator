import json
import subprocess
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from PIL import Image
from pydub import AudioSegment
from pydub.generators import Sine
from sqlalchemy import select

from pandrator.logic.audio_processor import _save_metadata_and_cover
from pandrator.web.artifacts import ArtifactService
from pandrator.web.audio_assembly import compose_audio, export_audio
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    AudioTake,
    Document,
    DocumentRevision,
    GenerationRun,
    GenerationSegment,
    OutputAssembly,
    Segment,
    SessionRecord,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workspace import (
    GenerationService,
    RevisionConflict,
    WorkspaceSettingsService,
    output_assembly_settings_hash,
    stable_hash,
)
from tests.web_test_support import prepare_web_test_data_root


class AudioCompositionTests(unittest.TestCase):
    def test_composition_applies_inter_segment_silence_without_trailing_padding(self):
        first = Sine(440).to_audio_segment(duration=100)
        second = Sine(660).to_audio_segment(duration=150)
        result = compose_audio([(first, 200), (second, 999)], {"fade_enabled": False})
        self.assertEqual(450, len(result))
        self.assertLess(result[120:280].max, first.max // 20)

    def test_composition_applies_bounded_fades(self):
        tone = Sine(440).to_audio_segment(duration=100)
        result = compose_audio(
            [(tone, 0)], {"fade_enabled": True, "fade_in_ms": 30, "fade_out_ms": 30}
        )
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
            for output_format, reader in (
                ("m4b", MP4),
                ("opus", OggOpus),
                ("flac", FLAC),
            ):
                with self.subTest(output_format=output_format):
                    destination = root / f"tagged.{output_format}"
                    export_audio(
                        Sine(440).to_audio_segment(duration=120),
                        destination,
                        output_format,
                        "128k",
                    )
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
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.jobs = JobQueue(self.database)
        self.settings = WorkspaceSettingsService(self.database)
        self.generation = GenerationService(self.database, self.jobs, self.settings)
        self.record = SessionService(self.database).create(
            "Assembly", workflow_kind="audiobook"
        )
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
                {
                    "text": "First",
                    "node_kind": "chapter_marker",
                    "silence_after_ms": 180,
                },
                {"text": "Second", "silence_after_ms": 900},
            ],
        )
        artifacts = ArtifactService(self.database, self.paths)
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan["active_revision_id"]
                    )
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            segment_ids = [segment.id for segment in segments]
        for index, (segment_id, duration) in enumerate(zip(segment_ids, (100, 140))):
            path = self.session_dir / f"take-{index}.wav"
            Sine(440 + index * 110).to_audio_segment(duration=duration).export(
                path, format="wav"
            ).close()
            artifact = artifacts.register(
                path, kind="audio", role="generation_take", session_id=self.record.id
            )
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

    def test_export_variant_assembles_completed_run_inline_before_export(self):
        self._plan_with_takes()
        with self.database.session() as session:
            segment = session.scalar(
                select(GenerationSegment).order_by(GenerationSegment.ordinal)
            )
            run = GenerationRun(
                session_id=self.record.id,
                plan_revision_id=segment.plan_revision_id,
                sequence_number=1,
                status="completed",
            )
            session.add(run)
            session.flush()
            run_id = run.id
            for take in session.scalars(select(AudioTake)).all():
                take.generation_run_id = run_id

        snapshot, _settings_hash = self.settings.resolve(
            self.record.id,
            sections=["audio", "output"],
        )
        output_settings = {
            **dict(snapshot.get("output") or {}),
            "generation_run_id": run_id,
            "format": "wav",
        }
        resolved_snapshot = {
            "audio": dict(snapshot.get("audio") or {}),
            "output": output_settings,
        }
        result = WorkflowHandlers(self.database, self.paths).export_variant(
            {
                "session_id": self.record.id,
                "settings": output_settings,
                "resolved_settings_snapshot": resolved_snapshot,
            },
            lambda *_args: None,
            threading.Event(),
        )

        self.assertEqual(1, len(result["artifact_ids"]))
        with self.database.session() as session:
            assembly = session.scalar(
                select(OutputAssembly).where(
                    OutputAssembly.session_id == self.record.id,
                    OutputAssembly.generation_run_id == run_id,
                )
            )
            self.assertIsNotNone(assembly)
            self.assertEqual("completed", assembly.status)
            self.assertIsNone(assembly.job_id)
            exported = session.get(Artifact, result["artifact_ids"][0])
            self.assertEqual("export", exported.role)

    def _legacy_completed_run_assembly(self):
        self._plan_with_takes()
        with self.database.session() as session:
            segment = session.scalar(
                select(GenerationSegment).order_by(GenerationSegment.ordinal)
            )
            run = GenerationRun(
                session_id=self.record.id,
                plan_revision_id=segment.plan_revision_id,
                sequence_number=1,
                status="completed",
            )
            session.add(run)
            session.flush()
            run_id = run.id
            for take in session.scalars(select(AudioTake)).all():
                take.generation_run_id = run_id

        legacy_snapshot, legacy_hash = self.settings.resolve(self.record.id)
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            assembly = OutputAssembly(
                session_id=self.record.id,
                generation_run_id=run_id,
                status="queued",
                settings_json={
                    "resolved": legacy_snapshot,
                    "plan_revision_id": run.plan_revision_id,
                },
                settings_hash=legacy_hash,
            )
            session.add(assembly)
            session.flush()
            assembly_id = assembly.id
        WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": assembly_id},
            lambda *_args: None,
            threading.Event(),
        )
        return run_id, legacy_snapshot, legacy_hash, assembly_id

    def test_direct_export_reuses_legacy_full_snapshot_assembly(self):
        run_id, legacy_snapshot, legacy_hash, assembly_id = (
            self._legacy_completed_run_assembly()
        )
        with self.database.session() as session:
            assembly = session.get(OutputAssembly, assembly_id)
            self.assertEqual(legacy_hash, assembly.settings_hash)
            self.assertEqual(stable_hash(legacy_snapshot), assembly.settings_hash)
            self.assertNotEqual(
                output_assembly_settings_hash(legacy_snapshot),
                assembly.settings_hash,
            )
            assembly_artifact_id = assembly.artifact_id

        result = WorkflowHandlers(self.database, self.paths).export(
            {
                "session_id": self.record.id,
                "settings": {
                    **dict(legacy_snapshot["output"]),
                    "generation_run_id": run_id,
                },
                "resolved_settings_snapshot": legacy_snapshot,
            },
            lambda *_args: None,
            threading.Event(),
        )

        with self.database.session() as session:
            assemblies = list(
                session.scalars(
                    select(OutputAssembly).where(
                        OutputAssembly.session_id == self.record.id,
                        OutputAssembly.generation_run_id == run_id,
                    )
                ).all()
            )
            exported = session.get(Artifact, result["artifact_ids"][0])
            edge = session.scalar(
                select(ArtifactEdge).where(
                    ArtifactEdge.child_artifact_id == exported.id,
                    ArtifactEdge.parent_artifact_id == assembly_artifact_id,
                )
            )
        self.assertEqual([assembly_id], [assembly.id for assembly in assemblies])
        self.assertIsNotNone(edge)

    def test_direct_export_rejects_legacy_assembly_after_output_settings_change(self):
        run_id, legacy_snapshot, _legacy_hash, _assembly_id = (
            self._legacy_completed_run_assembly()
        )
        changed_snapshot = deepcopy(legacy_snapshot)
        changed_bitrate = (
            "64k" if changed_snapshot["output"]["bitrate"] != "64k" else "128k"
        )
        changed_snapshot["output"]["bitrate"] = changed_bitrate

        with self.assertRaisesRegex(ValueError, "settings changed.*Reassemble"):
            WorkflowHandlers(self.database, self.paths).export(
                {
                    "session_id": self.record.id,
                    "settings": {
                        **dict(changed_snapshot["output"]),
                        "generation_run_id": run_id,
                    },
                    "resolved_settings_snapshot": changed_snapshot,
                },
                lambda *_args: None,
                threading.Event(),
            )

    def test_batch_segment_update_is_atomic_on_revision_conflict(self):
        plan = self.generation.create_plan(
            self.record.id,
            source_revision_id=None,
            segments=[{"text": "First"}, {"text": "Second"}],
        )
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan["active_revision_id"]
                    )
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            original = [(item.id, item.text, item.revision) for item in segments]

        with self.assertRaises(RevisionConflict):
            self.generation.update_segments(
                self.record.id,
                [
                    {
                        "id": original[0][0],
                        "revision": original[0][2],
                        "changes": {"text": "Changed first"},
                    },
                    {
                        "id": original[1][0],
                        "revision": original[1][2] + 1,
                        "changes": {"text": "Changed second"},
                    },
                ],
            )

        with self.database.session() as session:
            stored = [
                session.get(GenerationSegment, segment_id)
                for segment_id, _text, _revision in original
            ]
            self.assertEqual(
                [(text, revision) for _id, text, revision in original],
                [(item.text, item.revision) for item in stored],
            )

    def test_batch_segment_update_commits_all_changes_together(self):
        plan = self.generation.create_plan(
            self.record.id,
            source_revision_id=None,
            segments=[{"text": "First"}, {"text": "Second"}],
        )
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan["active_revision_id"]
                    )
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            updates = [
                {
                    "id": item.id,
                    "revision": item.revision,
                    "changes": {"text": f"Changed {index + 1}"},
                }
                for index, item in enumerate(segments)
            ]

        result = self.generation.update_segments(self.record.id, updates)

        self.assertEqual(
            ["Changed 1", "Changed 2"], [item["text"] for item in result["items"]]
        )
        self.assertEqual([2, 2], [item["revision"] for item in result["items"]])

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
        artifact, output_path = ArtifactService(self.database, self.paths).resolve(
            result["artifact_id"]
        )
        self.assertEqual(
            [{"start_ms": 0, "title": "First"}], artifact.metadata_json["chapters"]
        )
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_chapters",
                "-of",
                "json",
                str(output_path),
            ],
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
        artifact, path = ArtifactService(self.database, self.paths).resolve(
            latest["artifact_id"]
        )
        self.assertEqual(420, len(AudioSegment.from_file(path)))
        self.assertEqual("assembled_audio", artifact.role)
        snapshot = artifact.metadata_json["output_settings"]
        self.assertEqual(1, snapshot["version"])
        self.assertEqual("wav", snapshot["sections"]["output"]["format"])
        self.assertEqual(
            800,
            snapshot["sections"]["audio"]["synchronization_delay_ms"],
        )
        self.assertEqual(64, len(snapshot["settings_hash"]))

        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_ids[0])
            revision = segment.revision
        self.generation.update_segment(
            segment_ids[0], revision, {"silence_after_ms": 250}
        )
        self.assertEqual(
            "stale", self.generation.latest_assembly(self.record.id)["status"]
        )
        with self.database.session() as session:
            self.assertEqual("stale", session.get(Artifact, artifact.id).state)

    def test_assembly_payload_exposes_durable_job_progress_detail(self):
        self._plan_with_takes()
        queued = self.generation.create_assembly(self.record.id)
        job = self.jobs.claim("assembly-progress-worker")
        self.assertEqual(queued["job_id"], job.id)
        self.jobs.heartbeat(
            job.id,
            "assembly-progress-worker",
            lease_generation=job.lease_generation,
            progress=0.42,
            detail="Loaded 1 of 2 audio segments",
        )

        latest = self.generation.latest_assembly(self.record.id)

        self.assertEqual(0.42, latest["progress"])
        self.assertEqual("Loaded 1 of 2 audio segments", latest["progress_detail"])

    def test_subtitle_generation_assembly_uses_source_timestamps_without_added_pauses(
        self,
    ):
        with self.database.session() as session:
            document = Document(
                session_id=self.record.id, stage="translation", language="pl"
            )
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id, revision_number=1, content_hash="timed"
            )
            session.add(revision)
            session.flush()
            session.add_all(
                [
                    Segment(
                        revision_id=revision.id,
                        ordinal=0,
                        start_ms=500,
                        end_ms=1000,
                        text="Pierwszy.",
                    ),
                    Segment(
                        revision_id=revision.id,
                        ordinal=1,
                        start_ms=2000,
                        end_ms=2500,
                        text="Drugi.",
                    ),
                ]
            )
            document.active_revision_id = revision.id
            revision_id = revision.id
        plan = self.generation.create_plan(
            self.record.id,
            source_revision_id=revision_id,
            segments=[
                {
                    "text": "Pierwszy.",
                    "node_kind": "subtitle_cue",
                    "source_segment_ids": [1],
                    "silence_after_ms": 0,
                },
                {
                    "text": "Drugi.",
                    "node_kind": "subtitle_cue",
                    "source_segment_ids": [2],
                    "silence_after_ms": 0,
                },
            ],
        )
        artifacts = ArtifactService(self.database, self.paths)
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan["active_revision_id"]
                    )
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            segment_ids = [segment.id for segment in segments]
        for index, (segment_id, duration) in enumerate(zip(segment_ids, (100, 150))):
            path = self.session_dir / f"subtitle-take-{index}.wav"
            Sine(440 + index * 110).to_audio_segment(duration=duration).export(
                path, format="wav"
            ).close()
            artifact = artifacts.register(
                path, kind="audio", role="generation_take", session_id=self.record.id
            )
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

        queued = self.generation.create_assembly(self.record.id)
        result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": queued["id"]}, lambda *_args: None, threading.Event()
        )

        self.assertEqual(2500, result["duration_ms"])
        artifact, output_path = artifacts.resolve(result["artifact_id"])
        self.assertEqual(2500, len(AudioSegment.from_file(output_path)))
        self.assertEqual(
            [500, 2000],
            [item["target_start_ms"] for item in artifact.metadata_json["takes"]],
        )
        self.assertTrue(
            all(
                item["silence_after_ms"] == 0
                for item in artifact.metadata_json["takes"]
            )
        )

    def test_subtitle_generation_assembly_joins_explicit_alignment_group_before_timing(
        self,
    ):
        with self.database.session() as session:
            document = Document(
                session_id=self.record.id, stage="translation", language="pl"
            )
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id,
                revision_number=1,
                content_hash="explicit-alignment-group",
            )
            session.add(revision)
            session.flush()
            cues = [
                Segment(
                    revision_id=revision.id,
                    ordinal=0,
                    start_ms=500,
                    end_ms=1000,
                    text="Pierwsza część.",
                ),
                Segment(
                    revision_id=revision.id,
                    ordinal=1,
                    start_ms=1000,
                    end_ms=1500,
                    text="Druga część.",
                ),
            ]
            session.add_all(cues)
            session.flush()
            cue_ids = [cue.id for cue in cues]
            document.active_revision_id = revision.id
            revision_id = revision.id
        plan = self.generation.create_plan(
            self.record.id,
            source_revision_id=revision_id,
            segments=[
                {
                    "text": "Pierwsza część.",
                    "node_kind": "subtitle_cue",
                    "source_segment_ids": [cue_ids[0]],
                    "alignment_group": "a0001",
                    "silence_after_ms": 0,
                },
                {
                    "text": "Druga część.",
                    "node_kind": "subtitle_cue",
                    "source_segment_ids": [cue_ids[1]],
                    "alignment_group": "a0001",
                    "silence_after_ms": 0,
                },
            ],
        )
        artifacts = ArtifactService(self.database, self.paths)
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan["active_revision_id"]
                    )
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            segment_ids = [segment.id for segment in segments]
        for index, segment_id in enumerate(segment_ids):
            path = self.session_dir / f"explicit-group-take-{index}.wav"
            Sine(440 + index * 110).to_audio_segment(duration=100).export(
                path, format="wav"
            ).close()
            artifact = artifacts.register(
                path,
                kind="audio",
                role="generation_take",
                session_id=self.record.id,
            )
            with self.database.session() as session:
                segment = session.get(GenerationSegment, segment_id)
                segment.status = "completed"
                session.add(
                    AudioTake(
                        generation_segment_id=segment_id,
                        artifact_id=artifact.id,
                        kind="tts",
                        status="completed",
                        duration_ms=100,
                        is_active=True,
                    )
                )

        queued = self.generation.create_assembly(self.record.id)
        result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": queued["id"]},
            lambda *_args: None,
            threading.Event(),
        )

        self.assertEqual("subtitle_timed", result["synchronization"]["mode"])
        self.assertEqual(1, result["synchronization"]["block_count"])
        artifact, _output_path = artifacts.resolve(result["artifact_id"])
        self.assertEqual(
            ["a0001", "a0001"],
            [item["alignment_group"] for item in artifact.metadata_json["takes"]],
        )

    def test_second_generation_run_still_applies_configured_drift_speedup(self):
        with self.database.session() as session:
            document = Document(
                session_id=self.record.id, stage="translation", language="pl"
            )
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id, revision_number=1, content_hash="drift"
            )
            session.add(revision)
            session.flush()
            session.add(
                Segment(
                    revision_id=revision.id,
                    ordinal=0,
                    start_ms=0,
                    end_ms=400,
                    text="Długi tekst.",
                )
            )
            document.active_revision_id = revision.id
            revision_id = revision.id
        plan = self.generation.create_plan(
            self.record.id,
            source_revision_id=revision_id,
            segments=[
                {
                    "text": "Długi tekst.",
                    "node_kind": "subtitle_cue",
                    "source_segment_ids": [1],
                }
            ],
        )
        with self.database.session() as session:
            segment = session.scalar(
                select(GenerationSegment).where(
                    GenerationSegment.plan_revision_id == plan["active_revision_id"]
                )
            )
            first_run = GenerationRun(
                session_id=self.record.id,
                plan_revision_id=plan["active_revision_id"],
                sequence_number=1,
                status="completed",
            )
            second_run = GenerationRun(
                session_id=self.record.id,
                plan_revision_id=plan["active_revision_id"],
                sequence_number=2,
                status="completed",
            )
            session.add_all([first_run, second_run])
            session.flush()
            second_run_id = second_run.id
            segment_id = segment.id
            segment.status = "completed"
        take_path = self.session_dir / "second-run-drifting-take.wav"
        Sine(440).to_audio_segment(duration=900).export(take_path, format="wav").close()
        take_artifact = ArtifactService(self.database, self.paths).register(
            take_path, kind="audio", role="generation_take", session_id=self.record.id
        )
        with self.database.session() as session:
            session.add(
                AudioTake(
                    generation_segment_id=segment_id,
                    generation_run_id=second_run_id,
                    artifact_id=take_artifact.id,
                    kind="tts",
                    status="completed",
                    duration_ms=900,
                    is_active=True,
                )
            )
        audio_profile = self.settings.get(self.record.id, "audio")
        self.settings.update(
            self.record.id,
            "audio",
            audio_profile["revision"],
            {
                **audio_profile["override"],
                "synchronization_speed": 3.0,
                "synchronization_delay_ms": 0,
            },
        )

        queued = self.generation.create_assembly(
            self.record.id, generation_run_id=second_run_id
        )
        result = WorkflowHandlers(self.database, self.paths).assemble_generation_output(
            {"output_assembly_id": queued["id"]}, lambda *_args: None, threading.Event()
        )

        self.assertEqual("subtitle_timed", result["synchronization"]["mode"])
        self.assertEqual(3.0, result["synchronization"]["configured_max_speed_factor"])
        self.assertEqual(1, result["synchronization"]["speed_adjusted_block_count"])

    def test_selected_run_assembles_its_cumulative_take_snapshot(self):
        segment_ids = self._plan_with_takes()
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment).order_by(GenerationSegment.ordinal)
                ).all()
            )
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
            for take in session.scalars(
                select(AudioTake).order_by(AudioTake.created_at)
            ).all():
                take.generation_run_id = first_run.id
            first_run_id = first_run.id
            second_run_id = second_run.id

        replacement_path = self.session_dir / "take-replacement.wav"
        Sine(880).to_audio_segment(duration=200).export(
            replacement_path, format="wav"
        ).close()
        replacement_artifact = ArtifactService(self.database, self.paths).register(
            replacement_path,
            kind="audio",
            role="generation_take",
            session_id=self.record.id,
        )
        with self.database.session() as session:
            for take in session.scalars(
                select(AudioTake).where(
                    AudioTake.generation_segment_id == segment_ids[0]
                )
            ).all():
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

        first_assembly = self.generation.create_assembly(
            self.record.id, generation_run_id=first_run_id
        )
        first_result = WorkflowHandlers(
            self.database, self.paths
        ).assemble_generation_output(
            {"output_assembly_id": first_assembly["id"]},
            lambda *_args: None,
            threading.Event(),
        )
        second_assembly = self.generation.create_assembly(
            self.record.id, generation_run_id=second_run_id
        )
        second_result = WorkflowHandlers(
            self.database, self.paths
        ).assemble_generation_output(
            {"output_assembly_id": second_assembly["id"]},
            lambda *_args: None,
            threading.Event(),
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
            take = session.scalar(
                select(AudioTake).where(AudioTake.generation_segment_id == segment.id)
            )
            take_id = take.id

        updated = self.generation.update_segment(
            segment_ids[1], revision, {"node_kind": "chapter_marker"}
        )

        self.assertEqual("completed", updated["status"])
        self.assertEqual(
            "stale", self.generation.latest_assembly(self.record.id)["status"]
        )
        with self.database.session() as session:
            self.assertEqual("completed", session.get(AudioTake, take_id).status)

    def test_stale_selected_take_is_rejected_and_failure_is_persisted(self):
        segment_ids = self._plan_with_takes()
        with self.database.session() as session:
            take = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_segment_id == segment_ids[0]
                )
            )
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
        artifact, output_path = ArtifactService(self.database, self.paths).resolve(
            result["artifact_id"]
        )
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
        artifact, output_path = ArtifactService(self.database, self.paths).resolve(
            result["artifact_id"]
        )

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
            self.assertEqual(
                "canceled", session.get(OutputAssembly, queued["id"]).status
            )


if __name__ == "__main__":
    unittest.main()
