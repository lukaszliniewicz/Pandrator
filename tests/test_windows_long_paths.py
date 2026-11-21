from __future__ import annotations

import argparse
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import pandrator.runtime.windows_paths as windows_paths
from pandrator.runtime.windows_paths import (
    LONGEST_DATA_ROOT,
    data_root_length_warning,
    explain_long_path_error,
    warn_if_data_root_too_long,
)
from pandrator.web import cli
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker
from tests import web_test_support

ROOT_OK = "C:\\" + "r" * (LONGEST_DATA_ROOT - 3)
ROOT_LONG = ROOT_OK + "r"


def _limited(monkeypatch: pytest.MonkeyPatch, *, windows=True, long_paths=False):
    monkeypatch.setattr(windows_paths, "_is_windows", lambda: windows)
    monkeypatch.setattr(
        windows_paths, "_registry_long_paths_enabled", lambda: long_paths
    )


def _long_file(root: str, length: int = 262) -> str:
    base = root + "\\sessions\\"
    return base + "x" * (length - len(base))


def test_limit_constant() -> None:
    assert LONGEST_DATA_ROOT == 55
    assert len(ROOT_OK) == 55
    assert len(ROOT_LONG) == 56


def test_root_over_and_under_limit(monkeypatch) -> None:
    _limited(monkeypatch)
    message = data_root_length_warning(ROOT_LONG)
    assert message is not None
    assert ROOT_LONG in message
    assert "56" in message
    assert "55" in message
    assert data_root_length_warning(ROOT_OK) is None


def test_no_warning_with_long_paths_on_or_off_windows(monkeypatch) -> None:
    _limited(monkeypatch, long_paths=True)
    assert data_root_length_warning(ROOT_LONG) is None
    _limited(monkeypatch, windows=False)
    assert data_root_length_warning(ROOT_LONG) is None


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fake_winreg(*, value=None, missing=False):
    def open_key(hive, subkey):
        assert hive == "HKLM"
        assert subkey == r"SYSTEM\CurrentControlSet\Control\FileSystem"
        if missing:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return _FakeKey()

    def query_value(_key, name):
        assert name == "LongPathsEnabled"
        if value is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return value, 4

    return SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM", OpenKey=open_key, QueryValueEx=query_value
    )


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        (_fake_winreg(value=1), True),
        (_fake_winreg(value=0), False),
        (_fake_winreg(value=None), False),
        (_fake_winreg(missing=True), False),
        (None, False),
    ],
    ids=["enabled", "disabled", "value-missing", "key-missing", "no-winreg"],
)
def test_registry_reader(module, expected) -> None:
    # A missing key or value is Windows' default, long paths off.
    with patch.dict("sys.modules", {"winreg": module}):
        assert windows_paths._registry_long_paths_enabled() is expected


def test_long_paths_limited_reads_the_registry_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(windows_paths, "_is_windows", lambda: True)
    with patch.dict("sys.modules", {"winreg": _fake_winreg(value=0)}):
        assert windows_paths.long_paths_limited() is True
    with patch.dict("sys.modules", {"winreg": _fake_winreg(value=1)}):
        assert windows_paths.long_paths_limited() is False


def test_warning_is_logged_once_and_logging_errors_are_swallowed(
    monkeypatch, caplog
) -> None:
    _limited(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=windows_paths.logger.name):
        warn_if_data_root_too_long(ROOT_LONG)
    assert len([r for r in caplog.records if ROOT_LONG in r.getMessage()]) == 1

    def boom(*_args, **_kwargs):
        raise RuntimeError("logging broke")

    monkeypatch.setattr(windows_paths.logger, "warning", boom)
    warn_if_data_root_too_long(ROOT_LONG)


def test_chained_error_is_explained(monkeypatch) -> None:
    _limited(monkeypatch)
    path = _long_file(ROOT_OK)
    try:
        try:
            raise FileNotFoundError(2, "No such file", path)
        except FileNotFoundError as inner:
            raise RuntimeError("export failed") from inner
    except RuntimeError as error:
        message = explain_long_path_error(error, ROOT_OK)
    assert message is not None
    assert message.startswith("export failed")
    assert ROOT_OK in message
    assert "55" in message
    assert "259" in message


def test_filename2_only_at_exactly_260_characters(monkeypatch) -> None:
    _limited(monkeypatch)
    error = OSError(2, "No such file", "short.tmp", None, _long_file(ROOT_OK, 260))
    message = explain_long_path_error(error, ROOT_OK)
    assert message is not None
    assert "260 characters long" in message


def test_implicit_context_chain_is_explained(monkeypatch) -> None:
    _limited(monkeypatch)
    path = _long_file(ROOT_OK)
    try:
        try:
            raise FileNotFoundError(2, "No such file", path)
        except FileNotFoundError:
            raise RuntimeError("cleanup failed")  # noqa: B904 - implicit chaining is the case under test
    except RuntimeError as error:
        message = explain_long_path_error(error, ROOT_OK)
    assert message is not None
    assert message.startswith("cleanup failed")


def test_bytes_filename_is_explained(monkeypatch) -> None:
    _limited(monkeypatch)
    error = FileNotFoundError(2, "No such file", _long_file(ROOT_OK).encode())
    assert explain_long_path_error(error, ROOT_OK) is not None


def test_short_root_is_not_told_to_shorten_the_folder(monkeypatch) -> None:
    _limited(monkeypatch)
    root = "C:\\data"
    error = FileNotFoundError(2, "No such file", _long_file(root))
    message = explain_long_path_error(error, root)
    assert message is not None
    assert "259" in message
    assert "shorter data folder" not in message
    assert "at most 55" not in message
    assert "LongPathsEnabled" in message
    long_root_message = explain_long_path_error(
        FileNotFoundError(2, "No such file", _long_file(ROOT_LONG)), ROOT_LONG
    )
    assert long_root_message is not None
    assert "shorter data folder" in long_root_message


