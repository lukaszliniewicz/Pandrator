import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

from pandrator.web.audio_assembly import (
    PYDUB_BACKEND,
    STREAMING_BACKEND,
    AudioAssemblyCancelled,
    AudioAssemblyPart,
    AudioAssemblyResult,
    assemble_audio_plan,
    build_audio_assembly_plan,
)


def write_pcm(path, frames, *, sample_rate=44100, channels=2):
    with wave.open(str(path), "wb") as writer:
        writer.setframerate(sample_rate)
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.writeframes(b"\x01\x00" * frames * channels)


class BookAssemblyTimelineTests(unittest.TestCase):
    def test_result_remains_backward_compatible(self):
        result = AudioAssemblyResult(1, (1,), (), STREAMING_BACKEND)
        self.assertEqual(0, result.sample_rate_hz)
        self.assertEqual(0, result.total_frames)
        self.assertEqual((), result.part_start_frames)
        self.assertEqual((), result.part_end_frames)

    def test_both_backends_report_sample_exact_ranges_and_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parts = []
            lengths = (7, 51, 109)
            pauses = ((1, 7), (3, 11), (0, 3))
            for index, (frames, (before, after)) in enumerate(zip(lengths, pauses, strict=True)):
                source = root / f"take-{index}.wav"
                write_pcm(source, frames)
                parts.append(AudioAssemblyPart(source, 100, before, after))
            plan = build_audio_assembly_plan(
                parts,
                output_format="wav",
                sample_rate_hz=44100,
                channels=2,
                chapters=[(index, str(index)) for index in range(len(parts))],
            )
            for backend in (STREAMING_BACKEND, PYDUB_BACKEND):
                with self.subTest(backend=backend):
                    destination = root / f"{backend}.wav"
                    result = assemble_audio_plan(plan, destination, backend=backend)
                    cursor = 0
                    starts, ends = [], []
                    # Keep each backend's existing silence rounding behavior.
                    count_silence = round if backend == STREAMING_BACKEND else int
                    for frames, (before, after) in zip(lengths, pauses, strict=True):
                        cursor += count_silence(44100 * before / 1000)
                        starts.append(cursor)
                        cursor += frames
                        ends.append(cursor)
                        cursor += count_silence(44100 * after / 1000)
                    self.assertEqual(44100, result.sample_rate_hz)
                    self.assertEqual(tuple(starts), result.part_start_frames)
                    self.assertEqual(tuple(ends), result.part_end_frames)
                    self.assertEqual(cursor, result.total_frames)
                    self.assertEqual(
                        tuple(round(start * 1000 / 44100) for start in starts),
                        result.chapter_starts_ms,
                    )
                    with wave.open(str(destination), "rb") as reader:
                        self.assertEqual(cursor, reader.getnframes())
                        pcm = reader.readframes(cursor)
                    bytes_per_frame = 4
                    for start, end, frames in zip(starts, ends, lengths, strict=True):
                        self.assertEqual(
                            b"\x01\x00" * frames * 2,
                            pcm[start * bytes_per_frame : end * bytes_per_frame],
                        )
                    for left, right in zip(ends[:-1], starts[1:], strict=True):
                        self.assertEqual(
                            b"\0" * ((right - left) * bytes_per_frame),
                            pcm[left * bytes_per_frame : right * bytes_per_frame],
                        )

    def test_submillisecond_parts_do_not_accumulate_rounding_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "seven-frames.wav"
            write_pcm(source, 7)
            count = 256
            plan = build_audio_assembly_plan(
                [AudioAssemblyPart(source, 1) for _ in range(count)],
                output_format="wav",
                sample_rate_hz=44100,
                channels=2,
            )
            for backend in (STREAMING_BACKEND, PYDUB_BACKEND):
                with self.subTest(backend=backend):
                    destination = root / f"{backend}.wav"
                    result = assemble_audio_plan(plan, destination, backend=backend)
                    self.assertEqual(
                        tuple(7 * index for index in range(count)), result.part_start_frames
                    )
                    self.assertEqual(
                        tuple(7 * (index + 1) for index in range(count)), result.part_end_frames
                    )
                    self.assertEqual(7 * count, result.total_frames)
                    self.assertEqual((0,) * count, result.part_duration_ms)
                    with wave.open(str(destination), "rb") as reader:
                        self.assertEqual(result.total_frames, reader.getnframes())

    def test_streaming_counts_normalized_frames_instead_of_expected_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            write_pcm(source, 17, sample_rate=22050)
            plan = build_audio_assembly_plan(
                [AudioAssemblyPart(source, 500)],
                output_format="wav",
                sample_rate_hz=44100,
                channels=2,
            )

            def normalize(_part, destination, _encoding, **_kwargs):
                write_pcm(destination, 33)

            with mock.patch("pandrator.web.audio_assembly._normalize_part", side_effect=normalize):
                result = assemble_audio_plan(plan, root / "output.wav", backend=STREAMING_BACKEND)
            self.assertEqual((0,), result.part_start_frames)
            self.assertEqual((33,), result.part_end_frames)
            self.assertEqual(33, result.total_frames)

    def test_pydub_counts_resampled_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            write_pcm(source, 121, sample_rate=22050)
            plan = build_audio_assembly_plan(
                [AudioAssemblyPart(source, 500)],
                output_format="wav",
                sample_rate_hz=44100,
                channels=2,
            )
            destination = root / "output.wav"
            result = assemble_audio_plan(plan, destination, backend=PYDUB_BACKEND)
            with wave.open(str(destination), "rb") as reader:
                actual_frames = reader.getnframes()
            self.assertEqual(241, actual_frames)
            self.assertEqual((0,), result.part_start_frames)
            self.assertEqual((actual_frames,), result.part_end_frames)
            self.assertEqual(actual_frames, result.total_frames)

    def test_cancellation_does_not_publish_partial_frame_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            write_pcm(source, 51)
            plan = build_audio_assembly_plan(
                [AudioAssemblyPart(source, 1) for _ in range(3)],
                output_format="wav",
                sample_rate_hz=44100,
                channels=2,
            )
            for backend in (STREAMING_BACKEND, PYDUB_BACKEND):
                with self.subTest(backend=backend):
                    cancel_event = threading.Event()
                    destination = root / f"{backend}.wav"
                    with self.assertRaises(AudioAssemblyCancelled):
                        assemble_audio_plan(
                            plan,
                            destination,
                            backend=backend,
                            cancel_event=cancel_event,
                            progress=lambda _fraction, _detail, event=cancel_event: event.set(),
                        )
                    self.assertFalse(destination.exists())
