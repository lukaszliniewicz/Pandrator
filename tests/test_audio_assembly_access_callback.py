from __future__ import annotations

import os
import wave
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest

from pandrator.web import audio_assembly
from pandrator.web.audio_assembly import (
    PYDUB_BACKEND,
    STREAMING_BACKEND,
    AudioAssemblyPart,
    assemble_audio_plan,
    build_audio_assembly_plan,
    preferred_pcm_format,
)


def _write_pcm(
    path: Path,
    *,
    sample_rate: int = 24000,
    channels: int = 1,
    frames: int = 24,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\0" * frames * channels * 2)
    return path


def _plan(
    sources: tuple[Path, ...],
    *,
    output_format: str = "wav",
    sample_rate_hz: int = 24000,
    channels: int = 1,
):
    return build_audio_assembly_plan(
        [AudioAssemblyPart(source, 1) for source in sources],
        output_format=output_format,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
    )


def test_preferred_pcm_format_checks_before_wave_open_and_preserves_permission_error(
    tmp_path: Path,
) -> None:
    source = _write_pcm(tmp_path / "source.wav")
    denial = PermissionError("source read denied")
    opened: list[Path] = []
    real_wave_open = wave.open

    def tracked_wave_open(file, *args, **kwargs):
        opened.append(Path(file))
        return real_wave_open(file, *args, **kwargs)

    def deny() -> None:
        raise denial

    with mock.patch.object(audio_assembly.wave, "open", side_effect=tracked_wave_open):
        with pytest.raises(PermissionError) as caught:
            preferred_pcm_format(source, before_io=deny)

    assert caught.value is denial
    assert opened == []


def test_preferred_pcm_format_checks_before_fallback_probe(tmp_path: Path) -> None:
    source = tmp_path / "not-a-wave.wav"
    source.write_bytes(b"not a wave file")
    denial = RuntimeError("probe denied")
    calls = 0

    def before_io() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise denial

    with mock.patch.object(audio_assembly, "probe_audio_stream") as probe:
        with pytest.raises(RuntimeError) as caught:
            preferred_pcm_format(source, before_io=before_io)

    assert caught.value is denial
    assert calls == 2
    probe.assert_not_called()


def test_assemble_callback_runs_before_output_directory_creation(tmp_path: Path) -> None:
    source = _write_pcm(tmp_path / "source.wav")
    destination = tmp_path / "not-created" / "assembled.wav"
    denial = RuntimeError("output denied")

    def deny() -> None:
        assert not destination.parent.exists()
        raise denial

    with pytest.raises(RuntimeError) as caught:
        assemble_audio_plan(_plan((source,)), destination, before_io=deny)

    assert caught.value is denial
    assert not destination.parent.exists()


def test_streaming_permission_error_before_first_source_open_propagates(
    tmp_path: Path,
) -> None:
    source = _write_pcm(tmp_path / "source.wav")
    denial = PermissionError("source read denied")
    source_opens: list[Path] = []
    calls = 0
    real_wave_open = wave.open

    def tracked_wave_open(file, *args, **kwargs):
        path = Path(file)
        if path == source:
            source_opens.append(path)
        return real_wave_open(file, *args, **kwargs)

    def before_io() -> None:
        nonlocal calls
        calls += 1
        if calls == 5:  # Outer entry, three streaming setup gates, then source wave.open.
            raise denial

    with (
        mock.patch.object(audio_assembly.wave, "open", side_effect=tracked_wave_open),
        mock.patch.object(audio_assembly, "run_media_process") as process,
    ):
        with pytest.raises(PermissionError) as caught:
            assemble_audio_plan(_plan((source,)), tmp_path / "output.wav", before_io=before_io)

    assert caught.value is denial
    assert calls == 5
    assert source_opens == []
    process.assert_not_called()


@pytest.mark.parametrize(
    ("denial_call", "temporary_directory_exists_before_denial"),
    ((2, False), (3, False), (4, True)),
)
def test_streaming_setup_denials_stop_before_directory_or_timeline_open(
    tmp_path: Path,
    denial_call: int,
    temporary_directory_exists_before_denial: bool,
) -> None:
    source = _write_pcm(tmp_path / "source.wav")
    destination = tmp_path / "destination" / "output.wav"
    work_dir = tmp_path / "assembly-work"
    assert not destination.parent.exists()
    denial = PermissionError("streaming setup denied")
    calls = 0
    opened_paths: list[Path] = []
    real_wave_open = wave.open

    def tracked_wave_open(file, *args, **kwargs):
        opened_paths.append(Path(file))
        return real_wave_open(file, *args, **kwargs)

    def before_io() -> None:
        nonlocal calls
        calls += 1
        if calls == denial_call:
            assert destination.parent.exists()  # The outer entry already created it.
            assert work_dir.exists() is (denial_call != 2)
            temporary_dirs = list(work_dir.glob(".assembly-stream-*")) if work_dir.exists() else []
            assert bool(temporary_dirs) is temporary_directory_exists_before_denial
            raise denial

    with mock.patch.object(audio_assembly.wave, "open", side_effect=tracked_wave_open):
        with pytest.raises(PermissionError) as caught:
            assemble_audio_plan(
                _plan((source,)),
                destination,
                work_dir=work_dir,
                before_io=before_io,
            )

    assert caught.value is denial
    assert calls == denial_call
    assert opened_paths == []
    if work_dir.exists():
        assert list(work_dir.iterdir()) == []


def test_streaming_late_denial_prevents_second_source_open(tmp_path: Path) -> None:
    first = _write_pcm(tmp_path / "first.wav")
    second = _write_pcm(tmp_path / "second.wav")
    denial = RuntimeError("second source read denied")
    source_opens: list[Path] = []
    calls = 0
    real_wave_open = wave.open

    def tracked_wave_open(file, *args, **kwargs):
        path = Path(file)
        if path in {first, second}:
            source_opens.append(path)
        return real_wave_open(file, *args, **kwargs)

    def before_io() -> None:
        nonlocal calls
        calls += 1
        if calls == 6:  # Setup gates and first source open, then second source open.
            raise denial

    with mock.patch.object(audio_assembly.wave, "open", side_effect=tracked_wave_open):
        with pytest.raises(RuntimeError) as caught:
            assemble_audio_plan(
                _plan((first, second)), tmp_path / "output.wav", before_io=before_io
            )

    assert caught.value is denial
    assert calls == 6
    assert source_opens == [first]


def test_normalization_denial_prevents_media_process_launch(tmp_path: Path) -> None:
    source = _write_pcm(tmp_path / "source.wav", sample_rate=22050)
    denial = RuntimeError("normalization denied")
    calls = 0

    def before_io() -> None:
        nonlocal calls
        calls += 1
        if calls == 6:  # Setup gates, source open, then normalization process.
            raise denial

    with mock.patch.object(audio_assembly, "run_media_process") as process:
        with pytest.raises(RuntimeError) as caught:
            assemble_audio_plan(_plan((source,)), tmp_path / "output.wav", before_io=before_io)

    assert caught.value is denial
    assert calls == 6
    process.assert_not_called()


def test_normalized_pcm_open_has_its_own_access_check(tmp_path: Path) -> None:
    source = _write_pcm(tmp_path / "source.wav", sample_rate=22050)
    denial = PermissionError("normalized pcm read denied")
    calls = 0
    normalized_paths: list[Path] = []
    real_wave_open = wave.open

    def tracked_wave_open(file, *args, **kwargs):
        path = Path(file)
        mode = args[0] if args else kwargs.get("mode", "rb")
        if path.name.startswith("part-") and mode != "wb":
            normalized_paths.append(path)
        return real_wave_open(file, *args, **kwargs)

    def before_io() -> None:
        nonlocal calls
        calls += 1
        if calls == 7:  # Setup gates, source open, normalize process, normalized PCM open.
            raise denial

    def fake_normalize(command, *, cancel_event=None):
        _write_pcm(Path(command[-1]), sample_rate=24000)
        return None

    with (
        mock.patch.object(audio_assembly.wave, "open", side_effect=tracked_wave_open),
        mock.patch.object(
            audio_assembly, "run_media_process", side_effect=fake_normalize
        ) as process,
    ):
        with pytest.raises(PermissionError) as caught:
            assemble_audio_plan(_plan((source,)), tmp_path / "output.wav", before_io=before_io)

    assert caught.value is denial
    assert calls == 7
    process.assert_called_once()
    assert normalized_paths == []


def test_encoding_denial_prevents_media_process_launch(tmp_path: Path) -> None:
    source = _write_pcm(tmp_path / "source.wav")
    denial = RuntimeError("encoding denied")
    calls = 0

    def before_io() -> None:
        nonlocal calls
        calls += 1
        if calls == 6:  # Setup gates, source open, then encoding process.
            raise denial

    with mock.patch.object(audio_assembly, "run_media_process") as process:
        with pytest.raises(RuntimeError) as caught:
            assemble_audio_plan(
                _plan((source,), output_format="mp3"),
                tmp_path / "output.mp3",
                before_io=before_io,
            )

    assert caught.value is denial
    assert calls == 6
    process.assert_not_called()


def test_pydub_late_denial_prevents_second_source_read(tmp_path: Path) -> None:
    first = _write_pcm(tmp_path / "first.wav")
    second = _write_pcm(tmp_path / "second.wav")
    denial = RuntimeError("second Pydub read denied")
    calls = 0
    source_reads: list[Path] = []
    real_load = audio_assembly.load_local_audio_segment

    def tracked_load(file, *args, **kwargs):
        source_reads.append(Path(file))
        return real_load(file, *args, **kwargs)

    def before_io() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise denial

    with mock.patch.object(audio_assembly, "load_local_audio_segment", side_effect=tracked_load):
        with pytest.raises(RuntimeError) as caught:
            assemble_audio_plan(
                _plan((first, second)),
                tmp_path / "output.wav",
                backend=PYDUB_BACKEND,
                before_io=before_io,
            )

    assert caught.value is denial
    assert calls == 3
    assert source_reads == [first]


def test_pydub_export_denial_prevents_export_call(tmp_path: Path) -> None:
    source = _write_pcm(tmp_path / "source.wav")
    denial = RuntimeError("Pydub export denied")
    calls = 0

    def before_io() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:  # Outer check, source read, then export.
            raise denial

    with mock.patch.object(audio_assembly.AudioSegment, "export") as export:
        with pytest.raises(RuntimeError) as caught:
            assemble_audio_plan(
                _plan((source,)),
                tmp_path / "output.wav",
                backend=PYDUB_BACKEND,
                before_io=before_io,
            )

    assert caught.value is denial
    assert calls == 3
    export.assert_not_called()


@pytest.mark.parametrize(
    ("backend", "denial_call"),
    ((STREAMING_BACKEND, 6), (PYDUB_BACKEND, 4)),
)
def test_final_replacement_denial_preserves_existing_destination(
    tmp_path: Path,
    backend: str,
    denial_call: int,
) -> None:
    source = _write_pcm(tmp_path / "source.wav")
    destination = tmp_path / f"{backend}-output.wav"
    destination.write_bytes(b"existing destination")
    denial = PermissionError("final replacement denied")
    calls = 0
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def before_io() -> None:
        nonlocal calls
        calls += 1
        if calls == denial_call:
            raise denial

    def tracked_replace(source_path, destination_path):
        replacements.append((Path(source_path), Path(destination_path)))
        return real_replace(source_path, destination_path)

    def fake_export(_audio, out_f=None, **_kwargs):
        assert out_f is not None
        Path(out_f).write_bytes(b"encoded temporary")
        return BytesIO()

    patches = [mock.patch.object(audio_assembly.os, "replace", side_effect=tracked_replace)]
    if backend == PYDUB_BACKEND:
        patches.append(
            mock.patch.object(
                audio_assembly.AudioSegment,
                "export",
                autospec=True,
                side_effect=fake_export,
            )
        )

    with patches[0]:
        if backend == PYDUB_BACKEND:
            export_patch = patches[1]
        else:
            export_patch = mock.patch.object(audio_assembly.AudioSegment, "export")
        with export_patch as export:
            with pytest.raises(PermissionError) as caught:
                assemble_audio_plan(
                    _plan((source,)),
                    destination,
                    backend=backend,
                    before_io=before_io,
                )

    assert caught.value is denial
    assert calls == denial_call
    assert destination.read_bytes() == b"existing destination"
    assert all(target != destination for _, target in replacements)
    if backend == PYDUB_BACKEND:
        export.assert_called_once()
    else:
        export.assert_not_called()
