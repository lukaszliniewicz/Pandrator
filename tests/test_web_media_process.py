import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

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
    assert not any(
        thread.name == "pandrator-media-progress-reader" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_never_exiting_child_is_stopped_at_the_deadline(monkeypatch):
    import psutil

    from pandrator.web import media_process
    from pandrator.web.media_process import MediaProcessTimeout

    spawned = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append((process, psutil.Process(process.pid).create_time()))
        return process

    monkeypatch.setattr(media_process.subprocess, "Popen", recording_popen)
    command = [
        getattr(sys, "_base_executable", sys.executable),
        "-c",
        "import time; time.sleep(600)",
    ]
    started = time.monotonic()
    with pytest.raises(MediaProcessTimeout, match="timeout"):
        run_media_process(command, timeout_seconds=2)
    assert time.monotonic() - started < 10
    process, created = spawned[0]
    assert process.poll() is not None
    try:
        assert psutil.Process(process.pid).create_time() != created
    except psutil.NoSuchProcess:
        pass


def test_stuck_child_still_surfaces_the_timeout(monkeypatch):
    from pandrator.web import media_process
    from pandrator.web.media_process import MediaProcessTimeout

    class StuckProcess:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            time.sleep(min(timeout or 0.01, 0.01))
            raise subprocess.TimeoutExpired("stuck", timeout)

    monkeypatch.setattr(media_process.subprocess, "Popen", lambda *a, **k: StuckProcess())
    with pytest.raises(MediaProcessTimeout):
        run_media_process(["stuck"], timeout_seconds=0.05)


def test_media_work_timeout_scales_with_duration():
    from pandrator.logic.media_input_policy import (
        MEDIA_PROBE_TIMEOUT_SECONDS,
        media_work_timeout_seconds,
    )

    assert MEDIA_PROBE_TIMEOUT_SECONDS == 60.0
    assert media_work_timeout_seconds(120) == 720.0
    assert media_work_timeout_seconds(0) == 600.0
    assert media_work_timeout_seconds(30, allowance_seconds=60.0) == 90.0
    assert media_work_timeout_seconds(-5) == 600.0
    for unknown in (None, float("inf"), float("-inf"), float("nan"), "bad"):
        assert media_work_timeout_seconds(unknown) == 3600.0


def test_probe_and_audible_scan_pass_their_deadlines(tmp_path):
    from pandrator.web import media_process

    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        return media_process.MediaProcessResult(
            0,
            json.dumps(
                {
                    "streams": [{"duration": "2.0", "sample_rate": "8000", "channels": 1}],
                    "format": {},
                }
            ),
            "",
        )

    with patch.object(media_process, "run_media_process", fake_run):
        media_process.probe_audio_stream(tmp_path / "a.wav", ffprobe_executable="ffprobe")
        media_process.find_first_audible_seconds(tmp_path / "a.wav", ffmpeg_executable="ffmpeg")
    assert calls[0]["timeout_seconds"] == 60.0
    assert calls[1]["timeout_seconds"] == 600.0 + 600.0


def test_waveform_window_passes_a_duration_scaled_deadline(tmp_path):
    from pandrator.web import media_process, waveform

    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        return media_process.MediaProcessResult(0, "", "")

    info = media_process.AudioStreamInfo(duration_ms=60000, sample_rate_hz=8000, channels=1)
    with (
        patch.object(waveform, "probe_audio_stream", return_value=info),
        patch.object(waveform, "run_media_process", fake_run),
        patch.object(waveform, "_parse_peak_levels", return_value=[0.0]),
    ):
        peaks = waveform.generate_waveform_peaks(
            tmp_path / "a.wav", start_ms=10000, end_ms=40000, max_points=128
        )
    assert (peaks.start_ms, peaks.end_ms) == (10000, 40000)
    assert calls[0]["timeout_seconds"] == 90.0


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 10**400, "bad", []])
def test_watchdog_requires_positive_finite_timeout(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        run_media_process(_python_command("pass"), timeout_seconds=timeout)


def test_watchdog_default_and_explicit_deadline_allow_success():
    assert run_media_process(_python_command("pass")).returncode == 0
    assert run_media_process(_python_command("pass"), timeout_seconds=2).returncode == 0


@pytest.mark.parametrize("field", ["duration", "sample_rate", "channels"])
@pytest.mark.parametrize("value", [None, "N/A", "inf", "-inf", "nan", 10**400, -(10**400)])
def test_probe_rejects_unusable_metadata_with_controlled_errors(field, value):
    from pandrator.web.media_process import MediaProcessResult, probe_audio_stream

    stream = {"duration": "1.25", "sample_rate": "24000", "channels": 2}
    stream[field] = value
    result = MediaProcessResult(0, json.dumps({"streams": [stream]}))
    with patch("pandrator.web.media_process.run_media_process", return_value=result):
        with pytest.raises(MediaProcessError, match="metadata"):
            probe_audio_stream("fixture.wav")


def test_probe_rejects_finite_duration_that_overflows_milliseconds():
    from pandrator.web.media_process import MediaProcessResult, probe_audio_stream

    result = MediaProcessResult(
        0,
        json.dumps(
            {
                "streams": [
                    {
                        "duration": 1e308,
                        "sample_rate": "24000",
                        "channels": 2,
                    }
                ]
            }
        ),
    )
    with patch("pandrator.web.media_process.run_media_process", return_value=result):
        with pytest.raises(MediaProcessError, match="invalid audio metadata"):
            probe_audio_stream("fixture.wav")


@pytest.mark.parametrize("value", [None, "N/A", "inf", 10**400])
def test_probe_uses_valid_container_duration_when_stream_duration_is_unusable(value):
    from pandrator.web.media_process import MediaProcessResult, probe_audio_stream

    result = MediaProcessResult(
        0,
        json.dumps(
            {
                "streams": [{"duration": value, "sample_rate": "24000", "channels": 2}],
                "format": {"duration": "1.25"},
            }
        ),
    )
    with patch("pandrator.web.media_process.run_media_process", return_value=result):
        info = probe_audio_stream("fixture.wav")
    assert (info.duration_ms, info.sample_rate_hz, info.channels) == (1250, 24000, 2)


def test_progress_capture_keeps_large_output_utf8_and_unterminated_final_record(tmp_path):
    message = "x" * 300000 + "👋"
    content = "unstructured output\nmessage=" + message + "\r\nprogress=end"
    records = []
    script = tmp_path / "writer.py"
    script.write_text(f"import sys;sys.stdout.buffer.write({content.encode()!r})", encoding="utf-8")
    result = run_media_process(
        [sys.executable, str(script)],
        capture_stdout=True,
        progress_callback=records.append,
    )
    assert result.stdout == content
    assert records == [{"message": message, "progress": "end"}]


@pytest.mark.skipif(os.name == "nt", reason="Disposable process-group fence requires POSIX")
@pytest.mark.parametrize("exit_early", [True, False])
def test_inherited_progress_handle_does_not_block_completion_or_watchdog(exit_early):
    import textwrap

    child_code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)']);"
        "print('out_time_us=1000000',flush=True);print('progress=end',flush=True);"
        + ("" if exit_early else "time.sleep(10)")
    )
    wrapper = textwrap.dedent(f"""
        import json,sys
        from pandrator.web.media_process import run_media_process,MediaProcessTimeout
        records=[]
        try:
            result=run_media_process([sys.executable,'-c',{child_code!r}],
                progress_callback=records.append,capture_stdout=True,timeout_seconds=.3)
            print(json.dumps({{'status':'completed','stdout':result.stdout,'records':records}}))
        except MediaProcessTimeout:
            print(json.dumps({{'status':'timed_out','records':records}}))
    """)
    process = subprocess.Popen(
        _python_command(wrapper),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    group = process.pid
    assert os.getpgid(group) == group
    started = time.monotonic()
    try:
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, stderr.decode()
        payload = json.loads(stdout)
        assert payload["status"] == ("completed" if exit_early else "timed_out")
        assert payload["records"][-1] == {"out_time_us": "1000000", "progress": "end"}
        assert time.monotonic() - started < 3
    finally:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=2)


