from __future__ import annotations

import hashlib
import io
import os
import stat
import wave
from pathlib import Path

import pytest

from pandrator.runtime import DataPaths
from pandrator.web.speech_input_contracts import SpeechInputError
from pandrator.web.speech_input_files import (
    cleanup_files,
    create_staging_directory,
    file_identity,
    read_verified_file,
    stage_file,
    validate_wav,
)


def wav_bytes(value: int = 1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(bytes([value, 0]) * 400)
    return output.getvalue()


def test_bounded_actual_hash_and_identity(tmp_path: Path) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    path = paths.uploads / "source.wav"
    data = wav_bytes()
    path.write_bytes(data)
    identity = file_identity(paths, "uploads/source.wav")
    actual, digest, observed = read_verified_file(
        paths, "uploads/source.wav", limit=len(data), expected_identity=identity
    )
    assert actual == data and observed == identity
    assert digest == hashlib.sha256(data).hexdigest()
    validate_wav(data)
    with pytest.raises(SpeechInputError):
        read_verified_file(paths, "uploads/source.wav", limit=len(data) - 1)
    with pytest.raises(SpeechInputError, match="hash"):
        read_verified_file(paths, "uploads/source.wav", limit=len(data), expected_hash="0" * 64)
    path.write_bytes(wav_bytes(2))
    with pytest.raises(SpeechInputError, match="changed"):
        read_verified_file(paths, "uploads/source.wav", limit=len(data), expected_identity=identity)


@pytest.mark.parametrize(
    "relative",
    [
        "../outside",
        "/etc/passwd",
        "C:\\private.wav",
        "uploads/../source.wav",
        "uploads/./source.wav",
    ],
)
def test_path_traversal(tmp_path: Path, relative: str) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    with pytest.raises(SpeechInputError):
        read_verified_file(paths, relative, limit=100)


def test_rejects_symlinks_directories_and_fifo_without_blocking(tmp_path: Path) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    (paths.uploads / "real.wav").write_bytes(wav_bytes())
    (paths.uploads / "link.wav").symlink_to(paths.uploads / "real.wav")
    (paths.root / "alias").symlink_to(paths.uploads, target_is_directory=True)
    (paths.uploads / "directory.wav").mkdir()
    os.mkfifo(paths.uploads / "fifo.wav")
    for relative in (
        "uploads/link.wav",
        "alias/real.wav",
        "uploads/directory.wav",
        "uploads/fifo.wav",
    ):
        with pytest.raises(SpeechInputError):
            read_verified_file(paths, relative, limit=1024)


def test_private_staging_and_cleanup_retains_replacements_and_borrowed_files(
    tmp_path: Path,
) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    directory, identity = create_staging_directory(paths)
    first = stage_file(paths, directory, wav_bytes(), ".wav")
    second = stage_file(paths, directory, b"{}", ".json")
    assert stat.S_IMODE(paths.managed_path(directory).stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.managed_path(first.relative_path).stat().st_mode) == 0o600
    replacement = paths.uploads / "replacement"
    replacement.write_bytes(b"replacement")
    replacement.replace(paths.managed_path(first.relative_path))
    cleanup_files(paths, (first, second), directory, identity)
    assert paths.managed_path(first.relative_path).read_bytes() == b"replacement"
    assert not paths.managed_path(second.relative_path).exists()


def test_cleanup_preserves_replaced_directory(tmp_path: Path) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    directory, identity = create_staging_directory(paths)
    item = stage_file(paths, directory, b"ours", ".json")
    original = paths.managed_path(directory)
    original.rename(original.with_name(original.name + "-moved"))
    original.mkdir()
    (original / Path(item.relative_path).name).write_bytes(b"not ours")
    cleanup_files(paths, (item,), directory, identity)
    assert (original / Path(item.relative_path).name).read_bytes() == b"not ours"


@pytest.mark.parametrize("data", [b"RIFFinvalid", b"not wav", wav_bytes()[:-10]])
def test_invalid_or_truncated_wav(data: bytes) -> None:
    with pytest.raises(SpeechInputError):
        validate_wav(data)


def test_path_replacement_during_open_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    source = paths.uploads / "source.wav"
    source.write_bytes(wav_bytes())
    replacement = paths.uploads / "replacement.wav"
    replacement.write_bytes(wav_bytes(2))
    original = os.open
    changed = []

    def changing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "source.wav" and not changed:
            changed.append(True)
            replacement.replace(source)
        return original(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", changing_open)
    # Preserve the capability test after replacing the function object.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {changing_open})
    with pytest.raises(SpeechInputError, match="changed"):
        read_verified_file(paths, "uploads/source.wav", limit=1024)