def test_filename_comparison_ignores_case_and_separators(monkeypatch) -> None:
    _limited(monkeypatch)
    path = _long_file(ROOT_OK).upper().replace("\\", "/")
    error = FileNotFoundError(2, "No such file", path)
    assert explain_long_path_error(error, ROOT_OK) is not None


def test_cyclic_chain_terminates(monkeypatch) -> None:
    _limited(monkeypatch)
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__context__ = second
    second.__context__ = first
    assert explain_long_path_error(first, ROOT_OK) is None


def test_unrelated_errors_are_not_translated(monkeypatch) -> None:
    _limited(monkeypatch)
    assert explain_long_path_error(ValueError("x"), ROOT_OK) is None
    short = FileNotFoundError(2, "No such file", ROOT_OK + "\\a.wav")
    assert explain_long_path_error(short, ROOT_OK) is None
    outside = FileNotFoundError(2, "No such file", _long_file("D:\\elsewhere"))
    assert explain_long_path_error(outside, ROOT_OK) is None
    exactly = FileNotFoundError(2, "No such file", _long_file(ROOT_OK, 259))
    assert explain_long_path_error(exactly, ROOT_OK) is None


def test_no_translation_when_long_paths_on_or_off_windows(monkeypatch) -> None:
    error = FileNotFoundError(2, "No such file", _long_file(ROOT_OK))
    _limited(monkeypatch, long_paths=True)
    assert explain_long_path_error(error, ROOT_OK) is None
    _limited(monkeypatch, windows=False)
    assert explain_long_path_error(error, ROOT_OK) is None


@pytest.fixture
def job_runtime(tmp_path: Path):
    paths = web_test_support.prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        yield database, JobQueue(database)
    finally:
        database.dispose()


def _run_failing_job(queue: JobQueue, **worker_options):
    path = _long_file(ROOT_OK)

    def handler(_payload, _progress, _cancel):
        raise FileNotFoundError(2, "No such file or directory", path)

    job = queue.enqueue("long.path.test", max_attempts=1)
    assert Worker(
        queue, "worker", {"long.path.test": handler}, **worker_options
    ).run_once()
    return queue.get(job.id)


def test_worker_names_data_root_in_failed_job(job_runtime, monkeypatch) -> None:
    _, queue = job_runtime
    _limited(monkeypatch)
    failed = _run_failing_job(queue, data_root=ROOT_OK)
    assert failed.status == "failed"
    assert failed.error_code == "FileNotFoundError"
    assert ROOT_OK in failed.error_message
    assert "55" in failed.error_message


def test_worker_without_data_root_keeps_message(job_runtime, monkeypatch) -> None:
    _, queue = job_runtime
    _limited(monkeypatch)
    failed = _run_failing_job(queue)
    assert failed.error_code == "FileNotFoundError"
    original = str(
        FileNotFoundError(2, "No such file or directory", _long_file(ROOT_OK))
    )
    assert failed.error_message == queue.secret_redactor.redact(original)


def test_worker_survives_a_failing_explainer(job_runtime, monkeypatch) -> None:
    import pandrator.web.jobs as jobs_module

    _, queue = job_runtime
    _limited(monkeypatch)

    def broken(*_args, **_kwargs):
        raise RuntimeError("explainer broke")

    monkeypatch.setattr(jobs_module, "explain_long_path_error", broken)
    failed = _run_failing_job(queue, data_root=ROOT_OK)
    assert failed.status == "failed"
    assert failed.error_code == "FileNotFoundError"
    original = str(
        FileNotFoundError(2, "No such file or directory", _long_file(ROOT_OK))
    )
    assert failed.error_message == queue.secret_redactor.redact(original)


def test_delivery_lane_passes_data_root(tmp_path: Path, monkeypatch) -> None:
    from pandrator.web.job_handler_domains import DELIVERY_LANE_JOB_KINDS

    paths = web_test_support.prepare_web_test_data_root(tmp_path)
    kind = sorted(DELIVERY_LANE_JOB_KINDS)[0]

    def handler(_payload, _progress, _cancel):
        return {}

    monkeypatch.setattr(
        cli,
        "WorkflowHandlers",
        lambda *_a, **_k: SimpleNamespace(handler_registry={kind: handler}),
    )
    built: list[dict] = []

    class RecordingWorker:
        def __init__(self, *args, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(cli, "Worker", RecordingWorker)
    lane = cli._delivery_lane(object(), paths, object(), object(), "w1")
    assert isinstance(lane, RecordingWorker)
    assert built[0]["data_root"] == paths.root


@pytest.mark.parametrize("command", ["command_worker", "command_serve"])
def test_commands_warn_with_data_root(tmp_path: Path, command: str) -> None:
    sentinel = RuntimeError("stop after warning")
    paths = web_test_support.prepare_web_test_data_root(tmp_path)
    args = argparse.Namespace(
        data_dir=str(paths.root), worker_id=None, once=True, host="127.0.0.1"
    )
    calls: list[object] = []

    def record(root):
        calls.append(root)
        raise sentinel

    with (
        patch.object(cli, "_database", return_value=(paths, object())),
        patch.object(cli, "warn_if_data_root_too_long", side_effect=record),
        patch.object(cli, "serve_network_profile"),
    ):
        with pytest.raises(RuntimeError) as caught:
            getattr(cli, command)(args)
    assert caught.value is sentinel
    assert calls == [paths.root]
