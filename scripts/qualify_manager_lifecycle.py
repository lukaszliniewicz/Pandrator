#!/usr/bin/env python3
"""Exercise the native manager bootstrap, handoff, and uninstall lifecycle.

The default mode creates a disposable workspace.  A private ``--prepare``
mode runs in a short-lived source interpreter so the authenticated handoff
records refer to a process that genuinely exits before the native helper
takes over.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_from_output(output: str) -> dict[str, Any]:
    """Decode the final JSON object even if a dependency printed a banner."""

    decoder = json.JSONDecoder()
    for offset, character in enumerate(output):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(output[offset:])
        except json.JSONDecodeError:
            continue
        if not output[offset + end :].strip() and isinstance(value, dict):
            return value
    raise RuntimeError(f"Command did not emit a final JSON object:\n{output}")


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: float = 120,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        shell=False,
        env=None if env is None else dict(env),
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: "
            f"{command!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _wait_for_process_exit(pid: int, create_time: float, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - create_time) > 0.01:
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(0.1)
    raise TimeoutError(f"Process {pid} did not exit.")


def _prepare(workspace: Path, bundle: Path) -> int:
    from pandrator_manager import __version__
    from pandrator_manager.application import create_application
    from pandrator_manager.models import OperationState
    from pandrator_manager.operations import OperationEngine
    from pandrator_manager.releases import (
        TrustStore,
        canonical_json,
        release_cache_path,
    )

    application = create_application(workspace)
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    trust = TrustStore({"qualification": public})
    application.configure_release_trust(trust)
    artifact = {
        "filename": bundle.name,
        "url": "https://qualification.invalid/" + bundle.name,
        "sha256": _sha256_file(bundle),
        "size_bytes": bundle.stat().st_size,
        "kind": "zip",
        "systems": [application.context.system],
        "architectures": [application.context.architecture],
        "python_tags": [],
    }
    payload = {
        "schema_version": 1,
        "product": "pandrator-manager",
        "channel": "stable",
        "version": __version__,
        "sequence": 1,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "minimum_manager_version": "0.1",
        "artifacts": [artifact],
        "key_rotation": None,
    }
    signature = base64.b64encode(private.sign(canonical_json(payload))).decode()
    manifest = {
        "signed": payload,
        "signatures": [
            {
                "key_id": "qualification",
                "signature": signature,
            }
        ],
    }
    verified = trust.verify(manifest)
    selected = verified.select_artifact(
        system=application.context.system,
        architecture=application.context.architecture,
    )
    cached = release_cache_path(application.context.layout, selected)
    cached.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle, cached)
    plan = application.release_plan(manifest, offline=True)
    operation, created = application.submit_operation(
        plan_id=plan.id,
        plan_digest=plan.digest,
        accepted_confirmations=tuple(
            confirmation.key for confirmation in plan.confirmations
        ),
        idempotency_key=f"qualification:{plan.id}",
    )
    if not created:
        raise RuntimeError("Qualification operation was unexpectedly replayed.")
    engine = OperationEngine(
        application.context,
        application.store,
        application.registry,
        release_authority=application.release_authority,
        manager_handoff_callback=lambda _execution, _result: None,
    )
    engine._execute(operation.id)
    pending = application.store.get_operation(operation.id)
    if pending.state != OperationState.HANDOFF_PENDING:
        raise RuntimeError(
            f"Manager update did not reach handoff_pending: {pending.state}"
        )
    print(
        json.dumps(
            {
                "operation_id": operation.id,
                "version": payload["version"],
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify the native Pandrator Manager bootstrap, update handoff, "
            "and preserve-data uninstall in a disposable workspace."
        )
    )
    parser.add_argument("--bootstrap", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument("--prepare", action="store_true", help=argparse.SUPPRESS)
    return parser


def _default_bundle_path(
    repo_root: Path,
    version: str,
    *,
    system: str | None = None,
    machine: str | None = None,
) -> Path:
    from scripts.build_manager_release_bundle import _release_platform

    return (
        repo_root
        / "dist"
        / (
            f"pandrator-manager-{version}-"
            f"{_release_platform(system=system, machine=machine)}.zip"
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    from pandrator_manager import __version__

    if args.prepare:
        if args.workspace is None or args.bundle is None:
            raise RuntimeError("--prepare requires --workspace and --bundle.")
        return _prepare(
            args.workspace.expanduser().resolve(strict=True),
            args.bundle.expanduser().resolve(strict=True),
        )

    suffix = ".exe" if os.name == "nt" else ""
    bootstrap = (
        args.bootstrap
        or repo_root / "dist" / f"PandratorManagerBootstrap{suffix}"
    ).expanduser().resolve(strict=True)
    bundle = (
        args.bundle
        or _default_bundle_path(repo_root, __version__)
    ).expanduser().resolve(strict=True)
    if not bootstrap.is_file() or bootstrap.is_symlink():
        raise RuntimeError(f"Unsafe or missing bootstrap: {bootstrap}")
    if not bundle.is_file() or bundle.is_symlink():
        raise RuntimeError(f"Unsafe or missing manager bundle: {bundle}")

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.workspace is None:
        temporary = tempfile.TemporaryDirectory(
            prefix="PandratorManagerLifecycle-",
            ignore_cleanup_errors=True,
        )
        workspace = Path(temporary.name).resolve(strict=True)
    else:
        workspace = args.workspace.expanduser().resolve(strict=False)
        if workspace.exists() and any(workspace.iterdir()):
            raise RuntimeError(
                "An explicit qualification workspace must be absent or empty."
            )
        workspace.mkdir(parents=True, exist_ok=True)

    from pandrator_manager.client import ManagerClient
    from pandrator_manager.context import WorkspaceLayout
    from pandrator_manager.uninstall import uninstall_control_root

    layout = WorkspaceLayout.from_value(workspace)
    report: dict[str, Any] = {
        "workspace": str(workspace),
        "bootstrap": str(bootstrap),
        "bundle": str(bundle),
    }
    settings_temporary = tempfile.TemporaryDirectory(
        prefix="PandratorManagerLifecycleSettings-",
        ignore_cleanup_errors=True,
    )
    settings_root = Path(settings_temporary.name).resolve(strict=True)
    qualification_environment = os.environ.copy()
    qualification_environment["LOCALAPPDATA"] = str(settings_root)
    qualification_environment["XDG_CONFIG_HOME"] = str(settings_root)
    try:
        setup_result = settings_root / "bootstrap-setup-result.json"
        _run(
            [
                str(bootstrap),
                "setup",
                "--workspace",
                str(workspace),
                "--no-open",
                "--result-file",
                str(setup_result),
            ],
            cwd=repo_root,
            env=qualification_environment,
        )
        report["bootstrap_setup"] = _json_from_output(
            setup_result.read_text(encoding="utf-8")
        )
        remembered_path = Path(
            str(report["bootstrap_setup"].get("workspace_settings") or "")
        ).resolve(strict=False)
        if (
            not report["bootstrap_setup"].get("workspace_remembered")
            or not remembered_path.is_file()
            or not remembered_path.is_relative_to(settings_root)
        ):
            raise RuntimeError(
                "Bootstrap setup did not persist its selected workspace."
            )
        initial = ManagerClient.discover(workspace)
        initial_status = initial.status()
        report["initial_manager"] = initial_status
        layout.data.mkdir(parents=True, exist_ok=True)
        sentinel = layout.data / "qualification-sentinel.txt"
        sentinel.write_text("preserve me\n", encoding="utf-8")
        old_descriptor = initial.descriptor
        initial.stop_manager()
        _wait_for_process_exit(
            old_descriptor.pid,
            old_descriptor.process_create_time,
        )

        remembered_result = settings_root / "remembered-start-result.json"
        _run(
            [
                str(bootstrap),
                "start",
                "--result-file",
                str(remembered_result),
            ],
            cwd=repo_root,
            env=qualification_environment,
        )
        report["remembered_start"] = _json_from_output(
            remembered_result.read_text(encoding="utf-8")
        )
        if (
            report["remembered_start"].get("workspace") != str(workspace)
            or report["remembered_start"].get("workspace_source")
            != "remembered"
        ):
            raise RuntimeError(
                "A later launcher run did not reuse the selected workspace."
            )
        remembered_client = ManagerClient.discover(workspace)
        default_cli = _run(
            [
                sys.executable,
                "-m",
                "pandrator_manager.cli",
                "--json",
                "status",
            ],
            cwd=repo_root,
            env=qualification_environment,
        )
        report["remembered_cli"] = _json_from_output(default_cli.stdout)
        if report["remembered_cli"].get("workspace") != str(workspace):
            raise RuntimeError(
                "The CLI default did not reuse the selected workspace."
            )
        remembered_descriptor = remembered_client.descriptor
        remembered_client.stop_manager()
        _wait_for_process_exit(
            remembered_descriptor.pid,
            remembered_descriptor.process_create_time,
        )

        prepared = _run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--prepare",
                "--workspace",
                str(workspace),
                "--bundle",
                str(bundle),
            ],
            cwd=repo_root,
            env=qualification_environment,
        )
        preparation = _json_from_output(prepared.stdout)
        operation_id = str(preparation["operation_id"])
        report["prepared_handoff"] = preparation

        handoff = _run(
            [
                str(bootstrap),
                "handoff",
                "--workspace",
                str(workspace),
                "--operation-id",
                operation_id,
            ],
            cwd=repo_root,
            timeout=180,
            env=qualification_environment,
        )
        report["handoff_exit_code"] = handoff.returncode
        active = ManagerClient.discover(workspace)
        active_status = active.status()
        report["active_manager"] = active_status
        if active_status.get("manager_version") != preparation["version"]:
            raise RuntimeError(
                "The post-handoff daemon reported the wrong manager version."
            )
        active_descriptor = active.descriptor
        active.stop_manager()
        _wait_for_process_exit(
            active_descriptor.pid,
            active_descriptor.process_create_time,
        )

        uninstall = _run(
            [
                sys.executable,
                "-m",
                "pandrator_manager.cli",
                "--workspace",
                str(workspace),
                "--json",
                "uninstall",
                "--yes",
                "--wait",
            ],
            cwd=repo_root,
            timeout=240,
            env=qualification_environment,
        )
        uninstall_status = _json_from_output(uninstall.stdout)
        report["uninstall"] = uninstall_status
        if uninstall_status.get("status") != "succeeded":
            raise RuntimeError(
                "Uninstall did not finish without cleanup residue: "
                + json.dumps(uninstall_status, sort_keys=True)
            )
        if uninstall_status.get("cleanup_residue") is not None:
            raise RuntimeError("Uninstall reported unexpected cleanup residue.")

        control = uninstall_control_root(layout)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and os.path.lexists(control):
            time.sleep(0.1)
        if os.path.lexists(control):
            raise RuntimeError(
                f"External uninstall control directory remains: {control}"
            )
        remaining = (
            sorted(path.name for path in layout.root.iterdir())
            if layout.root.is_dir()
            else []
        )
        if remaining != ["data"]:
            raise RuntimeError(
                f"Unexpected files remained after uninstall: {remaining}"
            )
        if sentinel.read_text(encoding="utf-8") != "preserve me\n":
            raise RuntimeError("Preserved user data did not survive uninstall.")
        report["remaining_root_entries"] = remaining
        report["ok"] = True
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        try:
            running = ManagerClient.discover(workspace)
            descriptor = running.descriptor
            running.stop_manager()
            _wait_for_process_exit(
                descriptor.pid,
                descriptor.process_create_time,
                timeout=10,
            )
        except Exception:
            pass
        if args.keep_workspace:
            print(f"Qualification workspace retained: {workspace}", file=sys.stderr)
        elif temporary is not None:
            temporary.cleanup()
        settings_temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
