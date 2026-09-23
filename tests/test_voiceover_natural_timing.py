"""Natural tempo, cue-local placement and bounded cumulative catch-up."""

import math
import shutil
import tempfile
import threading
from pathlib import Path

import pytest
from pydub import AudioSegment
from pydub.generators import Sine

from pandrator.logic.dubbing.audio_sync import align_audio_blocks, alignment_adjustment
from pandrator.logic.dubbing.models import AudioAlignmentBlock
from pandrator.web.voiceover_repair import TimingGroup, advance_timing


def test_long_gap_cannot_delay_a_brief_reply_beyond_its_own_cue():
    # A real Pascal-session shape: 400 ms speech, 640 ms cue, 8.92 s to next cue.
    decision = alignment_adjustment(
        400, 8920, 0, delay_start_ms=1000, max_speed_factor=1.2,
        speech_window_duration_ms=640,
    )
    assert decision.start_delay_ms == 168
    assert decision.speed_factor == 0.9
    assert decision.start_delay_ms + math.ceil(400 / decision.speed_factor) <= 640


def test_gap_is_still_available_for_natural_overrun_without_speedup():
    decision = alignment_adjustment(
        2500, 4000, 0, delay_start_ms=1000, max_speed_factor=1.2,
        speech_window_duration_ms=2000,
    )
    assert decision.speed_factor == 1.0
    assert decision.start_delay_ms == 0


def test_small_overruns_are_bounded_across_a_sequence_not_reset_each_block():
    drift = 0
    saw_catchup = False
    for _ in range(30):
        decision = alignment_adjustment(
            1040, 1000, drift, delay_start_ms=800, max_speed_factor=1.2,
            speech_window_duration_ms=1000,
        )
        if drift:
            assert decision.start_delay_ms == 0
            assert decision.speed_factor >= 1.0
        saw_catchup |= decision.speed_factor > 1.0
        drift = max(0, drift + math.ceil(1040 / decision.speed_factor) - 1000)
        assert drift <= 50
    assert saw_catchup


def test_large_overrun_respects_speed_cap_and_carries_remaining_delay():
    decision = alignment_adjustment(
        4000, 2000, 500, delay_start_ms=800, max_speed_factor=1.2,
        speech_window_duration_ms=2000,
    )
    assert decision.speed_factor == 1.2
    assert decision.available_ms == 1500
    assert decision.start_delay_ms == 0
    assert math.ceil(4000 / decision.speed_factor) > decision.available_ms


@pytest.mark.parametrize("backend", ["streaming", "pydub"])
def test_real_assembly_and_repair_preview_share_cue_local_placement(backend):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paths = [root / "reply.wav", root / "closing.wav"]
        for path, duration in zip(paths, [400, 1005], strict=False):
            Sine(440).to_audio_segment(duration=duration).export(path, format="wav")
        blocks = [
            AudioAlignmentBlock(number="1", text="Ja.", start_ms=0, end_ms=640,
                                audio_files=[paths[0]], subtitles=[1]),
            AudioAlignmentBlock(number="2", text="A closing phrase.",
                                start_ms=8920, end_ms=9920,
                                audio_files=[paths[1]], subtitles=[2]),
        ]
        diagnostics = {}
        align_audio_blocks(
            blocks, root, output_path=root / "mix.wav", backend=backend,
            delay_start_ms=1000, speed_up_percent=120, diagnostics=diagnostics,
        )
        first, second = diagnostics["blocks"]
        assert first["start_delay_ms"] == 168
        assert first["effective_speed_factor"] < 1.0
        assert first["processed_audio_ms"] + first["start_delay_ms"] <= 640
        assert second["requested_speed_factor"] == 1.0
        assert second["drift_after_ms"] == 5
        cursor = 0
        settings = {"synchronization_delay_ms": 1000, "synchronization_speed": 1.2}
        for index, block in enumerate(blocks):
            group = TimingGroup(segments=[], paths=block.audio_files,
                                start_ms=block.start_ms, end_ms=block.end_ms,
                                references=block.subtitles)
            slot_end = blocks[index + 1].start_ms if index == 0 else block.end_ms
            preview_diagnostics = {}
            cursor, duration, delay = advance_timing(
                group, cursor, slot_end, settings, root, threading.Event(),
                diagnostics=preview_diagnostics,
            )
            detail = diagnostics["blocks"][index]
            assert duration == detail["original_audio_ms"]
            assert delay == detail["start_delay_ms"]
            assert abs(preview_diagnostics["processed_audio_ms"] - detail["processed_audio_ms"]) <= 2
            assert cursor == slot_end + detail["drift_after_ms"]


