from __future__ import annotations

import math
import shutil
import threading
import wave
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pandrator.web.media_process import MediaProcessCancelled
from pandrator.web.sound_mix_plan import (
    build_sound_mix_plan,
    map_base_frame,
    remap_audio_metadata,
)
from pandrator.web.sound_mix_render import (
    MAX_RENDER_BLOCK_FRAMES,
    normalize_mix_audio,
    render_sound_mix,
)


def _write_pcm(path: Path, values: list[int], *, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(np.asarray(values, dtype="<i2").tobytes())


def _read_pcm(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as reader:
        assert reader.getcomptype() == "NONE"
        assert reader.getsampwidth() == 2
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").tolist()


def _clip(
    cue_id: str,
    *,
    anchor: int,
    source_frames: int,
    mode: str = "overlay",
    offset: int = 0,
    **settings: int | float,
) -> dict[str, object]:
    return {
        "cue_id": cue_id,
        "variant_id": f"variant-{cue_id}",
        "content_hash": f"hash-{cue_id}",
        "anchor_frame": anchor,
        "mode": mode,
        "offset_frames": offset,
        "source_frames": source_frames,
        **settings,
    }


def _plan(
    base_frames: int,
    clips: list[dict[str, object]] | None = None,
    *,
    fixed_timeline: bool = False,
):
    return build_sound_mix_plan(
        base_frames=base_frames,
        rate=8000,
        channels=1,
        base_content_hash="base-hash",
        clips=clips or [],
        fixed_timeline=fixed_timeline,
    )


def test_insertions_preserve_same_anchor_order_and_frame_biases() -> None:
    plan = _plan(
        10,
        [
            _clip(
                "first",
                anchor=5,
                source_frames=3,
                mode="insert",
                offset=2,
                trim_start_frame=1,
                trim_end_frame=3,
            ),
            _clip("second", anchor=5, source_frames=2, mode="insert"),
        ],
    )

    assert plan["total_frames"] == 16
    assert plan["insertions"] == [
        {"base_frame": 5, "added_frames": 4},
        {"base_frame": 5, "added_frames": 2},
    ]
    assert [(clip["start_frame"], clip["end_frame"]) for clip in plan["clips"]] == [
        (7, 9),
        (9, 11),
    ]
    assert map_base_frame(plan, 5, bias="before") == 5
    assert map_base_frame(plan, 5, bias="after") == 11
    assert map_base_frame(plan, 6, bias="after") == 12
    assert plan["pieces"][0] == {
        "output_start_frame": 0,
        "output_end_frame": 5,
        "source_start_frame": 0,
        "source_end_frame": 5,
    }
    assert plan["pieces"][-1] == {
        "output_start_frame": 11,
        "output_end_frame": 16,
        "source_start_frame": 5,
        "source_end_frame": 10,
    }
    assert len(plan["mix_signature"]) == 64
    assert "source_path" not in plan


@pytest.mark.parametrize(
    "override",
    [
        {"anchor_frame": True},
        {"source_frames": 0},
        {"offset_frames": True},
        {"gain_db": math.nan},
        {"gain_db": 12.01},
        {"trim_start_frame": -1},
        {"trim_start_frame": 3, "trim_end_frame": 3},
        {"fade_in_frames": 4},
        {"unexpected": 1},
    ],
)
def test_invalid_clip_values_are_rejected(override: dict[str, object]) -> None:
    clip = _clip("bad", anchor=0, source_frames=3)
    clip.update(override)
    with pytest.raises(ValueError):
        _plan(10, [clip])


def test_fixed_timeline_rejects_insertions_and_overrunning_overlays() -> None:
    with pytest.raises(ValueError, match="fixed timeline"):
        _plan(
            8,
            [_clip("insert", anchor=2, source_frames=1, mode="insert")],
            fixed_timeline=True,
        )
    with pytest.raises(ValueError, match="fixed-timeline overlay"):
        _plan(
            8,
            [_clip("overrun", anchor=6, source_frames=2, offset=1)],
            fixed_timeline=True,
        )


def test_duplicate_cue_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        _plan(
            8,
            [
                _clip("same", anchor=0, source_frames=1),
                _clip("same", anchor=3, source_frames=1),
            ],
        )


def test_renderer_preserves_speech_and_places_trimmed_insertion_after_offset(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.wav"
    effect = tmp_path / "effect.wav"
    output = tmp_path / "mixed.wav"
    _write_pcm(base, [1000] * 8)
    _write_pcm(effect, [100, 2000, 3000])
    plan = _plan(
        8,
        [
            _clip(
                "cue",
                anchor=4,
                source_frames=3,
                mode="insert",
                offset=2,
                trim_start_frame=1,
                trim_end_frame=3,
            )
        ],
    )

    result = render_sound_mix(
        base,
        {"cue": effect},
        plan,
        output,
        cancel_event=threading.Event(),
    )

    assert _read_pcm(output) == [1000] * 4 + [0, 0, 2000, 3000] + [1000] * 4
    assert result["frames"] == 12
    assert result["sample_rate"] == 8000
    assert result["channels"] == 1
    assert result["clipped_samples"] == 0
    assert result["mix_signature"] == plan["mix_signature"]


def test_renderer_adds_simultaneous_trimmed_overlays_with_gain_and_fades(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.wav"
    fade = tmp_path / "fade.wav"
    gain = tmp_path / "gain.wav"
    output = tmp_path / "mixed.wav"
    _write_pcm(base, [1000] * 8)
    _write_pcm(fade, [100, 2000, 3000, 4000])
    _write_pcm(gain, [5000, 6000])
    plan = _plan(
        8,
        [
            _clip(
                "fade",
                anchor=2,
                source_frames=4,
                offset=-1,
                trim_start_frame=1,
                trim_end_frame=4,
                gain_db=6,
                fade_in_frames=2,
                fade_out_frames=2,
            ),
            _clip("gain", anchor=2, source_frames=2, offset=1, gain_db=-6),
        ],
        fixed_timeline=True,
    )

    render_sound_mix(
        base,
        {"fade": fade, "gain": gain},
        plan,
        output,
        cancel_event=threading.Event(),
    )

    actual = _read_pcm(output)
    expected = [
        1000,
        1000,
        1000 + 3000 * 10 ** (6 / 20),
        1000 + 5000 * 10 ** (-6 / 20),
        1000 + 6000 * 10 ** (-6 / 20),
        1000,
        1000,
        1000,
    ]
    assert actual == [int(np.rint(value)) for value in expected]


def test_no_effect_render_is_sample_equivalent_and_metadata_copy_keeps_timing(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.wav"
    output = tmp_path / "identity.wav"
    samples = [-32768, -1001, 0, 1, 32767, 42, -17, 128, -129, 99]
    _write_pcm(base, samples)
    plan = _plan(10)
    render_sound_mix(base, {}, plan, output, cancel_event=threading.Event())
    assert _read_pcm(output) == samples

    metadata = {
        "duration_ms": 2,
        "audio_timeline": {"version": 1, "sample_rate_hz": 8000, "total_frames": 10},
        "takes": [
            {"take_id": "before", "start_frame": 2, "end_frame": 5},
            {"take_id": "inside", "start_frame": 5, "end_frame": 8},
            {"take_id": "marker", "start_frame": 5, "end_frame": 5},
        ],
        "chapters": [
            {"title": "Chapter", "start_ms": 0, "target_start_ms": 0},
            {"title": "After insertion", "start_ms": 0.625},
        ],
    }
    inserted = _plan(
        10,
        [_clip("effect", anchor=5, source_frames=2, mode="insert")],
    )
    remapped = remap_audio_metadata(metadata, inserted)

    assert metadata["audio_timeline"]["total_frames"] == 10
    assert remapped["audio_timeline"]["total_frames"] == 12
    assert remapped["duration_ms"] == 2
    assert [(take["start_frame"], take["end_frame"]) for take in remapped["takes"]] == [
        (2, 5),
        (7, 10),
        (7, 7),
    ]
    assert remapped["chapters"] == [
        {"title": "Chapter", "start_ms": 0, "target_start_ms": 0},
        {"title": "After insertion", "start_ms": 1},
    ]


def test_metadata_rejects_out_of_range_coordinates_without_mutating_input() -> None:
    metadata = {"takes": [{"take_id": "bad", "start_frame": 0, "end_frame": 11}]}
    before = {"takes": [{"take_id": "bad", "start_frame": 0, "end_frame": 11}]}
    with pytest.raises(ValueError, match="outside the base audio"):
        remap_audio_metadata(metadata, _plan(10))
    assert metadata == before


def test_render_cancellation_removes_only_its_partial_output(tmp_path: Path) -> None:
    base = tmp_path / "base.wav"
    output = tmp_path / "canceled.wav"
    _write_pcm(base, [1] * (MAX_RENDER_BLOCK_FRAMES + 1))
    canceled = threading.Event()

    def stop_after_first_block(_fraction: float, _detail: str) -> None:
        canceled.set()

    with pytest.raises(MediaProcessCancelled, match="canceled"):
        render_sound_mix(
            base,
            {},
            _plan(MAX_RENDER_BLOCK_FRAMES + 1),
            output,
            cancel_event=canceled,
            progress=stop_after_first_block,
        )
    assert not output.exists()
    assert base.exists()


def test_renderer_does_not_overwrite_existing_output(tmp_path: Path) -> None:
    base = tmp_path / "base.wav"
    output = tmp_path / "existing.wav"
    _write_pcm(base, [12, 34, 56])
    output.write_bytes(b"preserve me")
    with pytest.raises(FileExistsError):
        render_sound_mix(base, {}, _plan(3), output, cancel_event=threading.Event())
    assert output.read_bytes() == b"preserve me"


def test_renderer_rejects_symlink_audio_inputs(tmp_path: Path) -> None:
    base = tmp_path / "base.wav"
    linked_base = tmp_path / "linked-base.wav"
    output = tmp_path / "mixed.wav"
    _write_pcm(base, [12, 34, 56])
    linked_base.symlink_to(base)
    with pytest.raises(ValueError, match="non-symlink"):
        render_sound_mix(
            linked_base,
            {},
            _plan(3),
            output,
            cancel_event=threading.Event(),
        )
    assert not output.exists()


def test_renderer_uses_bounded_wave_reads(tmp_path: Path) -> None:
    base = tmp_path / "base.wav"
    output = tmp_path / "bounded.wav"
    _write_pcm(base, [5] * (MAX_RENDER_BLOCK_FRAMES + 17))
    requested: list[int] = []
    original = wave.Wave_read.readframes

    def tracked(reader: wave.Wave_read, count: int) -> bytes:
        requested.append(count)
        return original(reader, count)

    with mock.patch.object(wave.Wave_read, "readframes", tracked):
        render_sound_mix(
            base,
            {},
            _plan(MAX_RENDER_BLOCK_FRAMES + 17),
            output,
            cancel_event=threading.Event(),
        )
    assert requested
    assert max(requested) <= MAX_RENDER_BLOCK_FRAMES


def test_renderer_counts_saturated_output_samples(tmp_path: Path) -> None:
    base = tmp_path / "base.wav"
    effect = tmp_path / "effect.wav"
    output = tmp_path / "clipped.wav"
    _write_pcm(base, [32767] * 4)
    _write_pcm(effect, [32767, 32767])
    plan = _plan(4, [_clip("loud", anchor=1, source_frames=2, gain_db=12)])

    result = render_sound_mix(
        base,
        {"loud": effect},
        plan,
        output,
        cancel_event=threading.Event(),
    )

    assert _read_pcm(output) == [32767] * 4
    assert result["clipped_samples"] == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="audio normalization requires FFmpeg")
def test_normalize_mix_audio_reuses_streaming_pcm_assembly(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    destination = tmp_path / "normalized.wav"
    samples = [0, 100, -100, 32767]
    _write_pcm(source, samples)

    frames = normalize_mix_audio(
        source,
        destination,
        rate=8000,
        channels=1,
        cancel_event=threading.Event(),
    )

    assert frames == len(samples)
    assert _read_pcm(destination) == samples
