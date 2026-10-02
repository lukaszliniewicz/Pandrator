import shutil
import subprocess
import sys
import threading
import time

import pytest

from pandrator.logic.dubbing.video_muxing import build_removal_only_video_command
from pandrator.web.media_process import (
    MediaProcessCancelled,
    MediaProcessError,
    run_media_process,
)


def _python_command(source):
    return [sys.executable, "-c", source]


def test_progress_callback_receives_complete_records_and_preserves_capture():
    command = _python_command(
        "import sys\n"
        "sys.stdout.buffer.write(b'unstructured output\\n'"
        "+ b'out_time_us=1000000\\nprogress=continue\\n'"
        "+ b'out_time_ms=not-a-number\\nprogress=end\\n')\n"
    )
    records = []

    result = run_media_process(
        command,
        capture_stdout=True,
        progress_callback=records.append,
    )

    assert [record["progress"] for record in records] == ["continue", "end"]
    assert records[0]["out_time_us"] == "1000000"
    assert "unstructured output" not in records[0]
    assert records[1]["out_time_ms"] == "not-a-number"
    assert result.stdout == (
        "unstructured output\n"
        "out_time_us=1000000\n"
        "progress=continue\n"
        "out_time_ms=not-a-number\n"
        "progress=end\n"
    )


def test_progress_callback_cancellation_stops_process_and_cleans_reader(tmp_path):
    marker = tmp_path / "process-finished"
    command = _python_command(
        "import pathlib, time\n"
        "print('out_time_us=1000000', flush=True)\n"
        "print('progress=continue', flush=True)\n"
        "time.sleep(10)\n"
        f"pathlib.Path({str(marker)!r}).write_text('finished')\n"
    )
    cancel_event = threading.Event()
    started = time.monotonic()

    with pytest.raises(MediaProcessCancelled):
        run_media_process(
            command,
            cancel_event=cancel_event,
            progress_callback=lambda _record: cancel_event.set(),
        )

    assert time.monotonic() - started < 3
    assert not marker.exists()
    assert not any(
        thread.name == "pandrator-media-progress-reader" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_progress_process_failure_keeps_stderr_diagnostic():
    command = _python_command(
        "import sys\n"
        "print('out_time_us=500000', flush=True)\n"
        "print('progress=end', flush=True)\n"
        "print('conversion failed intentionally', file=sys.stderr, flush=True)\n"
        "sys.exit(7)\n"
    )
    records = []

    with pytest.raises(MediaProcessError, match="conversion failed intentionally"):
        run_media_process(command, progress_callback=records.append)

    assert records[0]["out_time_us"] == "500000"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is unavailable")
def test_real_synthetic_ffmpeg_progress_timestamps_are_monotonic(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    source = tmp_path / "source.mp4"
    output = tmp_path / "edited.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10:duration=1.2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    command = build_removal_only_video_command(
        str(source),
        str(output),
        [(0, 1000)],
        has_audio=False,
        include_progress=True,
    )
    command.insert(command.index("-i"), "-re")
    records = []

    result = run_media_process(command, progress_callback=records.append)

    timestamps = []
    for record in records:
        # FFmpeg may report N/A before it has emitted a timestamped frame.
        # Ignore only that unavailable value; malformed numbers
        # should still fail this native progress check.
        for field in ("out_time_us", "out_time_ms"):
            value = record.get(field)
            if value is None or value == "N/A":
                continue
            timestamps.append(int(value))
            break
    assert result.returncode == 0
    assert output.is_file()
    assert timestamps
    assert timestamps == sorted(timestamps)
    assert records[-1]["progress"] == "end"


def test_watchdog_stops_process_and_progress_reader(tmp_path):
    from pandrator.web.media_process import MediaProcessTimeout

    marker = tmp_path / "finished"
    command = _python_command(
        "import pathlib, time\n"
        "print('out_time_us=1000', flush=True)\n"
        "print('progress=continue', flush=True)\n"
        "time.sleep(10)\n"
        f"pathlib.Path({str(marker)!r}).write_text('finished')\n"
    )
    started = time.monotonic()
    with pytest.raises(MediaProcessTimeout, match="timeout"):
        run_media_process(command, timeout_seconds=0.2, progress_callback=lambda _record: None)
    assert time.monotonic() - started < 3
    assert not marker.exists()
    assert not any(thread.name == "pandrator-media-progress-reader" and thread.is_alive()
                   for thread in threading.enumerate())


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_watchdog_requires_positive_finite_timeout(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        run_media_process(_python_command("pass"), timeout_seconds=timeout)


def test_watchdog_default_and_explicit_deadline_allow_success():
    assert run_media_process(_python_command("pass")).returncode == 0
    assert run_media_process(_python_command("pass"), timeout_seconds=2).returncode == 0
