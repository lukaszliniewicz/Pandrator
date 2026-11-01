"""Restricted local audio decoding with PCM memory semantics preserved."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydub import AudioSegment
from pydub import audio_segment as pydub_audio_segment
from pydub import utils as pydub_utils
from pydub.exceptions import CouldntDecodeError

from pandrator.logic import local_audio_decode as decoder
from pandrator.logic.cancellable_process import run_cancellable
from pandrator.logic.media_input_policy import LOCAL_MEDIA_INPUT_OPTIONS

_NATIVE_AVAILABLE = all(
    shutil.which(os.environ.get(f"PANDRATOR_{name.upper()}_EXE") or name)
    for name in ("ffmpeg", "ffprobe")
)


def _wav(path: Path, data: bytes, *, width: int, rate: int = 22050, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(data)


@pytest.fixture
def no_pydub_native(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Unrestricted pydub decoder/probe called"))
    monkeypatch.setattr(AudioSegment, "from_file", forbidden)
    monkeypatch.setattr(AudioSegment, "from_wav", forbidden)
    monkeypatch.setattr(pydub_audio_segment, "mediainfo_json", forbidden)
    monkeypatch.setattr(pydub_utils, "mediainfo_json", forbidden)
    return forbidden


def test_pcm16_fastpath_preserves_exact_stereo_bytes_without_native_calls(
    tmp_path, monkeypatch, no_pydub_native
):
    path = tmp_path / "PCM16 stereo.wav"
    raw = struct.pack("<8h", -32768, 32767, -1, 1, 0, 0, 1234, -1234)
    _wav(path, raw, width=2, rate=44100, channels=2)
    runner = Mock(side_effect=AssertionError("PCM WAV opened a native process"))
    monkeypatch.setattr(decoder, "run_cancellable", runner)
    segment = decoder.load_local_audio_segment(path)
    assert segment.raw_data == raw
    assert (segment.sample_width, segment.frame_rate, segment.channels) == (2, 44100, 2)
    runner.assert_not_called()
    no_pydub_native.assert_not_called()


def test_pcm8_fastpath_biases_unsigned_wav_samples_once(tmp_path, monkeypatch, no_pydub_native):
    path = tmp_path / "pcm8.wav"
    _wav(path, bytes([0, 1, 127, 128, 129, 255]), width=1, rate=8000)
    runner = Mock(side_effect=AssertionError("PCM8 opened a native process"))
    monkeypatch.setattr(decoder, "run_cancellable", runner)
    segment = decoder.load_local_audio_segment(str(path))
    assert list(segment.get_array_of_samples()) == [-128, -127, -1, 0, 1, 127]
    assert segment.sample_width == 1 and segment.frame_rate == 8000
    runner.assert_not_called()
    no_pydub_native.assert_not_called()


def test_pcm24_fastpath_retains_pydub_precision_promotion(tmp_path, monkeypatch, no_pydub_native):
    path = tmp_path / "pcm24.wav"
    values = [-8388608, -123, 0, 123, 8388607]
    raw = b"".join(value.to_bytes(3, "little", signed=True) for value in values)
    _wav(path, raw, width=3, rate=48000)
    runner = Mock(side_effect=AssertionError("PCM24 opened a native process"))
    monkeypatch.setattr(decoder, "run_cancellable", runner)
    segment = decoder.load_local_audio_segment(path)
    promoted = b"".join(
        (b"\xff" if raw[index + 2] >= 128 else b"\x00") + raw[index : index + 3]
        for index in range(0, len(raw), 3)
    )
    assert segment.raw_data == promoted
    assert (segment.sample_width, segment.frame_rate, segment.channels) == (4, 48000, 1)
    runner.assert_not_called()
    no_pydub_native.assert_not_called()


@pytest.mark.skipif(not _NATIVE_AVAILABLE, reason="Local FFmpeg/FFprobe required")
@pytest.mark.parametrize(
    "codec,suffix,width,rate",
    [("libmp3lame", ".mp3", 2, 22050), ("flac", ".flac", 2, 44100), ("flac", ".flac", 3, 48000)],
)
def test_actual_compressed_roundtrip_retains_width_rate_channels_and_two_processes(
    tmp_path, monkeypatch, no_pydub_native, codec, suffix, width, rate
):
    source = tmp_path / "synthetic source.wav"
    values = [int(6000 * ((index % 64) - 32) / 32) for index in range(4096 * 2)]
    raw = b"".join(value.to_bytes(width, "little", signed=True) for value in values)
    _wav(source, raw, width=width, rate=rate, channels=2)
    compressed = tmp_path / f"input with spaces ' quote{suffix}"
    run_cancellable(
        [
            decoder._executable("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            *LOCAL_MEDIA_INPUT_OPTIONS,
            "-i",
            str(source),
            "-c:a",
            codec,
            str(compressed),
        ],
        check=True,
        capture_output=True,
        text=False,
        stdin=subprocess.DEVNULL,
    )
    expected = decoder.load_local_audio_segment(source)
    runner = Mock(wraps=run_cancellable)
    monkeypatch.setattr(decoder, "run_cancellable", runner)
    segment = decoder.load_local_audio_segment(compressed)
    assert segment.frame_rate == rate and segment.channels == 2
    assert segment.sample_width == (4 if width == 3 else width)
    decoded_data = segment.raw_data
    assert isinstance(decoded_data, bytes) and len(decoded_data) > 0
    if codec == "flac":
        assert segment.raw_data == expected.raw_data
    assert runner.call_count == 2
    probe, decode = [call.args[0] for call in runner.call_args_list]
    assert probe[-1] == str(compressed.resolve())
    assert tuple(probe[-5:-1]) == LOCAL_MEDIA_INPUT_OPTIONS
    position = decode.index("-i")
    assert tuple(decode[position - 4 : position]) == LOCAL_MEDIA_INPUT_OPTIONS
    assert decode[position + 1] == str(compressed.resolve()) and decode[-1] == "pipe:1"
    assert all(
        call.kwargs["text"] is False and call.kwargs["stdin"] == subprocess.DEVNULL
        for call in runner.call_args_list
    )
    no_pydub_native.assert_not_called()


@pytest.mark.skipif(not _NATIVE_AVAILABLE, reason="Local FFmpeg/FFprobe required")
def test_hls_playlist_is_rejected_without_unrestricted_retry(tmp_path, no_pydub_native):
    control = tmp_path / "control.wav"
    _wav(control, struct.pack("<8h", *range(8)) * 2000, width=2, rate=16000)
    child = tmp_path / "private.ts"
    run_cancellable(
        [
            decoder._executable("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            *LOCAL_MEDIA_INPUT_OPTIONS,
            "-i",
            str(control),
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(child),
        ],
        check=True,
        capture_output=True,
        text=False,
        stdin=subprocess.DEVNULL,
    )
    path = tmp_path / "owned.m3u8"
    path.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{child.name}\n#EXT-X-ENDLIST\n")
    with pytest.raises(CouldntDecodeError) as raised:
        decoder.load_local_audio_segment(path)
    assert str(raised.value) == "Could not decode local audio."
    assert str(child) not in str(raised.value)
    no_pydub_native.assert_not_called()


def test_nonexistent_file_stays_file_error_without_native_fallback(
    tmp_path, monkeypatch, no_pydub_native
):
    runner = Mock(side_effect=AssertionError("Missing file started native decoding"))
    monkeypatch.setattr(decoder, "run_cancellable", runner)
    with pytest.raises(FileNotFoundError):
        decoder.load_local_audio_segment(tmp_path / "absent.wav")
    runner.assert_not_called()
    no_pydub_native.assert_not_called()


@pytest.mark.parametrize(
    "stream,width,raw",
    [
        ({"sample_fmt": "u8", "bits_per_sample": 8}, 1, b"\x00\x80\xff"),
        ({"codec_name": "mp3", "sample_fmt": "fltp", "bits_per_sample": 32}, 2, b"\x01\x00"),
        (
            {"sample_fmt": "s32", "bits_per_raw_sample": "24", "bits_per_sample": 32},
            3,
            b"\x01\x00\x00",
        ),
        ({"sample_fmt": "dblp", "bits_per_sample": 0}, 4, b"\x01\x00\x00\x00"),
    ],
)
def test_native_precision_selection_and_raw_first_audio_contract(
    tmp_path, monkeypatch, no_pydub_native, stream, width, raw
):
    path = tmp_path / "compressed-input"
    path.write_bytes(b"not a PCM WAV")
    stream = {"codec_name": "synthetic", "sample_rate": "32000", "channels": 1, **stream}
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=json.dumps({"streams": [stream]}).encode()),
            subprocess.CompletedProcess([], 0, stdout=raw),
        ]
    )
    monkeypatch.setattr(decoder, "run_cancellable", runner)
    segment = decoder.load_local_audio_segment(path)
    assert (segment.sample_width, segment.frame_rate, segment.channels) == (
        4 if width == 3 else width,
        32000,
        1,
    )
    command = runner.call_args_list[1].args[0]
    assert command[command.index("-map") + 1] == "0:a:0"
    assert command[command.index("-c:a") + 1] == ("pcm_u8" if width == 1 else f"pcm_s{width * 8}le")
    assert command[command.index("-f") + 1] == ("u8" if width == 1 else f"s{width * 8}le")
    assert command[command.index("-ar") + 1] == "32000" and command[command.index("-ac") + 1] == "1"
    if width == 1:
        assert list(segment.get_array_of_samples()) == [-128, 0, 127]
    no_pydub_native.assert_not_called()


@pytest.mark.parametrize("failure", ["metadata", "alignment", "rate"])
def test_malformed_native_results_are_bounded_public_decode_errors(
    tmp_path, monkeypatch, no_pydub_native, failure
):
    path = tmp_path / "compressed-input"
    path.write_bytes(b"not a PCM WAV")
    metadata = {
        "streams": [
            {
                "codec_name": "flac",
                "sample_fmt": "s16",
                "sample_rate": "nan" if failure == "rate" else "16000",
                "channels": 1,
                "bits_per_raw_sample": 16,
            }
        ]
    }
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess(
                [],
                0,
                stdout=b"not JSON" if failure == "metadata" else json.dumps(metadata).encode(),
            ),
            subprocess.CompletedProcess([], 0, stdout=b"x"),
        ]
    )
    monkeypatch.setattr(decoder, "run_cancellable", runner)
    with pytest.raises(CouldntDecodeError) as raised:
        decoder.load_local_audio_segment(path)
    assert str(raised.value) == "Could not decode local audio."
    assert runner.call_count == (2 if failure == "alignment" else 1)
    no_pydub_native.assert_not_called()


def test_truncated_pcm_uses_only_restricted_native_fallback(tmp_path, monkeypatch, no_pydub_native):
    path = tmp_path / "truncated.wav"
    _wav(path, struct.pack("<4h", 1, 2, 3, 4), width=2)
    path.write_bytes(path.read_bytes()[:-2])
    metadata = {
        "streams": [
            {
                "codec_name": "pcm_s16le",
                "sample_fmt": "s16",
                "sample_rate": "22050",
                "channels": 1,
                "bits_per_sample": 16,
            }
        ]
    }
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=json.dumps(metadata).encode()),
            subprocess.CompletedProcess([], 0, stdout=b"\x01\x00"),
        ]
    )
    monkeypatch.setattr(decoder, "run_cancellable", runner)
    assert decoder.load_local_audio_segment(path).raw_data == b"\x01\x00"
    assert runner.call_count == 2
    no_pydub_native.assert_not_called()
