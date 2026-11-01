"""Synthetic boundary tests for video-tail access authorization."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from pandrator.web import video_tail_fast


def _stream(**overrides: object) -> dict[str, object]:
    stream: dict[str, object] = {
        "codec_type": "video",
        "codec_name": "h264",
        "profile": "High",
        "pix_fmt": "yuv420p",
        "width": 2,
        "height": 2,
        "sample_aspect_ratio": "1:1",
        "level": 31,
        "r_frame_rate": "25/1",
        "avg_frame_rate": "25/1",
        "time_base": "1/12800",
        "start_time": "0.000000",
        "duration": "30.000000",
        "nb_frames": "750",
    }
    stream.update(overrides)
    return stream


def _setup_synthetic_attempt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic source")
    destination = tmp_path / "destination.mp4"
    destination.write_bytes(b"previous destination")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    counters = {
        "probes": 0,
        "commands": [],
        "list_writes": 0,
        "md5_reads": 0,
        "promotions": 0,
    }
    source_stream = _stream()
    output_stream = _stream(duration="30.400000", nb_frames="760")
    packets = [
        {
            "pts_time": f"{29.0 + 0.04 * index:.2f}",
            "dts_time": f"{29.0 + 0.04 * index:.2f}",
            "duration_time": "0.04",
            "flags": "K",
        }
        for index in range(35)
    ]

    def fake_probe(command: list[str], **_kwargs: object) -> SimpleNamespace:
        counters["probes"] += 1
        probe_args = command[1:]
        if "-show_packets" in probe_args:
            payload = {"packets": packets}
        elif "-show_streams" in probe_args:
            stream = source_stream if probe_args[-1] == str(source) else output_stream
            payload = {"streams": [stream], "format": {}}
        else:
            raise AssertionError(f"unexpected synthetic probe: {command!r}")
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(video_tail_fast, "_ffprobe_for_ffmpeg", lambda _path: "stub-ffprobe")
    monkeypatch.setattr(
        video_tail_fast,
        "subprocess",
        SimpleNamespace(
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
            run=fake_probe,
        ),
    )

    original_write_text = Path.write_text

    def tracked_write_text(path: Path, *args: Any, **kwargs: Any) -> int:
        if path.name == ".tailfast-join.ffconcat":
            counters["list_writes"] += 1
        return original_write_text(path, *args, **kwargs)

    original_read_text = Path.read_text

    def tracked_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.name.endswith(".md5"):
            counters["md5_reads"] += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", tracked_write_text)
    monkeypatch.setattr(Path, "read_text", tracked_read_text)

    original_os = video_tail_fast.os

    def tracked_replace(source_path: Path, destination_path: Path) -> None:
        counters["promotions"] += 1
        original_os.replace(source_path, destination_path)

    monkeypatch.setattr(video_tail_fast, "os", SimpleNamespace(replace=tracked_replace))

    md5_payload = "0, 1, 0, 1, 2, deadbeef\n"

    def run_command(command: list[str], _cancel_event: threading.Event) -> None:
        counters["commands"].append(command)
        output_path = command[-1]
        if output_path == "-":
            return
        path = Path(output_path)
        output_format = None
        if "-f" in command:
            output_format = command[command.index("-f") + 1]
        if output_format == "rawvideo":
            path.write_bytes(b"\0" * 6)
        elif output_format == "framemd5":
            path.write_text(md5_payload, encoding="utf-8")
        else:
            path.write_bytes(b"synthetic output")

    return {
        "source": source,
        "destination": destination,
        "scratch": scratch,
        "counters": counters,
        "run_command": run_command,
    }


def _run_attempt(
    context: dict,
    before_io: Callable[[], None] | None = None,
) -> bool:
    return video_tail_fast.try_fast_video_tail(
        context["source"],
        context["destination"],
        scratch_dir=context["scratch"],
        extra_seconds=0.4,
        ffmpeg_executable="stub-ffmpeg",
        cancel_event=threading.Event(),
        run_command=context["run_command"],
        before_io=before_io,
    )


def _callback_error(kind: str) -> BaseException:
    if kind == "oserror":
        return OSError("access revoked")
    if kind == "valueerror":
        return ValueError("access revoked")
    return subprocess.CalledProcessError(7, ["stub-ffmpeg"])


@pytest.mark.parametrize(
    ("denied_call", "kind", "commands", "probes", "list_writes", "md5_reads"),
    [
        (1, "oserror", 0, 0, 0, 0),  # source stream probe
        (2, "valueerror", 0, 1, 0, 0),  # body command
        (3, "oserror", 1, 1, 0, 0),  # last-frame command
        (4, "valueerror", 2, 1, 0, 0),  # tail encode command
        (5, "oserror", 3, 1, 0, 0),  # concat-list content write
        (6, "valueerror", 3, 1, 1, 0),  # concat command
        (7, "oserror", 4, 1, 1, 0),  # joined-output stream probe
        (8, "valueerror", 4, 2, 1, 0),  # packet-window probe
        (9, "calledprocesserror", 4, 3, 1, 0),  # source framemd5 command
        (10, "oserror", 5, 3, 1, 0),  # joined framemd5 command
        (11, "oserror", 6, 3, 1, 0),  # source framemd5 content read
        (12, "valueerror", 6, 3, 1, 1),  # joined framemd5 content read
        (13, "calledprocesserror", 6, 3, 1, 2),  # strict decode command
        (14, "oserror", 7, 3, 1, 2),  # destination promotion
    ],
)
def test_callback_failure_stops_at_each_io_boundary_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    denied_call: int,
    kind: str,
    commands: int,
    probes: int,
    list_writes: int,
    md5_reads: int,
) -> None:
    context = _setup_synthetic_attempt(monkeypatch, tmp_path)
    error = _callback_error(kind)
    callback_calls = 0

    def before_io() -> None:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == denied_call:
            raise error

    with pytest.raises(type(error)) as caught:
        _run_attempt(context, before_io)

    assert caught.value is error
    assert callback_calls == denied_call
    assert len(context["counters"]["commands"]) == commands
    assert context["counters"]["probes"] == probes
    assert context["counters"]["list_writes"] == list_writes
    assert context["counters"]["md5_reads"] == md5_reads
    assert context["counters"]["promotions"] == 0
    assert context["destination"].read_bytes() == b"previous destination"
    assert list(context["scratch"].iterdir()) == []


def test_success_calls_callback_at_every_boundary_and_promotes_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _setup_synthetic_attempt(monkeypatch, tmp_path)
    callback_calls = 0

    def before_io() -> None:
        nonlocal callback_calls
        callback_calls += 1

    assert _run_attempt(context, before_io)
    assert callback_calls == 14
    assert len(context["counters"]["commands"]) == 7
    assert context["counters"]["probes"] == 3
    assert context["counters"]["list_writes"] == 1
    assert context["counters"]["md5_reads"] == 2
    assert context["counters"]["promotions"] == 1
    assert context["destination"].read_bytes() == b"synthetic output"
    assert list(context["scratch"].iterdir()) == []


def test_omitting_callback_keeps_synthetic_fast_path_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _setup_synthetic_attempt(monkeypatch, tmp_path)

    assert _run_attempt(context)
    assert len(context["counters"]["commands"]) == 7
    assert context["counters"]["probes"] == 3
    assert context["destination"].read_bytes() == b"synthetic output"
