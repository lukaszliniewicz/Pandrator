"""Disposable managed-worker capacity and process lifecycle contracts."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest

from pandrator_installer.lifecycle import _runtime_specs
from pandrator_installer.models import WorkspacePaths
from pandrator_installer.runtime import RuntimeMixin
from pandrator_manager.api import create_api
from pandrator_manager.application import create_application
from pandrator_manager.context import WorkspaceLayout
from pandrator_manager.models import HealthResult, HealthState, ManagedService, ProcessIdentity
from pandrator_manager.operations.release_tasks import ReleaseTasks
from pandrator_manager.runtime_specs import PANDRATOR_WORKER_SERVICES, pandrator_runtime_specs
from pandrator_manager.supervisor import ProcessSupervisor


class MemorySupervisor(ProcessSupervisor):
    """Exercise high-level lifecycle routes without launching any processes."""

    def __init__(self, specs):
        self.specs = {spec.service_id: spec for spec in specs}
        self.running = set()
        self.calls = []
        self.fail = None

    def spec(self, service_id):
        return self.specs.get(service_id)

    def snapshot(self):
        return [
            ManagedService(
                id=key,
                component_id="pandrator",
                service_key=key,
                process=ProcessIdentity(
                    pid=123, create_time=1, executable="fixture", manager_instance_id="fixture"
                )
                if key in self.running
                else None,
                desired_running=key in self.running,
                health=HealthResult(state=HealthState.HEALTHY, service_id=key),
            )
            for key in self.specs
        ]

    def start(self, service_id):
        self.calls.append(("start", service_id))
        if self.fail == service_id:
            raise RuntimeError("fixture worker failed")
        for dependency in self.specs[service_id].dependencies:
            if dependency not in self.running:
                self.start(dependency)
        self.running.add(service_id)
        return next(item for item in self.snapshot() if item.id == service_id)

    def stop(self, service_id):
        self.calls.append(("stop", service_id))
        self.running.discard(service_id)

    def replace_spec(self, spec):
        self.specs[spec.service_id] = spec

    def unregister(self, service_id):
        self.specs.pop(service_id)


@pytest.mark.parametrize("count", ["1", "2"])
def test_specs_have_independent_workers_and_no_worker_ports(tmp_path, count):
    layout = WorkspaceLayout.from_value(tmp_path)
    with mock.patch.dict(os.environ, {"PANDRATOR_WORKER_COUNT": "2"}):
        specs = pandrator_runtime_specs(
            layout, preferences={"PANDRATOR_WORKER_COUNT": count}, mcp_supported=False
        )
    workers = [spec for spec in specs if spec.service_id in PANDRATOR_WORKER_SERVICES]
    assert tuple(spec.service_id for spec in workers) == PANDRATOR_WORKER_SERVICES[: int(count)]
    for worker in workers:
        assert worker.arguments[-2:] == ("--worker-id", worker.service_id)
        assert worker.environment["PANDRATOR_EXPECTED_WORKERS"] == count
        assert worker.dependencies == ("pandrator.api",)
        assert not worker.ports


def test_default_worker_count_is_one_and_environment_override_is_respected(tmp_path):
    with mock.patch.dict(os.environ):
        os.environ.pop("PANDRATOR_WORKER_COUNT", None)
        specs = pandrator_runtime_specs(WorkspaceLayout.from_value(tmp_path), mcp_supported=False)
        assert len(specs) == 2
        native = _runtime_specs(
            WorkspacePaths.from_value(tmp_path), SimpleNamespace(host="127.0.0.1", port=8097)
        )
        assert [spec.key for spec in native] == ["api", "worker"]
        assert native[-1].env["PANDRATOR_EXPECTED_WORKERS"] == "1"
    with mock.patch.dict(os.environ, {"PANDRATOR_WORKER_COUNT": "2"}):
        assert (
            len(pandrator_runtime_specs(WorkspaceLayout.from_value(tmp_path), mcp_supported=False))
            == 3
        )


@pytest.mark.parametrize("count", ["0", "3", "", "two", " 2", "2.0"])
def test_worker_count_is_strict_for_manager_and_headless(tmp_path, count):
    args = SimpleNamespace(host="127.0.0.1", port=8097)
    with mock.patch.dict(os.environ, {"PANDRATOR_WORKER_COUNT": count}):
        with pytest.raises(ValueError, match="Worker count must be 1 or 2."):
            pandrator_runtime_specs(WorkspaceLayout.from_value(tmp_path))
        with pytest.raises(ValueError, match="Worker count must be 1 or 2."):
            _runtime_specs(WorkspacePaths.from_value(tmp_path), args)


@pytest.fixture
def managed_client(tmp_path):
    application = create_application(tmp_path)
    application.list_components = lambda: [
        {"definition": {"id": "pandrator"}, "inspection": {"state": "present"}}
    ]
    specs = pandrator_runtime_specs(
        application.context.layout, preferences={"PANDRATOR_WORKER_COUNT": "2"}, mcp_supported=False
    )
    supervisor = MemorySupervisor(specs)
    secret = "s" * 43
    api = create_api(
        application,
        supervisor,
        client_secret=secret,
        application_environment={"PANDRATOR_WORKER_COUNT": "2"},
    )
    headers = {"Authorization": f"Bearer {secret}"}
    return api.test_client(), supervisor, headers


def test_start_stop_restart_cover_secondary_when_primary_is_down(managed_client):
    client, supervisor, headers = managed_client
    supervisor.running = {"pandrator.api", "pandrator.worker.2"}
    response = client.get("/v1/application", headers=headers)
    assert response.status_code == 200
    assert response.json["worker_running"]
    assert not response.json["healthy"]
    assert response.json["worker_capacity"] == {"configured": 2, "running": 1, "ready": False}
    for action in ("start", "stop", "restart"):
        supervisor.calls.clear()
        headers["Idempotency-Key"] = action
        response = client.post(f"/v1/application/{action}", headers=headers, json={})
        assert response.status_code == 200, response.json
        for worker in PANDRATOR_WORKER_SERVICES:
            assert (("stop" if action == "stop" else "start"), worker) in supervisor.calls
        if action == "restart":
            assert all(("stop", worker) in supervisor.calls for worker in PANDRATOR_WORKER_SERVICES)


def test_failed_secondary_never_reports_full_capacity(managed_client):
    client, supervisor, headers = managed_client
    supervisor.running = {"pandrator.api"}
    supervisor.fail = "pandrator.worker.2"
    response = client.post(
        "/v1/application/start", headers={**headers, "Idempotency-Key": "fail"}, json={}
    )
    assert response.status_code == 409
    status = client.get("/v1/application", headers=headers).json
    assert not status["healthy"]
    assert status["worker_capacity"] == {"configured": 2, "running": 1, "ready": False}


def test_release_stop_and_rollback_restore_actual_worker_set(tmp_path):
    supervisor = MemorySupervisor(
        pandrator_runtime_specs(
            WorkspaceLayout.from_value(tmp_path),
            preferences={"PANDRATOR_WORKER_COUNT": "2"},
            mcp_supported=False,
        )
    )
    supervisor.running = {"pandrator.api", "pandrator.worker.2"}
    execution = SimpleNamespace(supervisor=supervisor)
    handler = ReleaseTasks()
    result = handler._execute_stop_application_release(execution, None)
    assert not supervisor.running
    assert result["services"]["pandrator.worker.2"]["was_running"]
    assert not result["services"]["pandrator.worker"]["was_running"]
    handler._rollback_stop_application_release(execution, None, result)
    assert supervisor.running == {"pandrator.api", "pandrator.worker.2"}


@pytest.mark.parametrize("count", ["1", "2"])
def test_headless_specs_and_readiness_require_configured_workers(tmp_path, count):
    with mock.patch.dict(os.environ, {"PANDRATOR_WORKER_COUNT": count}):
        specs = _runtime_specs(
            WorkspacePaths.from_value(tmp_path), SimpleNamespace(host="127.0.0.1", port=8097)
        )
    keys = ("worker", "worker-2")[: int(count)]
    assert [spec.key for spec in specs] == ["api", *keys]
    for worker in specs[1:]:
        assert worker.command[-2:] == ("--worker-id", worker.key)
        assert worker.env["PANDRATOR_EXPECTED_WORKERS"] == count
        assert not worker.ports
    process = SimpleNamespace(pid=123, poll=lambda: None, pandrator_worker_keys=keys)
    state = {"supervisor_pid": 123, "ready": True, "processes": {key: {} for key in ["api", *keys]}}
    (tmp_path / "runtime-processes.json").write_text(json.dumps(state))
    with mock.patch(
        "pandrator_installer.runtime.requests.get", return_value=SimpleNamespace(status_code=200)
    ):
        assert (
            RuntimeMixin.wait_for_web_runtime_ready(SimpleNamespace(), process, tmp_path) == state
        )


def test_headless_does_not_accept_missing_companion_or_probe_api(tmp_path):
    state = {"supervisor_pid": 123, "ready": True, "processes": {"api": {}, "worker": {}}}
    (tmp_path / "runtime-processes.json").write_text(json.dumps(state))
    process = SimpleNamespace(
        pid=123, poll=mock.Mock(side_effect=[None, 1]), pandrator_worker_keys=("worker", "worker-2")
    )
    runtime = SimpleNamespace(_read_log_tail_if_exists=lambda _path: "")
    with mock.patch("pandrator_installer.runtime.requests.get") as health:
        with pytest.raises(RuntimeError, match="exited before"):
            RuntimeMixin.wait_for_web_runtime_ready(runtime, process, tmp_path, poll_interval=0.01)
        health.assert_not_called()


def test_legacy_headless_readiness_accepts_single_worker(tmp_path):
    state = {"supervisor_pid": 123, "ready": True, "processes": {"api": {}, "worker": {}}}
    (tmp_path / "runtime-processes.json").write_text(json.dumps(state))
    process = SimpleNamespace(pid=123, poll=lambda: None)
    with mock.patch(
        "pandrator_installer.runtime.requests.get", return_value=SimpleNamespace(status_code=200)
    ):
        assert (
            RuntimeMixin.wait_for_web_runtime_ready(SimpleNamespace(), process, tmp_path) == state
        )


def test_native_stop_all_covers_both_workers_without_real_processes(tmp_path):
    from pandrator_installer.supervisor import ProcessSupervisor as NativeSupervisor

    stopped = []
    supervisor = SimpleNamespace(
        stop_event=mock.Mock(),
        ready=True,
        ready_at=1,
        starting_processes={},
        processes={key: key for key in ("api", "worker", "worker-2")},
        runtime_state=tmp_path / "runtime-processes.json",
        runtime_control=tmp_path / "runtime-control.json",
        lock=mock.Mock(),
        _stop_one=stopped.append,
    )
    NativeSupervisor.stop_all(supervisor)
    assert stopped == ["worker-2", "worker", "api"]
    assert not supervisor.processes
    assert not supervisor.ready


@pytest.mark.parametrize("expected,prefix", [("1", "WARNING:"), ("2", "INFO:")])
def test_worker_presence_advisory_distinguishes_expected_companion(
    tmp_path, capsys, expected, prefix
):
    from pandrator.runtime import DataPaths
    from pandrator.web.cli import command_worker

    paths = DataPaths.from_value(tmp_path).ensure()
    paths.worker_presence.write_text(
        json.dumps({"pid": os.getpid(), "worker_id": "primary", "token": "fixture"})
    )
    with mock.patch.dict(os.environ, {"PANDRATOR_EXPECTED_WORKERS": expected}):
        assert (
            command_worker(
                SimpleNamespace(
                    data_dir=str(tmp_path),
                    command="worker",
                    worker_id="companion",
                    once=True,
                    poll_interval=0.01,
                )
            )
            == 0
        )
    stderr = capsys.readouterr().err
    assert prefix in stderr
    if expected == "2":
        assert "WARNING:" not in stderr


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("count", ["1", "2"])
@pytest.mark.parametrize("mcp", [False, True])
def test_startup_journal_accepts_legacy_and_current_capacity(tmp_path, legacy, count, mcp):
    from pandrator_manager.models import TaskSpec
    from pandrator_manager.operations.application_recovery import _parse_application_start_journal
    from pandrator_manager.runtime_specs import PANDRATOR_SERVICE_START_ORDER

    specs = pandrator_runtime_specs(
        WorkspaceLayout.from_value(tmp_path),
        preferences={"PANDRATOR_WORKER_COUNT": count},
        mcp_supported=mcp,
    )
    task = TaskSpec(id="start", kind="start_application", label="fixture", component_id="pandrator")
    execution = SimpleNamespace(operation=SimpleNamespace(id="fixture"))
    ids = set(PANDRATOR_SERVICE_START_ORDER)
    if legacy:
        ids.remove("pandrator.worker.2")
    payload = {
        "schema_version": 1,
        "operation_id": "fixture",
        "task_id": "start",
        "component_id": "pandrator",
        "previous": {
            key: {"spec": None, "was_running": False, "desired_running": False} for key in ids
        },
        "target": {spec.service_id: spec.model_dump(mode="json") for spec in specs},
        "previous_active_path": None,
    }
    parsed = _parse_application_start_journal(execution, task, payload)
    assert set(parsed.previous) == set(PANDRATOR_SERVICE_START_ORDER)
    assert parsed.previous["pandrator.worker.2"].spec is None
    assert not parsed.previous["pandrator.worker.2"].was_running
    assert set(parsed.target) == {spec.service_id for spec in specs}
    for removed in ("pandrator.api", "pandrator.worker", "pandrator.mcp"):
        malformed = {
            **payload,
            "previous": {
                key: state for key, state in payload["previous"].items() if key != removed
            },
        }
        with pytest.raises(RuntimeError, match="journal is invalid"):
            _parse_application_start_journal(execution, task, malformed)
    malformed = {
        **payload,
        "target": {**payload["target"], "foreign": specs[0].model_dump(mode="json")},
    }
    with pytest.raises(RuntimeError, match="journal is invalid"):
        _parse_application_start_journal(execution, task, malformed)


@pytest.mark.parametrize("count", ["1", "2"])
def test_install_startup_starts_all_selected_workers(tmp_path, count):
    from contextlib import nullcontext

    from pandrator_manager.models import TaskSpec
    from pandrator_manager.operations.handlers import FilesystemTaskHandler

    application = create_application(tmp_path)
    specs = pandrator_runtime_specs(
        application.context.layout,
        preferences={"PANDRATOR_WORKER_COUNT": count},
        mcp_supported=False,
    )
    supervisor = MemorySupervisor(specs)
    supervisor.service_transition_guard = nullcontext
    execution = SimpleNamespace(
        supervisor=supervisor,
        context=application.context,
        operation=SimpleNamespace(id="fixture"),
        plan=SimpleNamespace(desired={}),
        prior_results={},
    )
    task = TaskSpec(id="start", kind="start_application", label="fixture", component_id="pandrator")
    handler = FilesystemTaskHandler()
    with mock.patch.object(handler, "_application_runtime_specs", return_value=specs):
        result = handler._execute_start_application(execution, task)
    assert result["started"]
    assert supervisor.running == {spec.service_id for spec in specs}


def test_release_rollback_reports_mcp_dependency_without_restarting_stopped_primary(tmp_path):
    application = create_application(tmp_path)
    supervisor = MemorySupervisor(
        pandrator_runtime_specs(
            application.context.layout,
            preferences={"PANDRATOR_WORKER_COUNT": "2"},
            mcp_supported=True,
        )
    )
    supervisor.running = {"pandrator.api", "pandrator.worker.2", "pandrator.mcp"}
    execution = SimpleNamespace(
        supervisor=supervisor, context=application.context, operation=SimpleNamespace(id="fixture")
    )
    handler = ReleaseTasks()
    result = handler._execute_stop_application_release(execution, None)
    with mock.patch.object(application.context.event_sink, "emit") as event:
        handler._rollback_stop_application_release(execution, None, result)
    assert supervisor.running == {"pandrator.api", "pandrator.worker.2"}
    assert event.call_args.args[0] == "application.mcp_start_failed"
    assert "primary worker was stopped" in event.call_args.args[1]["error"]


@pytest.mark.parametrize("workers", [{"pandrator.worker.2"}, set(PANDRATOR_WORKER_SERVICES)])
def test_install_rollback_restores_actual_worker_set(tmp_path, workers):
    from contextlib import nullcontext

    from pandrator_manager.models import TaskSpec, TaskState
    from pandrator_manager.operations.application_recovery import (
        _ApplicationServiceState,
        _ApplicationStartJournal,
    )
    from pandrator_manager.operations.handlers import FilesystemTaskHandler
    from pandrator_manager.runtime_specs import PANDRATOR_SERVICE_START_ORDER

    application = create_application(tmp_path)
    specs = pandrator_runtime_specs(
        application.context.layout, preferences={"PANDRATOR_WORKER_COUNT": "2"}, mcp_supported=False
    )
    supervisor = MemorySupervisor(specs)
    supervisor.service_transition_guard = nullcontext
    running = {"pandrator.api", *workers}
    previous = {
        key: _ApplicationServiceState(
            supervisor.spec(key), key in running, key in running or key == "pandrator.worker"
        )
        for key in PANDRATOR_SERVICE_START_ORDER
    }
    journal = _ApplicationStartJournal(previous, supervisor.specs, None)
    task = TaskSpec(id="start", kind="start_application", label="fixture", component_id="pandrator")
    execution = SimpleNamespace(
        supervisor=supervisor,
        context=application.context,
        operation=SimpleNamespace(id="fixture"),
        plan=SimpleNamespace(tasks=[task]),
        store=SimpleNamespace(
            operation_tasks=lambda _id: [SimpleNamespace(task=task, state=TaskState.ROLLED_BACK)]
        ),
    )
    with mock.patch(
        "pandrator_manager.operations.service_tasks.load_application_start_journal",
        return_value=journal,
    ):
        FilesystemTaskHandler()._restore_application_after_rollback(execution)
    assert supervisor.running == running


@pytest.mark.parametrize("count", [None, "2"])
def test_native_launcher_records_default_one_or_explicit_two_workers(tmp_path, count):
    runtime = SimpleNamespace(
        initial_working_dir=str(tmp_path), get_hidden_subprocess_kwargs=lambda: {}
    )
    process = SimpleNamespace()
    with (
        mock.patch.dict(os.environ),
        mock.patch("pandrator_installer.runtime.subprocess.Popen", return_value=process),
    ):
        os.environ.pop("PANDRATOR_WORKER_COUNT", None)
        if count is not None:
            os.environ["PANDRATOR_WORKER_COUNT"] = count
        launched = RuntimeMixin.run_web_supervisor(runtime, str(tmp_path))
    try:
        assert launched is process
        assert process.pandrator_worker_keys == (
            ("worker",) if count is None else ("worker", "worker-2")
        )
    finally:
        process.log_handle.close()
