"""Authenticated external handoff for whole-product uninstall.

The daemon stops owned services and prepares this descriptor, but an
interpreter outside the managed installation performs the final same-volume
quarantine move after the daemon exits.  A protected journal outside the
installation allows an interrupted cleanup to resume without constructing a
fresh manager state database.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import psutil
from pydantic import Field

from .auth import protect_path
from .autostart import autostart_adapter
from .context import WorkspaceLayout
from .errors import ManagerError
from .launcher import (
    current_runtime_executable,
    external_cleanup_runtime,
    runtime_command,
    stage_cleanup_launcher,
)
from .models import (
    HealthResult,
    HealthState,
    ManagedService,
    OperationKind,
    OperationState,
    StrictModel,
    TaskState,
)
from .processes.identity import (
    IdentityInspectionFailed,
    IdentityMismatch,
    validate_identity,
)
from .releases.slots import _atomic_json
from .releases.trust import canonical_json
from .state import ManagerStore

_CONTROL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class UninstallHandoffPayload(StrictModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=1, max_length=100)
    workspace: str
    expected_revision: int = Field(ge=0)
    purge_data: bool = False
    export_data: str | None = None
    targets: tuple[str, ...]
    prior_services: dict[str, dict[str, Any]]
    old_python: str
    old_runtime_mode: Literal["native_launcher", "python"]
    old_cwd: str
    old_pid: int = Field(gt=0)
    old_create_time: float = Field(gt=0)
    cleanup_mode: Literal["native_launcher", "python"]
    cleanup_executable: str
    cleanup_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    autostart_installed: bool
    autostart_enabled: bool | None = None
    status_path: str
    quarantine_path: str
    created_at: datetime


class UninstallHandoffEnvelope(StrictModel):
    payload: UninstallHandoffPayload
    authentication: str = Field(pattern=r"^[0-9a-f]{64}$")


def uninstall_control_root(layout: WorkspaceLayout) -> Path:
    suffix = hashlib.sha256(
        str(layout.workspace).encode("utf-8")
    ).hexdigest()[:12]
    return layout.workspace / f".pandrator-manager-uninstall-{suffix}"


def _validated_operation_id(operation_id: str) -> str:
    selected = str(operation_id)
    if (
        not _CONTROL_ID.fullmatch(selected)
        or selected in {".", ".."}
    ):
        raise ManagerError(
            "invalid_uninstall_handoff",
            "The uninstall operation identifier is not filesystem-safe.",
            {"operation_id": selected[:120]},
            400,
        )
    return selected


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(
        junction is not None and junction()
    )


def _safe_control_root(
    layout: WorkspaceLayout,
    *,
    create: bool = False,
) -> Path:
    """Return the external control directory after rejecting link redirection."""

    root = uninstall_control_root(layout)
    if root.parent != layout.workspace:
        raise ManagerError(
            "unsafe_uninstall_control",
            "The uninstall control directory is outside the workspace.",
            {"path": str(root)},
            500,
        )
    lexists = os.path.lexists(root)
    if lexists:
        if _is_link_or_junction(root) or not root.is_dir():
            raise ManagerError(
                "unsafe_uninstall_control",
                "The uninstall control path is not a real directory.",
                {"path": str(root)},
                409,
            )
        if root.resolve(strict=True) != root:
            raise ManagerError(
                "unsafe_uninstall_control",
                "The uninstall control directory resolves unexpectedly.",
                {"path": str(root)},
                409,
            )
    elif create:
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            # Close the create/check race by validating the winner.
            root = _safe_control_root(layout, create=False)
    if create:
        protect_path(root, directory=True)
    return root


def _require_regular_file(path: Path, *, description: str) -> None:
    if os.path.lexists(path) and (
        _is_link_or_junction(path) or not path.is_file()
    ):
        raise ManagerError(
            "unsafe_uninstall_control",
            f"The {description} is not a regular file.",
            {"path": str(path)},
            409,
        )


def uninstall_status_path(
    layout: WorkspaceLayout,
    operation_id: str,
) -> Path:
    selected = _validated_operation_id(operation_id)
    return uninstall_control_root(layout) / f"{selected}.status.json"


def _external_descriptor_path(
    layout: WorkspaceLayout,
    operation_id: str,
) -> Path:
    selected = _validated_operation_id(operation_id)
    return uninstall_control_root(layout) / f"{selected}.pending.json"


def _state_descriptor_path(
    layout: WorkspaceLayout,
    operation_id: str,
) -> Path:
    selected = _validated_operation_id(operation_id)
    return layout.state / "uninstalls" / f"{selected}.json"


def _secret_path(layout: WorkspaceLayout, operation_id: str) -> Path:
    selected = _validated_operation_id(operation_id)
    return uninstall_control_root(layout) / f"{selected}.secret"


def _write_secret(path: Path, secret: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    protect_path(path.parent, directory=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(secret)
            handle.flush()
            os.fsync(handle.fileno())
        protect_path(temporary)
        os.replace(temporary, path)
        protect_path(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _commit_path(layout: WorkspaceLayout, operation_id: str) -> Path:
    selected = _validated_operation_id(operation_id)
    return uninstall_control_root(layout) / f"{selected}.commit.json"


def _cleanup_launcher_path(
    layout: WorkspaceLayout,
    operation_id: str,
) -> Path:
    selected = _validated_operation_id(operation_id)
    suffix = ".exe" if os.name == "nt" else ""
    return uninstall_control_root(layout) / f"{selected}.cleanup{suffix}"


def _cleanup_script_path(
    layout: WorkspaceLayout,
    operation_id: str,
) -> Path:
    selected = _validated_operation_id(operation_id)
    return uninstall_control_root(layout) / f"{selected}.cleanup-delete.cmd"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authentication(payload: Mapping[str, Any], secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        canonical_json(payload),
        hashlib.sha256,
    ).hexdigest()


def _write_authenticated(
    path: Path,
    payload: Mapping[str, Any],
    secret: str,
) -> None:
    _atomic_json(
        path,
        {
            "payload": dict(payload),
            "authentication": _authentication(payload, secret),
        },
    )
    protect_path(path.parent, directory=True)
    protect_path(path)


def _read_authenticated(
    path: Path,
    secret: str,
) -> dict[str, Any]:
    _require_regular_file(path, description="authenticated control record")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        payload = raw["payload"]
        authentication = str(raw["authentication"])
    except Exception as error:
        raise RuntimeError(
            f"Authenticated control record is invalid: {path}"
        ) from error
    if not isinstance(payload, dict) or not hmac.compare_digest(
        _authentication(payload, secret),
        authentication,
    ):
        raise RuntimeError(
            f"Authenticated control record failed verification: {path}"
        )
    return payload


def _read_secret(layout: WorkspaceLayout, operation_id: str) -> str:
    path = _secret_path(layout, operation_id)
    _require_regular_file(path, description="uninstall handoff secret")
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ManagerError(
            "invalid_uninstall_handoff",
            "The protected uninstall handoff secret is unavailable.",
            {"operation_id": operation_id},
            500,
        ) from error
    if len(secret) < 32:
        raise ManagerError(
            "invalid_uninstall_handoff",
            "The protected uninstall handoff secret is invalid.",
            {"operation_id": operation_id},
            500,
        )
    return secret


def read_uninstall_handoff(
    layout: WorkspaceLayout,
    operation_id: str,
) -> tuple[UninstallHandoffEnvelope, Path]:
    operation_id = _validated_operation_id(operation_id)
    _safe_control_root(layout)
    external = _external_descriptor_path(layout, operation_id)
    state = _state_descriptor_path(layout, operation_id)
    _require_regular_file(
        external,
        description="external uninstall descriptor",
    )
    _require_regular_file(
        state,
        description="state uninstall descriptor",
    )
    path = external if external.is_file() else state
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        envelope = UninstallHandoffEnvelope.model_validate(raw)
    except Exception as error:
        raise ManagerError(
            "invalid_uninstall_handoff",
            "The pending uninstall descriptor is missing or invalid.",
            {"operation_id": operation_id, "reason": str(error)},
            500,
        ) from error
    payload = envelope.payload
    if (
        payload.operation_id != operation_id
        or Path(payload.workspace).resolve(strict=False) != layout.workspace
    ):
        raise ManagerError(
            "invalid_uninstall_handoff",
            "The pending uninstall belongs to another operation or workspace.",
            {"operation_id": operation_id},
            500,
        )
    secret = _read_secret(layout, operation_id)
    expected = _authentication(payload.model_dump(mode="json"), secret)
    if not hmac.compare_digest(expected, envelope.authentication):
        raise ManagerError(
            "invalid_uninstall_handoff",
            "The pending uninstall descriptor failed authentication.",
            {"operation_id": operation_id},
            500,
        )
    root = layout.root.resolve(strict=False)
    for target in payload.targets:
        selected = Path(target).resolve(strict=False)
        if selected == root or not layout.contains(root, selected):
            raise ManagerError(
                "invalid_uninstall_handoff",
                "The pending uninstall contains an unsafe target.",
                {"operation_id": operation_id, "path": str(selected)},
                500,
            )
        if (
            not payload.purge_data
            and (
                selected == layout.data.resolve(strict=False)
                or layout.contains(layout.data, selected)
            )
        ):
            raise ManagerError(
                "invalid_uninstall_handoff",
                "A preserve-data uninstall contains a data deletion target.",
                {"operation_id": operation_id, "path": str(selected)},
                500,
            )
    control = _safe_control_root(layout).resolve(strict=False)
    if (
        Path(payload.status_path).resolve(strict=False)
        != uninstall_status_path(layout, operation_id).resolve(strict=False)
        or Path(payload.quarantine_path).resolve(strict=False)
        != (control / f"{operation_id}.quarantine")
    ):
        raise ManagerError(
            "invalid_uninstall_handoff",
            "The pending uninstall control paths are unsafe.",
            {"operation_id": operation_id},
            500,
        )
    cleanup = Path(payload.cleanup_executable).resolve(strict=False)
    if payload.cleanup_mode == "native_launcher":
        expected_cleanup = _cleanup_launcher_path(
            layout,
            operation_id,
        ).resolve(strict=False)
        if (
            cleanup != expected_cleanup
            or payload.cleanup_sha256 is None
            or layout.contains(layout.root, cleanup)
        ):
            raise ManagerError(
                "invalid_uninstall_handoff",
                "The pending uninstall cleanup launcher path is unsafe.",
                {"operation_id": operation_id, "path": str(cleanup)},
                500,
            )
        _require_regular_file(
            cleanup,
            description="external cleanup launcher",
        )
        if _file_sha256(cleanup) != payload.cleanup_sha256:
            raise ManagerError(
                "invalid_uninstall_handoff",
                "The external cleanup launcher failed digest verification.",
                {"operation_id": operation_id, "path": str(cleanup)},
                500,
            )
        if os.name != "nt" and not os.access(cleanup, os.X_OK):
            raise ManagerError(
                "invalid_uninstall_handoff",
                "The external cleanup launcher is not executable.",
                {"operation_id": operation_id, "path": str(cleanup)},
                500,
            )
    elif (
        cleanup != Path(payload.old_python).resolve(strict=False)
        or layout.contains(layout.root, cleanup)
        or payload.cleanup_sha256 is not None
    ):
        raise ManagerError(
            "invalid_uninstall_handoff",
            "The pending uninstall Python cleanup runtime is unsafe.",
            {"operation_id": operation_id, "path": str(cleanup)},
            500,
        )
    else:
        _require_regular_file(
            cleanup,
            description="external cleanup Python runtime",
        )
    return envelope, path


def _safe_targets(
    layout: WorkspaceLayout,
    store: ManagerStore,
    *,
    purge_data: bool,
) -> tuple[Path, ...]:
    root = layout.root.resolve(strict=False)
    data = layout.data.resolve(strict=False)
    candidates = [
        layout.root / "app",
        layout.root / "manager",
        layout.services,
        layout.environments,
        layout.bin,
        layout.cache,
        layout.logs,
        layout.state,
    ]
    if purge_data:
        candidates.append(layout.data)
    for record in store.owned_paths():
        selected = Path(str(record["path"])).resolve(strict=False)
        if selected == root or not layout.contains(root, selected):
            raise ManagerError(
                "unsafe_ownership_manifest",
                "Uninstall refuses an ownership record outside the managed root.",
                {"path": str(selected)},
                409,
            )
        if not purge_data and (
            selected == data or layout.contains(data, selected)
        ):
            continue
        candidates.append(selected)
    for candidate in candidates:
        if os.path.lexists(candidate) and _is_link_or_junction(candidate):
            raise ManagerError(
                "unsafe_uninstall_target",
                "Uninstall refuses a top-level symbolic link or junction.",
                {"path": str(candidate)},
                409,
            )
        resolved = candidate.resolve(strict=False)
        if resolved == root or not layout.contains(root, resolved):
            raise ManagerError(
                "unsafe_uninstall_target",
                "Uninstall refuses a target outside the managed root.",
                {"path": str(resolved)},
                409,
            )
    unique = sorted(
        {path.resolve(strict=False) for path in candidates},
        key=lambda path: (len(path.parts), str(path)),
    )
    selected: list[Path] = []
    for candidate in unique:
        if any(
            candidate == parent or layout.contains(parent, candidate)
            for parent in selected
        ):
            continue
        selected.append(candidate)
    # Move state last so the authoritative journal remains available until
    # every other target is safely quarantined.
    selected.sort(
        key=lambda path: (
            path == layout.state.resolve(strict=False),
            str(path),
        )
    )
    return tuple(selected)


def prepare_uninstall_handoff(
    *,
    layout: WorkspaceLayout,
    store: ManagerStore,
    operation_id: str,
    expected_revision: int,
    purge_data: bool,
    export_data: str | None,
    prior_services: Mapping[str, Any],
) -> dict[str, Any]:
    operation_id = _validated_operation_id(operation_id)
    cleanup_runtime = external_cleanup_runtime(layout)
    if cleanup_runtime is None:
        raise ManagerError(
            "stable_uninstall_helper_unavailable",
            "The active cleanup runtime is inside the installation.",
            http_status=409,
        )
    operation = store.get_operation(operation_id)
    plan = store.get_plan(operation.plan_id)
    impact = plan.impacts.get("uninstall")
    if (
        operation.kind != OperationKind.UNINSTALL
        or operation.state != OperationState.RUNNING
        or plan.kind != OperationKind.UNINSTALL
        or plan.workspace != str(layout.workspace)
        or plan.expected_revision != expected_revision
        or not isinstance(impact, dict)
        or bool(impact.get("purge_data")) != bool(purge_data)
        or impact.get("export_data") != export_data
    ):
        raise ManagerError(
            "invalid_uninstall_handoff",
            "The pending uninstall does not match its reviewed plan.",
            {"operation_id": operation_id},
            500,
        )
    targets = _safe_targets(layout, store, purge_data=purge_data)
    control = _safe_control_root(layout, create=True)
    secret_path = _secret_path(layout, operation_id)
    external = _external_descriptor_path(layout, operation_id)
    state = _state_descriptor_path(layout, operation_id)
    status = uninstall_status_path(layout, operation_id)
    quarantine = control / f"{operation_id}.quarantine"
    if external.is_file():
        envelope, _ = read_uninstall_handoff(layout, operation_id)
        payload = envelope.payload
        if (
            payload.expected_revision != expected_revision
            or payload.purge_data != purge_data
            or payload.export_data != export_data
            or payload.targets != tuple(str(path) for path in targets)
        ):
            raise ManagerError(
                "invalid_uninstall_handoff",
                "The existing uninstall handoff differs from the reviewed plan.",
                {"operation_id": operation_id},
                500,
            )
    else:
        if cleanup_runtime.mode == "native_launcher":
            cleanup_runtime = stage_cleanup_launcher(
                cleanup_runtime,
                _cleanup_launcher_path(layout, operation_id),
            )
        secret = secrets.token_urlsafe(48)
        _write_secret(secret_path, secret)
        current = psutil.Process()
        autostart = autostart_adapter(layout).status()
        serialized_services = {
            str(service_id): dict(value)
            for service_id, value in prior_services.items()
            if isinstance(value, Mapping)
        }
        payload = UninstallHandoffPayload(
            operation_id=operation_id,
            workspace=str(layout.workspace),
            expected_revision=expected_revision,
            purge_data=purge_data,
            export_data=export_data,
            targets=tuple(str(path) for path in targets),
            prior_services=serialized_services,
            old_python=str(current_runtime_executable()),
            old_runtime_mode=(
                "native_launcher"
                if bool(getattr(sys, "frozen", False))
                else "python"
            ),
            old_cwd=str(Path.cwd().resolve(strict=False)),
            old_pid=current.pid,
            old_create_time=current.create_time(),
            cleanup_mode=cleanup_runtime.mode,
            cleanup_executable=str(cleanup_runtime.executable),
            cleanup_sha256=cleanup_runtime.sha256,
            autostart_installed=autostart.installed,
            autostart_enabled=autostart.enabled,
            status_path=str(status),
            quarantine_path=str(quarantine),
            created_at=datetime.now(timezone.utc),
        )
        raw_payload = payload.model_dump(mode="json")
        _write_authenticated(external, raw_payload, secret)
        _write_authenticated(state, raw_payload, secret)
    return {
        "external_handoff_pending": True,
        "handoff_kind": "uninstall",
        "handoff_descriptor": str(external),
        "status_path": str(status),
        "purge_data": purge_data,
        "preserved_data": None if purge_data else str(layout.data),
    }


def rollback_prepared_uninstall(
    *,
    layout: WorkspaceLayout,
    operation_id: str,
) -> None:
    operation_id = _validated_operation_id(operation_id)
    for path in (
        _state_descriptor_path(layout, operation_id),
        _external_descriptor_path(layout, operation_id),
        _secret_path(layout, operation_id),
        _commit_path(layout, operation_id),
        _cleanup_launcher_path(layout, operation_id),
        _cleanup_script_path(layout, operation_id),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    control = _safe_control_root(layout)
    try:
        control.rmdir()
    except OSError:
        pass


class UninstallHandoffCoordinator:
    def __init__(
        self,
        layout: WorkspaceLayout,
        *,
        shutdown_callback: Callable[[], None],
    ) -> None:
        self.layout = layout
        self.shutdown_callback = shutdown_callback

    def __call__(self, _execution, result: dict[str, Any]) -> None:
        operation_id = Path(str(result["handoff_descriptor"])).name.removesuffix(
            ".pending.json"
        )
        envelope, _ = read_uninstall_handoff(self.layout, operation_id)
        payload = envelope.payload
        control = _safe_control_root(self.layout)
        log_path = control / "uninstall.log"
        protect_path(control, directory=True)
        log = log_path.open("ab", buffering=0)
        options = (
            {
                "creationflags": (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                )
            }
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            process = subprocess.Popen(
                _uninstall_command(payload),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=False,
                cwd=str(control),
                **options,
            )
        finally:
            log.close()
        if process.poll() is not None:
            raise RuntimeError("Uninstall helper exited before startup.")
        self.shutdown_callback()


def _uninstall_command(payload: UninstallHandoffPayload) -> list[str]:
    from .launcher import LauncherRuntime

    return runtime_command(
        LauncherRuntime(
            mode=payload.cleanup_mode,
            executable=Path(payload.cleanup_executable),
            sha256=payload.cleanup_sha256,
        ),
        action="uninstall",
        workspace=Path(payload.workspace),
        operation_id=payload.operation_id,
    )


def uninstall_helper_command(
    layout: WorkspaceLayout,
    operation_id: str,
) -> list[str]:
    """Return the authenticated, path-validated pending cleanup command."""

    envelope, _ = read_uninstall_handoff(layout, operation_id)
    return _uninstall_command(envelope.payload)


def _wait_for_old_manager(
    payload: UninstallHandoffPayload,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(payload.old_pid)
            if (
                abs(process.create_time() - payload.old_create_time) > 0.01
                or Path(process.exe()).resolve(strict=False)
                != Path(payload.old_python).resolve(strict=False)
            ):
                return
        except psutil.NoSuchProcess:
            return
        except (psutil.AccessDenied, OSError) as error:
            raise RuntimeError(
                "The retiring manager process identity could not be verified."
            ) from error
        time.sleep(0.1)
    raise TimeoutError("The retiring manager did not exit before uninstall.")


def _database_for_resume(
    layout: WorkspaceLayout,
    payload: UninstallHandoffPayload,
) -> Path | None:
    if layout.database.is_file():
        return layout.database
    root = layout.root.resolve(strict=False)
    state = layout.state.resolve(strict=False)
    try:
        relative = state.relative_to(root)
    except ValueError:
        return None
    quarantined = Path(payload.quarantine_path) / relative / layout.database.name
    return quarantined if quarantined.is_file() else None


def _assert_services_stopped(store: ManagerStore) -> None:
    live: list[str] = []
    unverifiable: list[str] = []
    for service in store.list_services():
        if service.process is None:
            continue
        try:
            process = validate_identity(service.process)
        except (IdentityMismatch, IdentityInspectionFailed):
            unverifiable.append(service.id)
            continue
        if process is not None:
            live.append(service.id)
    if live or unverifiable:
        raise RuntimeError(
            "Uninstall refuses to remove files while managed service "
            "identities remain live or unverifiable: "
            + ", ".join(sorted((*live, *unverifiable)))
        )


def _relative_target(layout: WorkspaceLayout, target: Path) -> Path:
    try:
        return target.resolve(strict=False).relative_to(
            layout.root.resolve(strict=False)
        )
    except ValueError as error:
        raise RuntimeError(f"Unsafe uninstall target: {target}") from error


def _move_to_quarantine(
    layout: WorkspaceLayout,
    payload: UninstallHandoffPayload,
) -> list[tuple[Path, Path]]:
    quarantine = Path(payload.quarantine_path).resolve(strict=False)
    quarantine.mkdir(parents=True, exist_ok=True)
    protect_path(quarantine, directory=True)
    moved: list[tuple[Path, Path]] = []
    for raw in payload.targets:
        source = Path(raw).resolve(strict=False)
        destination = quarantine / _relative_target(layout, source)
        if destination.exists() and not source.exists():
            moved.append((source, destination))
            continue
        if destination.exists() and source.exists():
            raise RuntimeError(
                f"Both uninstall source and quarantine target exist: {source}"
            )
        if not source.exists() and not source.is_symlink():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        moved.append((source, destination))
    return moved


def _restore_quarantine(moved: list[tuple[Path, Path]]) -> None:
    failures: list[str] = []
    for source, destination in reversed(moved):
        if not destination.exists() and not destination.is_symlink():
            continue
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            if source.exists() or source.is_symlink():
                raise RuntimeError("restore destination already exists")
            os.replace(destination, source)
        except Exception as error:
            failures.append(f"{source}: {error}")
    if failures:
        raise RuntimeError(
            "Uninstall quarantine could not be fully restored: "
            + "; ".join(failures)
        )


def _remove_quarantine(path: Path, *, attempts: int = 30) -> None:
    """Remove a committed tree while tolerating concurrent child disappearance."""

    deletion_path = path
    if os.name == "nt":
        # Quarantining a normally valid installation path adds enough prefix
        # that descendants can cross legacy MAX_PATH even when long-path
        # policy is disabled.  Win32's extended-length syntax makes recursive
        # deletion independent of that machine-wide policy.
        absolute = str(path.resolve(strict=False))
        if not absolute.startswith("\\\\?\\"):
            absolute = (
                "\\\\?\\UNC\\" + absolute[2:]
                if absolute.startswith("\\\\")
                else "\\\\?\\" + absolute
            )
        deletion_path = Path(absolute)
    for attempt in range(max(1, attempts)):
        if not os.path.lexists(deletion_path):
            return
        try:
            shutil.rmtree(deletion_path)
            return
        except FileNotFoundError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(0.5, 0.05 * (attempt + 1)))
            continue
        except OSError as error:
            retryable = bool(
                error.errno == errno.ENOTEMPTY
                or (
                    os.name == "nt"
                    and (
                        error.errno in {errno.EACCES, errno.EPERM}
                        or getattr(error, "winerror", None)
                        in {5, 32, 145}
                    )
                )
            )
            if not retryable or attempt + 1 >= attempts:
                raise
            time.sleep(min(0.5, 0.05 * (attempt + 1)))


def _restore_service_desires(
    store: ManagerStore,
    payload: UninstallHandoffPayload,
) -> None:
    for value in payload.prior_services.values():
        try:
            service = ManagedService.model_validate(value)
        except Exception:
            continue
        service.process = None
        service.health = HealthResult(
            state=HealthState.STOPPED,
            service_id=service.id,
        )
        store.save_service(service)


def _mark_failed(
    store: ManagerStore,
    payload: UninstallHandoffPayload,
    error: Exception,
) -> None:
    operation = store.get_operation(payload.operation_id)
    operation.state = OperationState.FAILED
    operation.current_task_id = None
    operation.error_code = "uninstall_handoff_failed"
    operation.error_message = str(error)[:2000]
    operation.finished_at = datetime.now(timezone.utc)
    operation.updated_at = operation.finished_at
    operation.recovery = {"uninstall_quarantine_restored": True}
    store.update_operation(operation)
    for record in store.operation_tasks(payload.operation_id):
        if record.task.kind not in {
            "stop_all_services",
            "export_uninstall_data",
            "prepare_uninstall_handoff",
        }:
            continue
        store.update_operation_task(
            payload.operation_id,
            record.task.id,
            state=TaskState.ROLLED_BACK,
            attempt=record.attempt,
            result=record.result,
            error={
                "code": "uninstall_handoff_failed",
                "message": str(error)[:2000],
            },
            started_at=record.started_at,
            finished_at=operation.finished_at,
        )
    store.append_event(
        "operation.failed",
        {
            "operation_id": payload.operation_id,
            "error": {
                "code": "uninstall_handoff_failed",
                "message": str(error)[:2000],
            },
            "rollback_errors": [],
        },
        operation_id=payload.operation_id,
    )


def _restart_previous_manager(payload: UninstallHandoffPayload) -> None:
    options = (
        {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    command = (
        [
            payload.old_python,
            "daemon",
            "--workspace",
            payload.workspace,
            "--handoff-child",
            payload.operation_id,
        ]
        if payload.old_runtime_mode == "native_launcher"
        else [
            payload.old_python,
            "-m",
            "pandrator_manager.daemon",
            "--workspace",
            payload.workspace,
            "--handoff-child",
            payload.operation_id,
        ]
    )
    subprocess.Popen(
        command,
        cwd=payload.old_cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        **options,
    )


def _write_status(
    path: Path,
    payload: Mapping[str, Any],
) -> None:
    _atomic_json(path, dict(payload))
    protect_path(path.parent, directory=True)
    protect_path(path)


def _cleanup_control(
    layout: WorkspaceLayout,
    operation_id: str,
) -> None:
    for path in (
        _external_descriptor_path(layout, operation_id),
        _state_descriptor_path(layout, operation_id),
        _secret_path(layout, operation_id),
        _commit_path(layout, operation_id),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _retire_cleanup_runtime(
    layout: WorkspaceLayout,
    payload: UninstallHandoffPayload,
) -> str | None:
    """Remove the operation-specific native helper after this process exits."""

    if payload.cleanup_mode != "native_launcher":
        return None
    executable = Path(payload.cleanup_executable).resolve(strict=False)
    expected = _cleanup_launcher_path(
        layout,
        payload.operation_id,
    ).resolve(strict=False)
    if executable != expected:
        return "cleanup launcher path no longer matches the operation"
    try:
        if Path(sys.executable).resolve(strict=False) != executable:
            executable.unlink(missing_ok=True)
            return None
        if os.name != "nt":
            # POSIX permits unlinking a running executable; the mapped image
            # remains valid until this process exits.
            executable.unlink(missing_ok=True)
            return None

        script = _cleanup_script_path(layout, payload.operation_id)
        _require_regular_file(
            executable,
            description="external cleanup launcher",
        )
        script_payload = "\r\n".join(
            (
                "@echo off",
                "setlocal DisableDelayedExpansion",
                "set /a PANDRATOR_DELETE_ATTEMPT=0",
                ":retry",
                'del /f /q "%PANDRATOR_CLEANUP_EXECUTABLE%" >nul 2>&1',
                'if not exist "%PANDRATOR_CLEANUP_EXECUTABLE%" goto done',
                "set /a PANDRATOR_DELETE_ATTEMPT+=1",
                "if %PANDRATOR_DELETE_ATTEMPT% GEQ 120 goto done",
                "ping 127.0.0.1 -n 2 >nul",
                "goto retry",
                ":done",
                'del /f /q "%~f0" >nul 2>&1',
                'rmdir "%PANDRATOR_CLEANUP_DIRECTORY%" >nul 2>&1',
                "",
            )
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{script.name}.",
            suffix=".tmp",
            dir=script.parent,
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                newline="",
            ) as handle:
                handle.write(script_payload)
                handle.flush()
                os.fsync(handle.fileno())
            protect_path(temporary)
            os.replace(temporary, script)
            protect_path(script)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        environment = dict(os.environ)
        environment["PANDRATOR_CLEANUP_EXECUTABLE"] = str(executable)
        environment["PANDRATOR_CLEANUP_DIRECTORY"] = str(script.parent)
        subprocess.Popen(
            [
                environment.get("COMSPEC", "cmd.exe"),
                "/d",
                "/s",
                "/c",
                f'call "{script}"',
            ],
            cwd=str(script.parent),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            ),
        )
        return None
    except Exception as error:
        return str(error)[:1000]


def run_uninstall_handoff(
    workspace: str | Path,
    operation_id: str,
    *,
    shutdown_timeout: float = 60,
) -> int:
    layout = WorkspaceLayout.from_value(workspace)
    operation_id = _validated_operation_id(operation_id)
    _safe_control_root(layout)
    envelope, _descriptor = read_uninstall_handoff(layout, operation_id)
    payload = envelope.payload
    status_path = Path(payload.status_path)
    quarantine = Path(payload.quarantine_path)
    commit_path = _commit_path(layout, operation_id)
    moved: list[tuple[Path, Path]] = []
    autostart_removed = False
    try:
        _wait_for_old_manager(payload, shutdown_timeout)
        if commit_path.is_file():
            secret = _read_secret(layout, operation_id)
            commit = _read_authenticated(commit_path, secret)
            if (
                commit.get("operation_id") != operation_id
                or commit.get("targets") != list(payload.targets)
            ):
                raise RuntimeError(
                    "The uninstall commit marker does not match the handoff."
                )
            cleanup_error = None
            try:
                if quarantine.exists():
                    _remove_quarantine(quarantine)
            except Exception as error:
                cleanup_error = str(error)
            helper_error = _retire_cleanup_runtime(layout, payload)
            if helper_error:
                cleanup_error = "; ".join(
                    value
                    for value in (
                        cleanup_error,
                        f"cleanup launcher: {helper_error}",
                    )
                    if value
                )
            _write_status(
                status_path,
                {
                    "status": (
                        "succeeded_with_cleanup_residue"
                        if cleanup_error
                        else "succeeded"
                    ),
                    "operation_id": operation_id,
                    "purged_data": payload.purge_data,
                    "preserved_data": (
                        None if payload.purge_data else str(layout.data)
                    ),
                    "export_data": payload.export_data,
                    "cleanup_residue": (
                        (
                            str(quarantine)
                            if quarantine.exists()
                            else payload.cleanup_executable
                        )
                        if cleanup_error
                        else None
                    ),
                    "message": cleanup_error,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            _cleanup_control(layout, operation_id)
            return 0
        database = _database_for_resume(layout, payload)
        if database is None:
            raise RuntimeError(
                "The manager state database is unavailable for uninstall recovery."
            )
        store = ManagerStore(database)
        operation = store.get_operation(operation_id)
        if operation.state != OperationState.HANDOFF_PENDING:
            raise RuntimeError(
                f"Uninstall operation has unexpected state {operation.state}."
            )
        _assert_services_stopped(store)
        integration = autostart_adapter(layout)
        if integration.status().installed:
            integration.remove()
        autostart_removed = payload.autostart_installed
        moved = _move_to_quarantine(layout, payload)
        secret = _read_secret(layout, operation_id)
        _write_authenticated(
            commit_path,
            {
                "operation_id": operation_id,
                "committed_at": datetime.now(timezone.utc).isoformat(),
                "targets": list(payload.targets),
            },
            secret,
        )
        cleanup_error = None
        try:
            if quarantine.exists():
                _remove_quarantine(quarantine)
        except Exception as error:
            cleanup_error = str(error)
        helper_error = _retire_cleanup_runtime(layout, payload)
        if helper_error:
            cleanup_error = "; ".join(
                value
                for value in (
                    cleanup_error,
                    f"cleanup launcher: {helper_error}",
                )
                if value
            )
        try:
            layout.root.rmdir()
        except OSError:
            pass
        _write_status(
            status_path,
            {
                "status": (
                    "succeeded_with_cleanup_residue"
                    if cleanup_error
                    else "succeeded"
                ),
                "operation_id": operation_id,
                "purged_data": payload.purge_data,
                "preserved_data": (
                    None if payload.purge_data else str(layout.data)
                ),
                "export_data": payload.export_data,
                "cleanup_residue": (
                    (
                        str(quarantine)
                        if quarantine.exists()
                        else payload.cleanup_executable
                    )
                    if cleanup_error
                    else None
                ),
                "message": cleanup_error,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        _cleanup_control(layout, operation_id)
        return 0
    except Exception as error:
        if commit_path.is_file():
            # Every selected target crossed the same-volume quarantine commit
            # boundary. Never attempt to resurrect a partially deleted
            # quarantine; leave the authenticated pending records so the next
            # launcher can finish cleanup safely.
            try:
                _write_status(
                    status_path,
                    {
                        "status": "cleanup_interrupted",
                        "operation_id": operation_id,
                        "error": str(error)[:2000],
                        "quarantine": str(quarantine),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception:
                pass
            return 3
        try:
            _restore_quarantine(moved)
            restored_database = layout.database
            if restored_database.is_file():
                restored = ManagerStore(restored_database)
                _restore_service_desires(restored, payload)
                _mark_failed(restored, payload, error)
            if autostart_removed and payload.autostart_installed:
                autostart_adapter(layout).restore(
                    enabled=payload.autostart_enabled
                )
            if payload.export_data:
                try:
                    Path(payload.export_data).unlink()
                except FileNotFoundError:
                    pass
            _write_status(
                status_path,
                {
                    "status": "failed",
                    "operation_id": operation_id,
                    "error": str(error)[:2000],
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            _cleanup_control(layout, operation_id)
            _restart_previous_manager(payload)
            _retire_cleanup_runtime(layout, payload)
        except Exception as rollback_error:
            _write_status(
                status_path,
                {
                    "status": "recovery_required",
                    "operation_id": operation_id,
                    "error": str(error)[:2000],
                    "rollback_error": str(rollback_error)[:2000],
                    "quarantine": str(quarantine),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return 3
        return 2


def pending_uninstalls(layout: WorkspaceLayout) -> tuple[str, ...]:
    root = _safe_control_root(layout)
    if not os.path.lexists(root):
        return ()
    pending: list[str] = []
    for path in root.glob("*.pending.json"):
        if _is_link_or_junction(path) or not path.is_file():
            raise ManagerError(
                "unsafe_uninstall_control",
                "A pending uninstall record is not a regular file.",
                {"path": str(path)},
                409,
            )
        operation_id = path.name.removesuffix(".pending.json")
        pending.append(_validated_operation_id(operation_id))
    return tuple(sorted(pending))


def read_uninstall_status(
    layout: WorkspaceLayout,
    operation_id: str,
) -> dict[str, Any] | None:
    _safe_control_root(layout)
    path = uninstall_status_path(layout, operation_id)
    _require_regular_file(path, description="uninstall status record")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def uninstall_statuses(
    layout: WorkspaceLayout,
) -> tuple[dict[str, Any], ...]:
    """Return external uninstall outcomes without recreating manager state."""

    root = _safe_control_root(layout)
    if not os.path.lexists(root):
        return ()
    records: list[tuple[float, dict[str, Any]]] = []
    for path in root.glob("*.status.json"):
        _require_regular_file(path, description="uninstall status record")
        operation_id = path.name.removesuffix(".status.json")
        _validated_operation_id(operation_id)
        status = read_uninstall_status(layout, operation_id)
        if status is None or status.get("operation_id") != operation_id:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        records.append((modified, status))
    records.sort(key=lambda item: item[0], reverse=True)
    return tuple(status for _modified, status in records)


def recover_uninstall_cleanup(
    layout: WorkspaceLayout,
    operation_id: str,
) -> dict[str, Any] | None:
    """Retry deletion of an exact, operation-owned post-commit residue.

    A successful quarantine commit means the product is already uninstalled,
    but Windows scanners can hold an archive or the native cleanup image
    briefly.  Status records are deliberately retained until this function can
    prove that the recorded residue is one of this operation's derived paths
    and that it no longer exists.
    """

    operation_id = _validated_operation_id(operation_id)
    status = read_uninstall_status(layout, operation_id)
    if status is None:
        return None
    if status.get("operation_id") != operation_id:
        return status
    if status.get("status") != "succeeded_with_cleanup_residue":
        return status

    raw_residue = status.get("cleanup_residue")
    if not isinstance(raw_residue, str) or not raw_residue:
        return status
    residue = Path(raw_residue).resolve(strict=False)
    control = _safe_control_root(layout).resolve(strict=False)
    quarantine = (control / f"{operation_id}.quarantine").resolve(
        strict=False
    )
    cleanup_launcher = _cleanup_launcher_path(
        layout,
        operation_id,
    ).resolve(strict=False)
    if residue not in {quarantine, cleanup_launcher}:
        return status

    try:
        if residue == quarantine:
            _remove_quarantine(quarantine)
        else:
            for attempt in range(30):
                try:
                    cleanup_launcher.unlink(missing_ok=True)
                    break
                except OSError as error:
                    retryable = bool(
                        os.name == "nt"
                        and (
                            error.errno in {errno.EACCES, errno.EPERM}
                            or getattr(error, "winerror", None) in {5, 32}
                        )
                    )
                    if not retryable or attempt == 29:
                        raise
                    time.sleep(min(0.5, 0.05 * (attempt + 1)))
    except OSError:
        return status
    if os.path.lexists(residue):
        return status

    recovered = dict(status)
    recovered.update(
        {
            "status": "succeeded",
            "cleanup_residue": None,
            "message": None,
            "cleanup_recovered_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_status(
        uninstall_status_path(layout, operation_id),
        recovered,
    )
    return recovered


def clear_uninstall_status(
    layout: WorkspaceLayout,
    operation_id: str,
) -> dict[str, Any] | None:
    status = recover_uninstall_cleanup(layout, operation_id)
    if (
        status is not None
        and status.get("status") == "succeeded_with_cleanup_residue"
    ):
        # Preserve the journal until the exact recorded residue can be
        # removed.  This makes a later CLI/launcher invocation recoverable.
        return status
    root = _safe_control_root(layout)

    def unlink_after_helper_exit(path: Path) -> bool:
        """Remove a helper-owned file without racing its final status write."""

        for attempt in range(30):
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return True
            except OSError as error:
                retryable = bool(
                    os.name == "nt"
                    and (
                        error.errno in {errno.EACCES, errno.EPERM}
                        or getattr(error, "winerror", None) in {5, 32}
                    )
                )
                if not retryable or attempt == 29:
                    return False
                time.sleep(min(0.5, 0.05 * (attempt + 1)))
        return False  # pragma: no cover - the bounded loop always returns

    # The helper publishes terminal status just before exiting, while stdout
    # can still hold uninstall.log open on Windows. Keep the status journal
    # until those handles close so a later invocation can safely retry.
    for log_name in ("uninstall.log", "uninstall-launch.log"):
        if not unlink_after_helper_exit(root / log_name):
            return status

    try:
        uninstall_status_path(layout, operation_id).unlink()
    except FileNotFoundError:
        pass
    for path in (
        _cleanup_launcher_path(layout, operation_id),
        _cleanup_script_path(layout, operation_id),
    ):
        try:
            path.unlink()
        except OSError:
            # A running Windows cleanup image remains locked briefly. Its
            # bounded self-delete script removes it after this process exits.
            pass
    try:
        root.rmdir()
    except OSError:
        pass
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pandrator-manager-uninstall")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    try:
        return run_uninstall_handoff(args.workspace, args.operation_id)
    except Exception as error:
        print(f"Uninstall handoff failed: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