def test_progress_callback_failure_preserves_error_and_reaps_direct_child():
    from pandrator.web import media_process

    native_popen = subprocess.Popen
    children = []
    failure = RuntimeError("callback failed")

    def launch(*args, **kwargs):
        child = native_popen(*args, **kwargs)
        children.append(child)
        return child

    def callback(_record):
        raise failure

    with patch.object(media_process.subprocess, "Popen", side_effect=launch):
        with pytest.raises(RuntimeError) as caught:
            run_media_process(
                _python_command("import time;print('progress=continue',flush=True);time.sleep(10)"),
                progress_callback=callback,
            )
    assert caught.value is failure
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.parametrize("text", [False, True])
@pytest.mark.parametrize("returncode", [0, 7])
def test_cancellable_runner_keeps_native_output_and_checked_error_contracts(text, returncode):
    from pandrator.logic.cancellable_process import run_cancellable

    command = _python_command(
        f"import sys;print('hello');print('detail',file=sys.stderr);sys.exit({returncode})"
    )
    options = dict(cancel_event=threading.Event(), capture_output=True, text=text, check=True)
    if returncode:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            run_cancellable(command, **options)
        result = caught.value
        assert result.returncode == returncode
        output = result.output
    else:
        result = run_cancellable(command, **options)
        assert result.returncode == 0
        output = result.stdout
    newline = os.linesep if text else os.linesep.encode()
    assert output == ("hello" if text else b"hello") + newline
    assert result.stderr == ("detail" if text else b"detail") + newline
