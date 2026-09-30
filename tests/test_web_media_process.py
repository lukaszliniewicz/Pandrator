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
        "sys.stdout.write('unstructured output\\n'"
        "+ 'out_time_us=1000000\\nprogress=continue\\n'"
        "+ 'out_time_ms=not-a-number\\nprogress=end\\n')\n"
        "sys.stdout.flush()\n"
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
    assert result.stdout.startswith("unstructured output\n")
    assert "progress=end\n" in result.stdout


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
        value = record.get("out_time_us") or record.get("out_time_ms")
        if value is not None:
            timestamps.append(int(value))
    assert result.returncode == 0
    assert output.is_file()
    assert timestamps
    assert timestamps == sorted(timestamps)
    assert records[-1]["progress"] == "end"