@pytest.mark.parametrize("backend", ["streaming", "pydub"])
def test_carried_drift_slowdown_fits_own_cue_and_repair_preview(backend):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paths = [root / f"block-{index}.wav" for index in range(1, 4)]
        for path, duration in zip(paths, [1200, 1000, 1000], strict=True):
            Sine(440).to_audio_segment(duration=duration).export(path, format="wav")
        blocks = [
            AudioAlignmentBlock("1", "Overrun", 0, 1000, [paths[0]], [1]),
            AudioAlignmentBlock("2", "Short cue", 1000, 3000, [paths[1]], [2]),
            AudioAlignmentBlock("3", "Next cue", 5000, 6000, [paths[2]], [3]),
        ]
        diagnostics = {}
        align_audio_blocks(
            blocks,
            root,
            output_path=root / f"aligned-{backend}.wav",
            backend=backend,
            delay_start_ms=0,
            speed_up_percent=100,
            sentence_gap_ms=0,
            diagnostics=diagnostics,
        )

        first, second, third = diagnostics["blocks"]
        assert first["drift_after_ms"] == 200
        assert second["drift_before_ms"] == 200
        assert second["effective_speed_factor"] < 1.0
        assert second["processed_audio_ms"] + second["drift_before_ms"] <= 2000
        assert second["drift_after_ms"] == 0
        assert third["drift_before_ms"] == 0

        cursor = 0
        settings = {"synchronization_delay_ms": 0, "synchronization_speed": 1.0}
        for index, block in enumerate(blocks):
            group = TimingGroup(
                segments=[], paths=block.audio_files, start_ms=block.start_ms,
                end_ms=block.end_ms, references=block.subtitles,
            )
            slot_end = blocks[index + 1].start_ms if index < 2 else block.end_ms
            preview_diagnostics = {}
            cursor, duration, delay = advance_timing(
                group, cursor, slot_end, settings, root, threading.Event(),
                diagnostics=preview_diagnostics,
            )
            detail = diagnostics["blocks"][index]
            assert duration == detail["original_audio_ms"]
            assert delay == detail["start_delay_ms"]
            assert abs(preview_diagnostics["processed_audio_ms"] - detail["processed_audio_ms"]) <= 2
            assert abs(cursor - (slot_end + detail["drift_after_ms"])) <= 2


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg qualification requires ffmpeg")
def test_repair_preview_rejects_candidate_past_drift_adjusted_own_cue(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first_path = root / "overrun.wav"
        second_path = root / "short.wav"
        Sine(440).to_audio_segment(duration=1200).export(first_path, format="wav")
        Sine(440).to_audio_segment(duration=1000).export(second_path, format="wav")

        def write_oversized_candidate(_source, destination, _factor, **_kwargs):
            AudioSegment.silent(duration=1900).export(destination, format="wav").close()

        monkeypatch.setattr(
            "pandrator.web.voiceover_repair._speed_up_wav_streaming",
            write_oversized_candidate,
        )
        settings = {"synchronization_delay_ms": 0, "synchronization_speed": 1.0}
        first = TimingGroup(
            segments=[], paths=[first_path], start_ms=0, end_ms=1000, references=[],
        )
        second = TimingGroup(
            segments=[], paths=[second_path], start_ms=1000, end_ms=3000, references=[],
        )
        cursor, _, _ = advance_timing(
            first, 0, 1000, settings, root, threading.Event(),
        )
        preview_diagnostics = {}
        cursor, duration, delay = advance_timing(
            second, cursor, 5000, settings, root, threading.Event(),
            diagnostics=preview_diagnostics,
        )

        assert duration == 1000
        assert delay == 0
        assert preview_diagnostics["processed_audio_ms"] == 1000
        assert cursor == 5000
