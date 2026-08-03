import json
import os
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from pydub import AudioSegment
from pydub.generators import Sine

from pandrator.web.audio_assembly import (
    PYDUB_BACKEND,
    STREAMING_BACKEND,
    AudioAssemblyCancelled,
    AudioAssemblyPart,
    assemble_audio_plan,
    build_audio_assembly_plan,
    resolve_assembly_backend,
)
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.media_process import (
    MediaProcessCancelled,
    find_first_audible_seconds,
    run_media_process,
)
from pandrator.web.sessions import SessionService
from pandrator.web.waveform import WaveformCancelled, generate_waveform_peaks
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg qualification requires ffmpeg and ffprobe")
class StreamingAudioAssemblyTests(unittest.TestCase):
    def test_plan_contains_metadata_only_and_resolves_chapter_starts(self):
        plan = build_audio_assembly_plan(
            [
                AudioAssemblyPart(Path("first.wav"), 100, silence_after_ms=50),
                AudioAssemblyPart(Path("second.wav"), 200),
            ],
            output_format="wav",
            sample_rate_hz=24000,
            channels=1,
            chapters=[(0, "First"), (1, "Second")],
        )

        self.assertEqual(350, plan.expected_duration_ms)
        self.assertEqual([0, 150], [chapter.expected_start_ms for chapter in plan.chapters])
        self.assertTrue(all(isinstance(part.path, Path) for part in plan.parts))
        self.assertFalse(any(isinstance(value, AudioSegment) for part in plan.parts for value in vars(part).values()))

    def test_mixed_codecs_rates_fades_chapters_and_long_paths_stream_correctly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            long_folder = root / ("source folder with apostrophe's " + ("x" * 90))
            long_folder.mkdir()
            first = long_folder / "first take.wav"
            second = long_folder / "second take.mp3"
            Sine(440, sample_rate=22050).to_audio_segment(duration=160).export(first, format="wav").close()
            (
                Sine(660, sample_rate=44100)
                .to_audio_segment(duration=200)
                .set_channels(2)
                .export(second, format="mp3", bitrate="128k")
                .close()
            )
            destination = root / "mixed output.wav"
            plan = build_audio_assembly_plan(
                [
                    AudioAssemblyPart(
                        first,
                        160,
                        silence_after_ms=90,
                        fade_in_ms=30,
                        fade_out_ms=30,
                    ),
                    AudioAssemblyPart(
                        second,
                        200,
                        fade_in_ms=30,
                        fade_out_ms=30,
                    ),
                ],
                output_format="wav",
                sample_rate_hz=22050,
                channels=1,
                chapters=[(0, "First"), (1, "Second")],
            )

            result = assemble_audio_plan(plan, destination)
            decoded = AudioSegment.from_wav(destination)

            self.assertEqual(STREAMING_BACKEND, result.backend)
            self.assertLessEqual(abs(result.duration_ms - 450), 20)
            self.assertLessEqual(abs(len(decoded) - 450), 20)
            self.assertLess(decoded[:5].max, decoded[60:100].max)
            self.assertLess(decoded[150:160].max, decoded[60:100].max)
            self.assertLess(decoded[175:235].max, decoded[60:100].max // 20)
            self.assertLessEqual(abs(result.chapter_starts_ms[1] - 250), 15)

    def test_streaming_encoder_supports_every_output_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            Sine(440, sample_rate=24000).to_audio_segment(duration=180).export(
                source,
                format="wav",
            ).close()
            for output_format in ("wav", "mp3", "m4b", "opus", "flac"):
                with self.subTest(output_format=output_format):
                    plan = build_audio_assembly_plan(
                        [AudioAssemblyPart(source, 180)],
                        output_format=output_format,
                        bitrate="128k",
                        sample_rate_hz=24000,
                        channels=1,
                    )
                    destination = root / f"output.{output_format}"

                    result = assemble_audio_plan(plan, destination)

                    self.assertLessEqual(abs(result.duration_ms - 180), 1)
                    self.assertLessEqual(
                        abs(len(AudioSegment.from_file(destination)) - 180),
                        50,
                    )

    def test_thousands_of_pcm_segments_keep_python_memory_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ten-ms.wav"
            AudioSegment.silent(duration=10, frame_rate=8000).export(source, format="wav").close()
            plan = build_audio_assembly_plan(
                [AudioAssemblyPart(source, 10) for _index in range(2000)],
                output_format="wav",
                sample_rate_hz=8000,
                channels=1,
            )
            destination = root / "twenty-seconds.wav"

            tracemalloc.start()
            result = assemble_audio_plan(plan, destination)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            self.assertEqual(20000, result.duration_ms)
            self.assertLess(peak, 8 * 1024 * 1024)
            self.assertEqual(2000, len(result.part_duration_ms))
            self.assertTrue(destination.is_file())

    def test_peak_memory_does_not_follow_single_take_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            peaks = []
            for duration_ms in (1000, 60000):
                source = root / f"source-{duration_ms}.wav"
                AudioSegment.silent(
                    duration=duration_ms,
                    frame_rate=8000,
                ).export(source, format="wav").close()
                plan = build_audio_assembly_plan(
                    [AudioAssemblyPart(source, duration_ms)],
                    output_format="wav",
                    sample_rate_hz=8000,
                    channels=1,
                )
                tracemalloc.start()
                assemble_audio_plan(plan, root / f"output-{duration_ms}.wav")
                _current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                peaks.append(peak)

            self.assertLessEqual(peaks[1], peaks[0] + 1024 * 1024)

    def test_cancellation_removes_partial_output_and_temporary_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            AudioSegment.silent(duration=25, frame_rate=8000).export(source, format="wav").close()
            plan = build_audio_assembly_plan(
                [AudioAssemblyPart(source, 25) for _index in range(50)],
                output_format="wav",
                sample_rate_hz=8000,
                channels=1,
            )
            destination = root / "canceled.wav"
            canceled = threading.Event()

            def progress(_fraction, _detail):
                canceled.set()

            with self.assertRaises(AudioAssemblyCancelled):
                assemble_audio_plan(
                    plan,
                    destination,
                    cancel_event=canceled,
                    progress=progress,
                )

            self.assertFalse(destination.exists())
            self.assertEqual([], list(root.glob(".assembly-stream-*")))

    def test_pydub_backend_remains_an_explicit_compatibility_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            Sine(440).to_audio_segment(duration=100).export(source, format="wav").close()
            plan = build_audio_assembly_plan(
                [AudioAssemblyPart(source, 100)],
                output_format="wav",
                sample_rate_hz=44100,
                channels=1,
            )
            destination = root / "legacy.wav"
            with mock.patch.dict(
                os.environ,
                {"PANDRATOR_AUDIO_ASSEMBLER": "pydub"},
            ):
                self.assertEqual(PYDUB_BACKEND, resolve_assembly_backend())
                result = assemble_audio_plan(plan, destination)

            self.assertEqual(PYDUB_BACKEND, result.backend)
            self.assertEqual(100, len(AudioSegment.from_wav(destination)))


class MediaCancellationTests(unittest.TestCase):
    def test_cancellable_process_stops_promptly(self):
        canceled = threading.Event()
        timer = threading.Timer(0.2, canceled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(MediaProcessCancelled):
                run_media_process(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    cancel_event=canceled,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg qualification requires ffmpeg")
    def test_first_audible_detection_returns_a_short_preroll(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "delayed-voice.wav")
            (
                AudioSegment.silent(duration=1500, frame_rate=48000)
                + Sine(440, sample_rate=48000).to_audio_segment(duration=1000)
            ).export(source, format="wav").close()

            start = find_first_audible_seconds(source)

        self.assertGreater(start, 0.4)
        self.assertLess(start, 0.7)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg qualification requires ffmpeg and ffprobe")
class BoundedWaveformTests(unittest.TestCase):
    def test_long_stereo_waveform_memory_tracks_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "long stereo.wav"
            (
                Sine(220, sample_rate=16000)
                .to_audio_segment(duration=60000)
                .set_channels(2)
                .export(source, format="wav")
                .close()
            )

            tracemalloc.start()
            result = generate_waveform_peaks(source, max_points=256)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            self.assertLessEqual(abs(result.duration_ms - 60000), 10)
            self.assertEqual(2, result.channels)
            self.assertLessEqual(len(result.points), 256)
            self.assertGreater(max(result.points), 0.8)
            self.assertLess(peak, 4 * 1024 * 1024)
            self.assertEqual([], list(root.glob(".waveform-*")))

    def test_waveform_cancellation_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            AudioSegment.silent(duration=100, frame_rate=8000).export(source, format="wav").close()
            canceled = threading.Event()
            canceled.set()

            with self.assertRaises(WaveformCancelled):
                generate_waveform_peaks(
                    source,
                    max_points=128,
                    work_dir=root,
                    cancel_event=canceled,
                )

            self.assertEqual([], list(root.glob(".waveform-*")))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg qualification requires ffmpeg and ffprobe")
class WaveformHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.record = SessionService(self.database).create(
            "Waveform",
            workflow_kind="voiceover",
        )
        self.session_dir = self.paths.sessions / self.record.storage_key
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def test_handler_registers_compact_waveform_metadata(self):
        source_path = self.session_dir / "source stereo.wav"
        (
            Sine(440, sample_rate=16000)
            .to_audio_segment(duration=1000)
            .set_channels(2)
            .export(source_path, format="wav")
            .close()
        )
        source = ArtifactService(self.database, self.paths).register(
            source_path,
            kind="audio",
            role="source_audio",
            session_id=self.record.id,
        )

        result = WorkflowHandlers(self.database, self.paths).generate_waveform(
            {"source_artifact_id": source.id, "max_points": 256},
            lambda *_args: None,
            threading.Event(),
        )
        artifact, waveform_path = ArtifactService(self.database, self.paths).resolve(
            result["artifact_id"]
        )
        payload = json.loads(waveform_path.read_text(encoding="utf-8"))

        self.assertEqual("waveform_peaks", artifact.role)
        self.assertEqual(2, payload["channels"])
        self.assertLessEqual(len(payload["points"]), 256)
        self.assertEqual(len(payload["points"]), result["point_count"])
        self.assertEqual(8000, artifact.metadata_json["analysis_sample_rate_hz"])


if __name__ == "__main__":
    unittest.main()
