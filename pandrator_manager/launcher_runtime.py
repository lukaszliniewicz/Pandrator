"""Backend runtime selection, native launcher validation and command construction."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from . import __version__
from .auth import protect_path
from .context import WorkspaceLayout
from .errors import ManagerError

LAUNCHER_METADATA_NAME = "launcher.json"

LAUNCHER_SCHEMA_VERSION = 1

MAXIMUM_LAUNCHER_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LauncherRuntime:
    """A runtime that can survive the operation it coordinates."""

    mode: Literal["native_launcher", "python"]
    executable: Path
    sha256: str | None = None


def current_runtime_executable() -> Path:
    """Return the invocation path without dereferencing a virtualenv symlink."""

    return Path(os.path.abspath(os.path.expanduser(sys.executable)))


def launcher_filename() -> str:
    return "pandrator-manager-launcher.exe" if os.name == "nt" else "pandrator-manager-launcher"


def stable_launcher_path(layout: WorkspaceLayout) -> Path:
    return layout.bin / launcher_filename()


def launcher_metadata_path(layout: WorkspaceLayout) -> Path:
    return layout.bin / LAUNCHER_METADATA_NAME


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction is not None and junction())


def _require_real_directory(path: Path, *, create: bool = False) -> Path:
    if create and not os.path.lexists(path):
        path.mkdir(parents=True, mode=0o700)
    if not os.path.lexists(path) or _is_link_or_junction(path) or not path.is_dir():
        raise ManagerError(
            "unsafe_stable_launcher",
            "The stable launcher directory is missing or redirected.",
            {"path": str(path)},
            409,
        )
    lexical = Path(os.path.abspath(os.fspath(path)))
    if path.resolve(strict=True) != lexical:
        raise ManagerError(
            "unsafe_stable_launcher",
            "The stable launcher directory resolves unexpectedly.",
            {"path": str(path)},
            409,
        )
    return path


def _require_regular_file(path: Path, *, description: str) -> None:
    if not os.path.lexists(path) or _is_link_or_junction(path) or not path.is_file():
        raise ManagerError(
            "unsafe_stable_launcher",
            f"The {description} is not a regular file.",
            {"path": str(path)},
            409,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _protect_executable(path: Path) -> None:
    if os.name == "nt":
        protect_path(path)
        return
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def installed_launcher(
    layout: WorkspaceLayout,
    *,
    strict: bool = False,
) -> LauncherRuntime | None:
    """Return the installed launcher only when its local manifest matches."""

    executable = stable_launcher_path(layout)
    metadata = launcher_metadata_path(layout)
    if not os.path.lexists(executable) and not os.path.lexists(metadata):
        return None
    try:
        _require_real_directory(layout.bin)
        _require_regular_file(
            executable,
            description="stable launcher executable",
        )
        _require_regular_file(
            metadata,
            description="stable launcher metadata",
        )
        raw = json.loads(metadata.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != LAUNCHER_SCHEMA_VERSION
            or raw.get("filename") != executable.name
            or not isinstance(raw.get("sha256"), str)
            or len(raw["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in raw["sha256"])
            or not isinstance(raw.get("size_bytes"), int)
            or raw["size_bytes"] <= 0
            or raw["size_bytes"] > MAXIMUM_LAUNCHER_BYTES
        ):
            raise ValueError("launcher metadata is invalid")
        metadata_workspace = raw.get("workspace")
        if metadata_workspace is not None and (
            not isinstance(metadata_workspace, str)
            or Path(metadata_workspace).expanduser().resolve(strict=False) != layout.workspace
        ):
            raise ValueError("launcher workspace does not match metadata")
        if executable.stat().st_size != raw["size_bytes"]:
            raise ValueError("launcher size does not match metadata")
        digest = _sha256(executable)
        if digest != raw["sha256"]:
            raise ValueError("launcher digest does not match metadata")
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise ValueError("launcher is not executable")
        return LauncherRuntime(
            mode="native_launcher",
            executable=executable.resolve(strict=True),
            sha256=digest,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        if strict:
            raise ManagerError(
                "invalid_stable_launcher",
                "The installed stable launcher failed validation.",
                {
                    "path": str(executable),
                    "reason": str(error),
                },
                409,
            ) from error
        return None


def install_stable_launcher(
    layout: WorkspaceLayout,
    *,
    source: Path | None = None,
) -> LauncherRuntime:
    """Install the current frozen bootstrap atomically into ``bin``.

    ``source`` is injectable for packaging tests.  Production callers omit it;
    a source-mode Python interpreter is never copied and mislabeled as a native
    launcher.
    """

    if source is None:
        if not bool(getattr(sys, "frozen", False)):
            raise ManagerError(
                "native_bootstrap_required",
                "Installing the stable launcher requires the packaged native bootstrap executable.",
                {"executable": str(Path(sys.executable).resolve(strict=False))},
                409,
            )
        source = Path(sys.executable)
    selected_source = source.expanduser().resolve(strict=True)
    _require_regular_file(
        selected_source,
        description="bootstrap source executable",
    )
    size = selected_source.stat().st_size
    if size <= 0 or size > MAXIMUM_LAUNCHER_BYTES:
        raise ManagerError(
            "invalid_stable_launcher",
            "The bootstrap executable has an invalid size.",
            {"path": str(selected_source), "size_bytes": size},
            409,
        )
    source_digest = _sha256(selected_source)

    layout.bin.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(layout.bin)
    protect_path(layout.bin, directory=True)
    destination = stable_launcher_path(layout)
    if destination.resolve(strict=False) != selected_source:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=layout.bin,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(selected_source, temporary)
            with temporary.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            _protect_executable(temporary)
            if temporary.stat().st_size != size or _sha256(temporary) != source_digest:
                raise RuntimeError("The staged launcher copy failed digest verification.")
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    _protect_executable(destination)
    _atomic_json(
        launcher_metadata_path(layout),
        {
            "schema_version": LAUNCHER_SCHEMA_VERSION,
            "filename": destination.name,
            "sha256": source_digest,
            "size_bytes": size,
            "manager_version": __version__,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "authenticode_signed": False if os.name == "nt" else None,
            "workspace": str(layout.workspace),
        },
    )
    protect_path(launcher_metadata_path(layout))
    runtime = installed_launcher(layout, strict=True)
    assert runtime is not None
    return runtime


def external_cleanup_runtime(
    layout: WorkspaceLayout,
) -> LauncherRuntime | None:
    """Select a cleanup runtime that will remain after ``layout.root`` moves."""

    stable = installed_launcher(layout)
    if stable is not None:
        return stable
    executable = current_runtime_executable()
    if layout.contains(layout.root, executable):
        return None
    return LauncherRuntime(
        mode=("native_launcher" if bool(getattr(sys, "frozen", False)) else "python"),
        executable=executable,
        sha256=_sha256(executable) if bool(getattr(sys, "frozen", False)) else None,
    )


def native_manager_installation(layout: WorkspaceLayout) -> bool:
    """Whether this workspace is controlled by the native release channel."""

    if installed_launcher(layout) is not None:
        return True
    executable = current_runtime_executable().resolve(strict=False)
    return layout.contains(layout.manager_versions, executable)


def stage_cleanup_launcher(
    runtime: LauncherRuntime,
    destination: Path,
) -> LauncherRuntime:
    """Copy a native launcher to an external, operation-specific path."""

    if runtime.mode != "native_launcher":
        raise ValueError("Only a native launcher can be staged for cleanup.")
    _require_regular_file(
        runtime.executable,
        description="cleanup launcher source",
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(destination.parent)
    if os.path.lexists(destination):
        _require_regular_file(
            destination,
            description="staged cleanup launcher",
        )
        digest = _sha256(destination)
        expected = runtime.sha256 or _sha256(runtime.executable)
        if digest != expected:
            raise ManagerError(
                "invalid_stable_launcher",
                "The existing staged cleanup launcher has another digest.",
                {"path": str(destination)},
                409,
            )
        _protect_executable(destination)
        return LauncherRuntime(
            mode="native_launcher",
            executable=destination.resolve(strict=True),
            sha256=digest,
        )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(runtime.executable, temporary)
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        _protect_executable(temporary)
        expected = runtime.sha256 or _sha256(runtime.executable)
        digest = _sha256(temporary)
        if digest != expected:
            raise RuntimeError("The external cleanup launcher failed digest verification.")
        os.replace(temporary, destination)
        _protect_executable(destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return LauncherRuntime(
        mode="native_launcher",
        executable=destination.resolve(strict=True),
        sha256=digest,
    )


def runtime_command(
    runtime: LauncherRuntime,
    *,
    action: Literal["daemon", "handoff", "uninstall"],
    workspace: Path,
    operation_id: str | None = None,
) -> list[str]:
    if runtime.mode == "native_launcher":
        command = [
            str(runtime.executable),
            action,
            "--workspace",
            str(workspace),
        ]
    else:
        module = {
            "daemon": "pandrator_manager.daemon",
            "handoff": "pandrator_manager.releases.handoff",
            "uninstall": "pandrator_manager.uninstall",
        }[action]
        command = [
            str(runtime.executable),
            "-m",
            module,
            "--workspace",
            str(workspace),
        ]
    if operation_id is not None:
        command.extend(("--operation-id", operation_id))
    return command
