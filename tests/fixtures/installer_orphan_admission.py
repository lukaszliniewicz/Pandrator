"""Disposable Linux orphan/owned-venv/native install-admission witness."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import psutil

sys.path.insert(0, str(Path.cwd()))
from pandrator_installer import lifecycle, supervisor
from pandrator_installer.lifecycle_guard import LifecycleBusy
from pandrator_installer.models import InstallSelection
from pandrator_installer.runtime_metadata import uninstall_runtime_may_be_active
from pandrator_installer.service import HeadlessInstaller
from pandrator_installer.workflows import WorkflowMixin


def host(workspace: Path, cut: str, output: Path) -> int:
    root = workspace / "Pandrator"
    marker = root / "child.json"
    env_python = root / "envs" / "fixture" / "bin" / "python"
    spec = supervisor.ManagedProcessSpec(
        key="service-fixture",
        label="Owned venv child",
        command=(str(env_python), str(root / "child.py")),
        cwd=str(root),
        startup_timeout_seconds=0.1,
        restart_limit=0,
    )
    native_owner = supervisor.ProcessSupervisor
    native_capture = supervisor.capture_process_identity
    owners: list[supervisor.ProcessSupervisor] = []
    identities: list[dict[str, Any]] = []
    stop_calls: list[int] = []

    def capture(process: psutil.Process, **kwargs: Any) -> Any:
        if process.pid != os.getpid():
            deadline = time.monotonic() + 2
            while not marker.exists():
                assert time.monotonic() < deadline, "native child readiness missing"
                time.sleep(0.01)
            identities.append(
                {"pid": process.pid, "create_time": process.create_time(), "exe": process.exe()}
            )
            if cut == "orphan":
                raise OSError("Controlled child identity-capture failure")
        return native_capture(process, **kwargs)

    def create(**kwargs: Any) -> supervisor.ProcessSupervisor:
        owner = native_owner(**kwargs)
        owners.append(owner)
        native_stop = owner._terminate_process_tree

        def terminate(process: subprocess.Popen) -> None:
            stop_calls.append(process.pid)
            if cut == "orphan":
                raise OSError("Controlled persistent child-stop failure")
            return native_stop(process, timeout=0.1)

        patches.enter_context(
            mock.patch.object(owner, "_terminate_process_tree", side_effect=terminate)
        )
        if cut == "control":
            owner.ready_callback = owner.stop_event.set
        return owner

    with (
        ExitStack() as patches,
        mock.patch.object(lifecycle, "_runtime_specs", return_value=[spec]),
        mock.patch.object(lifecycle, "ProcessSupervisor", side_effect=create),
        mock.patch.object(supervisor, "capture_process_identity", side_effect=capture),
    ):
        code = lifecycle.main(
            ["launch", "--workspace", str(workspace), "--no-browser", "--password-scope", "none"]
        )
    owner = owners[0]
    result = {
        "code": code,
        "identities": identities,
        "stop_calls": stop_calls,
        "starting_keys": list(owner.starting_processes),
        "managed_keys": list(owner.processes),
        "instance_acquired": owner.lock.acquired,
        "state_exists": owner.runtime_state.exists(),
        "lock_exists": owner.lock.path.exists(),
        "ready": owner.ready,
        "starting_logs_open": all(
            not s.log_handle.closed for s in owner.starting_processes.values()
        ),
    }
    output.write_text(json.dumps(result) + "\n")
    # Deliberate normal process exit; outer subreaper owns all remaining native cleanup.
    return code


def admission(workspace: Path, output: Path) -> int:
    root = workspace / "Pandrator"
    installer = HeadlessInstaller(working_dir=workspace)
    witness = root / "workflow-entered"
    before = {
        p.name: p.read_bytes().hex()
        for p in (root / "pandrator.instance.lock", root / "runtime-processes.json")
        if p.exists()
    }

    def body(*_args: Any, **_kwargs: Any) -> None:
        witness.write_text("protected workflow reached")

    with (
        ExitStack() as stack,
        mock.patch.object(WorkflowMixin, "install_process", side_effect=body),
    ):
        if revision := os.environ.get("PANDRATOR_TEST_INVOCATION_REVISION"):
            module = ModuleType("pandrator_installer._inventory_before")
            module.__package__ = "pandrator_installer"
            source = subprocess.check_output(
                ["git", "show", f"{revision}:pandrator_installer/components.py"], text=True
            )
            exec(compile(source, f"{revision}:components.py", "exec"), module.__dict__)
            stack.enter_context(
                mock.patch.object(
                    HeadlessInstaller,
                    "get_running_installation_processes",
                    module.ComponentOperationsMixin.get_running_installation_processes,
                )
            )
        active = uninstall_runtime_may_be_active(root)
        inventory = installer.get_running_installation_processes(str(root))
        try:
            installer.install_process(InstallSelection(pandrator=False))
            error = None
        except LifecycleBusy as failure:
            error = str(failure)
    after = {
        p.name: p.read_bytes().hex()
        for p in (root / "pandrator.instance.lock", root / "runtime-processes.json")
        if p.exists()
    }
    output.write_text(
        json.dumps(
            {
                "admitted": witness.exists(),
                "error": error,
                "metadata_active": active,
                "inventory": inventory,
                "metadata_preserved": before == after,
            }
        )
        + "\n"
    )
    return 0


def measure(phase: str, cut: str, output: Path) -> None:
    assert sys.platform == "linux"
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    libc.prctl.restype = ctypes.c_int
    assert libc.prctl(36, 1, 0, 0, 0) == 0, ctypes.get_errno()
    result: dict[str, Any] = {"phase": phase, "cut": cut}
    with tempfile.TemporaryDirectory(prefix="pandrator-o2aj-") as temporary:
        workspace = Path(temporary)
        root = workspace / "Pandrator"
        root.mkdir()
        marker = root / "child.json"
        child_script = (
            "import json,os,signal,sys,time;from pathlib import Path;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"Path({str(marker)!r}).write_text(json.dumps({{'pid':os.getpid(),'prefix':sys.prefix,'executable':sys.executable}}));time.sleep(30)"
        )
        (root / "child.py").write_text(child_script)
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(root / "envs" / "fixture")],
            check=True,
            timeout=5,
            capture_output=True,
            text=True,
        )
        environment = os.environ.copy()
        environment.pop("PANDRATOR_OWNER_PASSWORD", None)
        child: psutil.Process | None = None
        identity: dict[str, Any] | None = None
        reaped = False
        try:
            started = time.monotonic()
            host_result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "host",
                    str(workspace),
                    cut,
                    str(workspace / "host.json"),
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=6,
            )
            assert host_result.returncode == (0 if cut == "control" else 2), host_result.stderr
            observed = json.loads((workspace / "host.json").read_text())
            identity = observed["identities"][0]
            assert isinstance(identity, dict)
            child_payload = json.loads(marker.read_text())
            assert child_payload["pid"] == identity["pid"]
            assert child_payload["prefix"] == str(root / "envs" / "fixture")
            if psutil.pid_exists(identity["pid"]):
                child = psutil.Process(identity["pid"])
                assert child.create_time() == identity["create_time"]
            active = child is not None and child.status() != psutil.STATUS_ZOMBIE
            result.update(
                host=observed,
                host_returncode=host_result.returncode,
                host_elapsed=time.monotonic() - started,
                child=child_payload,
                child_active_after_host_exit=active,
                native_executable=identity["exe"],
                owned_invocation_symlink=(
                    root / "envs" / "fixture" / "bin" / "python"
                ).is_symlink(),
                native_executable_outside_root=not Path(identity["exe"]).is_relative_to(root),
                child_parent_after_exit=child.ppid() if child is not None else None,
                subreaper_pid=os.getpid(),
            )
            assert active == (cut == "orphan")
            if cut == "orphan":
                assert child is not None and child.ppid() == os.getpid()
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "admission",
                    str(workspace),
                    cut,
                    str(workspace / "admission.json"),
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=5,
                check=True,
            )
            result["admission"] = json.loads((workspace / "admission.json").read_text())
            if child is not None:
                result["child_still_same_identity_after_probe"] = (
                    child.is_running()
                    and child.create_time() == identity["create_time"]
                    and child.status() != psutil.STATUS_ZOMBIE
                )
                assert result["child_still_same_identity_after_probe"]
            assert result["admission"]["metadata_preserved"]
            assert result["admission"]["admitted"] == (cut == "control" or phase == "before"), (
                result
            )
        finally:
            if marker.exists():
                pid = json.loads(marker.read_text())["pid"]
                try:
                    # A cached PID from the control may already be reusable. Prove
                    # this is still our waitable child before inspection or signalling;
                    # WNOWAIT pins a dead adopted child until the final waitpid.
                    os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    process = psutil.Process(pid)
                    if identity is not None:
                        assert process.create_time() == identity["create_time"]
                    if process.status() != psutil.STATUS_ZOMBIE:
                        process.kill()
                    waited, status = os.waitpid(pid, 0)
                    assert waited == pid
                    reaped = True
                except ChildProcessError:
                    # Already reaped by the launcher: never look up or kill a reuse.
                    reaped = True
            result["child_reaped"] = reaped
    result["root_removed"] = not workspace.exists()
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=["host", "admission", "before", "accepted"])
parser.add_argument("workspace_or_cut")
parser.add_argument("cut_or_output")
parser.add_argument("output", nargs="?")
args = parser.parse_args()
if args.mode == "host":
    assert args.output is not None
    raise SystemExit(host(Path(args.workspace_or_cut), args.cut_or_output, Path(args.output)))
if args.mode == "admission":
    assert args.output is not None
    raise SystemExit(admission(Path(args.workspace_or_cut), Path(args.output)))
measure(args.mode, args.workspace_or_cut, Path(args.cut_or_output))
