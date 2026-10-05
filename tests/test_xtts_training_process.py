"""Real disposable trainer process lifetime controls, without GPU/provider work."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import psutil
import pytest

from pandrator.logic import xtts_trainer_handler as trainer


def running(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_for_file(path: Path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return path.read_text()
        time.sleep(0.02)
    raise AssertionError(f"Process fixture did not become ready: {path.name}")


@pytest.mark.skipif(os.name == "nt", reason="POSIX native process-group fixture")
def test_silent_training_cancellation_reaps_parent_and_child_and_preserves_unrelated(tmp_path):
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"fixture source")
    ready = tmp_path / "ready"
    script = (
        "import subprocess,sys,time;from pathlib import Path;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        f"Path({str(ready)!r}).write_text(str(child.pid));time.sleep(120)"
    )
    original_popen = subprocess.Popen
    launched = []
    unrelated = original_popen(
        [sys.executable, "-c", "import time;time.sleep(120)"], start_new_session=True
    )
    cancel = threading.Event()
    results = []
    paths = {
        "trainer_dir": str(tmp_path),
        "xtts_models_dir": str(tmp_path / "models"),
        "pixi_executable": "pixi",
        "trainer_manifest": "pixi.toml",
        "trainer_script": "train.py",
    }

    def launch(_command, **kwargs):
        # Baseline did not isolate the group; force it solely so the fixture can
        # clean up without signaling the test runner. Acceptance checks the
        # runner's own launch settings separately.
        kwargs["start_new_session"] = True
        process = original_popen([sys.executable, "-u", "-c", script], **kwargs)
        launched.append(process)
        return process

    def train():
        try:
            results.append(
                trainer.start_training(
                    {"model_name": "narrator", "source_audio_path": str(audio)}, stop_event=cancel
                )
            )
        except BaseException as error:
            results.append(error)

    worker = threading.Thread(target=train, daemon=True)
    try:
        with (
            mock.patch.object(trainer, "get_training_paths", return_value=paths),
            mock.patch.object(
                trainer, "validate_training_environment", return_value=(True, "ready")
            ),
            mock.patch.object(
                trainer, "_build_trainer_subprocess_env", return_value=dict(os.environ)
            ),
            mock.patch.object(subprocess, "Popen", side_effect=launch),
            mock.patch.object(trainer, "_copy_trained_model") as copy_model,
        ):
            worker.start()
            child_pid = int(wait_for_file(ready))
            assert running(child_pid)
            started = time.monotonic()
            cancel.set()
            worker.join(timeout=6.0)
            assert not worker.is_alive(), "Silent trainer ignored cancellation for six seconds"
            assert time.monotonic() - started < 6.0
            assert results == [(False, "XTTS training was canceled.")]
            assert launched[0].poll() is not None
            assert not running(child_pid)
            assert unrelated.poll() is None
            copy_model.assert_not_called()
    finally:
        cancel.set()
        for process in launched:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        worker.join(timeout=3)
        unrelated.kill()
        unrelated.wait(timeout=3)
    assert audio.read_bytes() == b"fixture source"


class ProcessHarness:
    def __init__(self, root):
        self.root = root
        self.roots = []
        self.pid_files = []
        self.launch_options = []
        self.env = {**os.environ, "PANDRATOR_XTTS_TRAINING_TOKEN": "caller-owned-marker"}

    def pid_file(self, name="child.pid"):
        path = self.root / name
        self.pid_files.append(path)
        return path

    def invoke(self, script, *, cancel=None, callback=None):
        from pandrator.logic.xtts_training_process import run_training_process

        original_popen = subprocess.Popen
        results = []

        def launch(command, **kwargs):
            self.launch_options.append(kwargs.copy())
            assert kwargs.get("start_new_session") is True, "Trainer must own its POSIX session"
            process = original_popen(command, **kwargs)
            self.roots.append(process)
            return process

        def invoke():
            try:
                results.append(
                    run_training_process(
                        [sys.executable, "-u", "-c", script],
                        cwd=str(self.root),
                        env=self.env,
                        cancel_event=cancel,
                        output_callback=callback,
                    )
                )
            except BaseException as error:
                results.append(error)

        worker = threading.Thread(target=invoke, daemon=True)
        try:
            with mock.patch.object(subprocess, "Popen", side_effect=launch):
                worker.start()
                worker.join(timeout=8)
                assert not worker.is_alive(), "Trainer lifetime exceeded bounded fixture deadline"
        finally:
            if worker.is_alive():
                self.cleanup()
                worker.join(timeout=3)
        assert self.env["PANDRATOR_XTTS_TRAINING_TOKEN"] == "caller-owned-marker"
        for options in self.launch_options:
            assert options["env"]["PANDRATOR_XTTS_TRAINING_TOKEN"] != "caller-owned-marker"
            assert options["cwd"] == str(self.root)
        assert len(results) == 1
        if isinstance(results[0], BaseException):
            raise results[0]
        return results[0]

    def cleanup(self):
        for path in self.pid_files:
            if path.exists():
                try:
                    process = psutil.Process(int(path.read_text()))
                    process.kill()
                except psutil.NoSuchProcess:
                    pass
        for process in self.roots:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)


@pytest.fixture
def process_harness(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX native process-family fixture; Windows execution remains separate")
    harness = ProcessHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.cleanup()


def test_streamed_progress_preserves_split_utf8_and_final_unterminated_line(process_harness):
    done = process_harness.root / "done"
    script = (
        "import sys,time;from pathlib import Path;"
        "sys.stdout.buffer.write(b'  Epoch 1  \\n');sys.stdout.flush();time.sleep(.15);"
        "sys.stdout.buffer.write(b'caf\\xc3');sys.stdout.flush();time.sleep(.15);"
        "sys.stdout.buffer.write(b'\\xa9\\nfinal');sys.stdout.flush();time.sleep(.2);"
        f"Path({str(done)!r}).write_text('done')"
    )
    lines = []
    before_exit = []

    def progress(line):
        lines.append(line)
        before_exit.append(not done.exists())

    assert process_harness.invoke(script, callback=progress) == 0
    assert lines == ["Epoch 1", "café", "final"]
    assert before_exit[:2] == [True, True]
    assert all(process.poll() == 0 for process in process_harness.roots)
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("xtts-training-output-")
    ]


def test_cancellation_escalates_stubborn_child_and_stops_reader(process_harness):
    from pandrator.logic.cancellable_process import ProcessCancelled

    ready = process_harness.pid_file()
    child_ready = process_harness.root / "child-ready"
    child_script = (
        "import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"Path({str(child_ready)!r}).write_text('ready');time.sleep(120)"
    )
    script = (
        "import subprocess,sys,time;from pathlib import Path;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_script!r}]);"
        f"Path({str(ready)!r}).write_text(str(child.pid));time.sleep(120)"
    )
    cancel = threading.Event()

    def request_cancel():
        wait_for_file(child_ready)
        cancel.set()

    requester = threading.Thread(target=request_cancel, daemon=True)
    requester.start()
    started = time.monotonic()
    with pytest.raises(ProcessCancelled):
        process_harness.invoke(script, cancel=cancel)
    requester.join(timeout=1)
    assert time.monotonic() - started < 6
    assert not running(int(ready.read_text()))
    assert all(process.poll() is not None for process in process_harness.roots)
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("xtts-training-output-")
    ]


def test_exited_launcher_detached_child_is_cleaned_before_return(process_harness):
    ready = process_harness.pid_file()
    child_script = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"
    script = (
        "import subprocess,sys,time;from pathlib import Path;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_script!r}],start_new_session=True);"
        f"Path({str(ready)!r}).write_text(str(child.pid));time.sleep(.2);print('launcher finished',flush=True)"
    )
    lines = []
    assert process_harness.invoke(script, callback=lines.append) == 0
    assert lines == ["launcher finished"]
    assert not running(int(ready.read_text()))
    assert all(process.poll() == 0 for process in process_harness.roots)
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("xtts-training-output-")
    ]


def test_progress_callback_failure_cleans_real_process_family(process_harness):
    ready = process_harness.pid_file()
    script = (
        "import subprocess,sys,time;from pathlib import Path;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        f"Path({str(ready)!r}).write_text(str(child.pid));print('epoch 1',flush=True);time.sleep(120)"
    )

    def refuse_progress(_line):
        raise ValueError("progress failure witness")

    with pytest.raises(ValueError, match="progress failure witness"):
        process_harness.invoke(script, callback=refuse_progress)
    assert not running(int(ready.read_text()))
    assert all(process.poll() is not None for process in process_harness.roots)
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("xtts-training-output-")
    ]


def test_nonzero_process_exit_preserves_output(process_harness):
    lines = []
    assert (
        process_harness.invoke(
            "import sys;print('failure detail',flush=True);sys.exit(7)", callback=lines.append
        )
        == 7
    )
    assert lines == ["failure detail"]


def test_preset_process_cancellation_does_not_launch(tmp_path):
    from pandrator.logic.cancellable_process import ProcessCancelled
    from pandrator.logic.xtts_training_process import run_training_process

    cancel = threading.Event()
    cancel.set()
    with mock.patch.object(subprocess, "Popen") as launch, pytest.raises(ProcessCancelled):
        run_training_process(
            [sys.executable, "-c", "raise RuntimeError('must not launch')"],
            cwd=str(tmp_path),
            env={},
            cancel_event=cancel,
            output_callback=None,
        )
    launch.assert_not_called()


def test_exited_launcher_tokenless_group_child_is_still_cleaned(process_harness):
    ready = process_harness.pid_file()
    script = (
        "import os,subprocess,sys,time;from pathlib import Path;env=dict(os.environ);"
        "env.pop('PANDRATOR_XTTS_TRAINING_TOKEN',None);"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],env=env);"
        f"Path({str(ready)!r}).write_text(str(child.pid));time.sleep(.2)"
    )
    assert process_harness.invoke(script) == 0
    assert not running(int(ready.read_text()))


def test_undiscoverable_pipe_holder_refuses_completion_with_bounded_wait(process_harness):
    ready = process_harness.pid_file()
    script = (
        "import os,subprocess,sys,time;from pathlib import Path;env=dict(os.environ);"
        "env.pop('PANDRATOR_XTTS_TRAINING_TOKEN',None);"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],env=env,start_new_session=True);"
        f"Path({str(ready)!r}).write_text(str(child.pid));time.sleep(.2)"
    )
    started = time.monotonic()
    with pytest.raises(
        RuntimeError,
        match="output pipe remained open",
    ):
        process_harness.invoke(script)
    assert time.monotonic() - started < 7
    # The worker cannot identify this deliberately escaped fixture. Prove refusal
    # instead of claiming containment, then perform independently authorized cleanup.
    assert running(int(ready.read_text()))
    process_harness.cleanup()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and any(
        thread.name.startswith("xtts-training-output-") for thread in threading.enumerate()
    ):
        time.sleep(0.02)
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("xtts-training-output-")
    ]


def test_buffered_progress_is_not_rejected_by_eof_timeout(process_harness):
    lines = []

    def slow_progress(line):
        lines.append(line)
        time.sleep(0.06)

    assert (
        process_harness.invoke(
            "for i in range(50): print(f'epoch {i}',flush=True)", callback=slow_progress
        )
        == 0
    )
    assert lines == [f"epoch {index}" for index in range(50)]


def test_burst_output_checks_cancellation_between_progress_callbacks(process_harness):
    from pandrator.logic.cancellable_process import ProcessCancelled

    cancel = threading.Event()
    lines = []

    def cancel_on_first_line(line):
        lines.append(line)
        cancel.set()
        time.sleep(0.05)

    started = time.monotonic()
    with pytest.raises(ProcessCancelled):
        process_harness.invoke(
            "for i in range(150): print(f'epoch {i}',flush=True)",
            cancel=cancel,
            callback=cancel_on_first_line,
        )
    assert len(lines) == 1
    assert time.monotonic() - started < 6
