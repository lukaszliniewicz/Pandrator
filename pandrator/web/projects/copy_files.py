"""Verified, private file staging for a frozen project-copy snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO

from pandrator.runtime import DataPaths, PathBoundaryError

from .copy_contracts import CopyFileSource, ProjectCopyError, ProjectCopySnapshot

_CHUNK_BYTES = 1024 * 1024
_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z", re.ASCII)
_SAFE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,16}\Z", re.ASCII)
_ERRORS = {
    "copy_graph_invalid": "The saved project state is incomplete or inconsistent.",
    "copy_file_unavailable": "A source file is unavailable or has changed. Refresh the copy preview.",
    "copy_staging_failed": "Project files could not be prepared for copying.",
}


@dataclass(frozen=True, slots=True)
class StagedCopyFile:
    source_artifact_id: str
    artifact_id: str
    relative_path: str
    content_hash: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class StagedProjectFiles:
    snapshot_fingerprint: str
    directory_relative_path: str
    directory_device: int
    directory_inode: int
    files: tuple[StagedCopyFile, ...]


@dataclass(frozen=True, slots=True)
class _CopyInput:
    source: CopyFileSource
    target_artifact_id: str
    target_suffix: str
    content_hash: str
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    source: _CopyInput
    path: Path
    device: int
    inode: int
    stat_size: int


class _SourceChangedError(Exception):
    """Internal reason for a source that no longer matches its frozen digest."""


def _error(code: str) -> ProjectCopyError:
    return ProjectCopyError(code, _ERRORS[code])


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _canonical_uuid_value(value: object) -> str | None:
    if isinstance(value, str) and _canonical_uuid(value):
        return value
    return None


def _identity(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return value


def _safe_suffix(relative_path: str) -> str:
    suffix = PurePosixPath(relative_path).suffix
    return suffix if _SAFE_SUFFIX.fullmatch(suffix) is not None else ".bin"


def _validate_snapshot(
    snapshot: ProjectCopySnapshot,
    *,
    copy_id: str,
    artifact_ids: Mapping[str, str],
) -> tuple[_CopyInput, ...]:
    if type(snapshot) is not ProjectCopySnapshot or type(snapshot.files) is not tuple:
        raise _error("copy_graph_invalid")
    if type(snapshot.blockers) is not tuple or snapshot.ready is not True:
        raise _error("copy_graph_invalid")
    if not _canonical_uuid(copy_id):
        raise _error("copy_graph_invalid")
    if not isinstance(artifact_ids, Mapping):
        raise _error("copy_graph_invalid")

    state_json = snapshot.state_json
    fingerprint = snapshot.fingerprint
    if type(state_json) is not str or type(fingerprint) is not str:
        raise _error("copy_graph_invalid")
    try:
        state_digest = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as error:
        raise _error("copy_graph_invalid") from error
    if _HEX_SHA256.fullmatch(fingerprint) is None or state_digest != fingerprint:
        raise _error("copy_graph_invalid")
    try:
        state = json.loads(state_json)
        canonical_state = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise _error("copy_graph_invalid") from error
    if canonical_state != state_json or type(state) is not dict:
        raise _error("copy_graph_invalid")
    rows = state.get("rows")
    if type(rows) is not dict or type(rows.get("artifacts")) is not list:
        raise _error("copy_graph_invalid")

    sources: list[CopyFileSource] = []
    source_ids: set[str] = set()
    state_artifacts: dict[str, dict[str, Any]] = {}
    for row in rows["artifacts"]:
        if type(row) is not dict or type(row.get("id")) is not str or not row["id"]:
            raise _error("copy_graph_invalid")
        if row["id"] in state_artifacts:
            raise _error("copy_graph_invalid")
        state_artifacts[row["id"]] = row

    inputs: list[_CopyInput] = []
    for source in snapshot.files:
        if type(source) is not CopyFileSource:
            raise _error("copy_graph_invalid")
        source_id = source.artifact_id
        relative_path = source.relative_path
        if (
            type(source_id) is not str
            or not source_id
            or source_id in source_ids
            or type(relative_path) is not str
            or not relative_path
            or "\x00" in relative_path
            or type(source.content_hash) is not str
            or _HEX_SHA256.fullmatch(source.content_hash) is None
            or (
                source.size_bytes is not None
                and (type(source.size_bytes) is not int or source.size_bytes < 0)
            )
        ):
            raise _error("copy_graph_invalid")
        posix_path, windows_path = PurePosixPath(relative_path), PureWindowsPath(relative_path)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or ".." in posix_path.parts
            or ".." in windows_path.parts
        ):
            raise _error("copy_graph_invalid")
        source_ids.add(source_id)
        sources.append(source)

    if set(artifact_ids) != source_ids or len(artifact_ids) != len(source_ids):
        raise _error("copy_graph_invalid")
    if set(state_artifacts) != source_ids:
        raise _error("copy_graph_invalid")

    target_ids: set[str] = set()
    source_identities = {_identity(source_id) for source_id in source_ids}
    for source in sources:
        target_id = _canonical_uuid_value(artifact_ids.get(source.artifact_id))
        if (
            target_id is None
            or target_id in target_ids
            or _identity(target_id) in source_identities
        ):
            raise _error("copy_graph_invalid")
        target_ids.add(target_id)

        row = state_artifacts[source.artifact_id]
        if (
            row.get("workspace_id") != source.workspace_id
            or row.get("project_id") != source.project_id
            or row.get("session_id") != source.session_id
            or row.get("relative_path") != source.relative_path
            or row.get("content_hash") != source.content_hash
            or row.get("size_bytes") != source.size_bytes
        ):
            raise _error("copy_graph_invalid")
        assert isinstance(source.content_hash, str)
        inputs.append(
            _CopyInput(
                source=source,
                target_artifact_id=target_id,
                target_suffix=_safe_suffix(source.relative_path),
                content_hash=source.content_hash,
                size_bytes=source.size_bytes,
            )
        )
    return tuple(inputs)


def _resolve_sources(
    paths: DataPaths,
    inputs: tuple[_CopyInput, ...],
    check: Callable[[], None],
) -> tuple[tuple[_ResolvedSource, ...], int]:
    resolved: list[_ResolvedSource] = []
    total_bytes = 0
    for item in inputs:
        check()
        try:
            source_path = paths.managed_path(item.source.relative_path)
            source_stat = source_path.stat()
        except (OSError, PathBoundaryError, RuntimeError, ValueError) as error:
            raise _error("copy_file_unavailable") from error
        if not stat.S_ISREG(source_stat.st_mode):
            raise _error("copy_file_unavailable") from _SourceChangedError()
        resolved.append(
            _ResolvedSource(
                source=item,
                path=source_path,
                device=source_stat.st_dev,
                inode=source_stat.st_ino,
                stat_size=source_stat.st_size,
            )
        )
        total_bytes += source_stat.st_size
    return tuple(resolved), total_bytes


def _safe_directory_components(paths: DataPaths, relative_path: str) -> Path:
    parts = PurePosixPath(relative_path).parts
    if (
        not parts
        or PurePosixPath(relative_path).is_absolute()
        or ".." in parts
    ):
        raise PathBoundaryError("Invalid private staging directory.")
    current = paths.root
    root_stat = current.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise PathBoundaryError("Invalid private staging root.")
    for part in parts:
        current = current / part
        component_stat = current.lstat()
        if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(component_stat.st_mode):
            raise PathBoundaryError("Invalid private staging directory component.")
    managed = paths.managed_path(relative_path)
    if managed != current:
        raise PathBoundaryError("Private staging directory changed during resolution.")
    return managed


def _ensure_private_parent_directories(paths: DataPaths, relative_path: str) -> Path:
    parts = PurePosixPath(relative_path).parts
    if (
        not parts
        or PurePosixPath(relative_path).is_absolute()
        or ".." in parts
    ):
        raise PathBoundaryError("Invalid private staging directory.")
    current = paths.root
    root_stat = current.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise PathBoundaryError("Invalid private staging root.")
    for part in parts:
        current = current / part
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            component_stat = current.lstat()
        if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(component_stat.st_mode):
            raise PathBoundaryError("Invalid private staging directory component.")
    managed = paths.managed_path(relative_path)
    if managed != current:
        raise PathBoundaryError("Private staging directory changed during resolution.")
    return managed


def _create_attempt_directory(
    paths: DataPaths, copy_id: str
) -> tuple[Path, str, int, int]:
    relative_copy_root = PurePosixPath("artifacts", "project-copies", copy_id).as_posix()
    attempt_path: Path | None = None
    attempt_device: int | None = None
    attempt_inode: int | None = None
    try:
        copy_root = _ensure_private_parent_directories(paths, relative_copy_root)
        os.chmod(copy_root, 0o700)
        attempt_path = Path(tempfile.mkdtemp(prefix="attempt-", dir=copy_root))
        attempt_stat = attempt_path.lstat()
        attempt_device, attempt_inode = attempt_stat.st_dev, attempt_stat.st_ino
        os.chmod(attempt_path, 0o700)
        relative_attempt = paths.relative_managed_path(attempt_path)
        safe_attempt = _safe_directory_components(paths, relative_attempt)
        confirmed_stat = safe_attempt.lstat()
        if (confirmed_stat.st_dev, confirmed_stat.st_ino) != (attempt_device, attempt_inode):
            raise PathBoundaryError("Private staging directory identity changed.")
        return safe_attempt, relative_attempt, attempt_device, attempt_inode
    except (OSError, PathBoundaryError, RuntimeError, ValueError) as error:
        if attempt_path is not None and attempt_device is not None and attempt_inode is not None:
            _remove_attempt_directory(
                paths,
                attempt_path,
                attempt_device,
                attempt_inode,
                suppress_errors=True,
            )
        raise _error("copy_staging_failed") from error


def _attempt_directory_if_safe(paths: DataPaths, relative_path: str) -> Path | None:
    parts = PurePosixPath(relative_path).parts
    if (
        len(parts) != 4
        or parts[:2] != ("artifacts", "project-copies")
        or PurePosixPath(relative_path).is_absolute()
        or ".." in parts
    ):
        return None
    current = paths.root
    try:
        root_stat = current.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return None
        for index, part in enumerate(parts):
            current = current / part
            component_stat = current.lstat()
            if stat.S_ISLNK(component_stat.st_mode):
                return None
            if index < len(parts) - 1 and not stat.S_ISDIR(component_stat.st_mode):
                return None
        final_stat = current.lstat()
        if not stat.S_ISDIR(final_stat.st_mode):
            return None
        managed = paths.managed_path(relative_path)
        return managed if managed == current else None
    except (FileNotFoundError, NotADirectoryError, OSError, PathBoundaryError, RuntimeError, ValueError):
        return None


def _remove_attempt_directory(
    paths: DataPaths,
    path: Path,
    device: int,
    inode: int,
    *,
    suppress_errors: bool,
) -> None:
    try:
        relative_path = path.relative_to(paths.root).as_posix()
        safe_path = _attempt_directory_if_safe(paths, relative_path)
        if safe_path is None:
            return
        current_stat = safe_path.lstat()
        if (current_stat.st_dev, current_stat.st_ino) != (device, inode):
            return
        shutil.rmtree(safe_path)
    except (OSError, PathBoundaryError, RuntimeError, ValueError) as error:
        if not suppress_errors:
            raise _error("copy_staging_failed") from error


def _open_source(source: _ResolvedSource) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source.path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or (opened_stat.st_dev, opened_stat.st_ino) != (source.device, source.inode)
        ):
            raise _SourceChangedError()
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def stage_project_files(
    paths: DataPaths,
    snapshot: ProjectCopySnapshot,
    *,
    copy_id: str,
    artifact_ids: Mapping[str, str],
    check: Callable[[], None],
    progress: Callable[[int, int], None] | None = None,
) -> StagedProjectFiles:
    """Copy verified source bytes into one exclusive private attempt directory."""
    if type(paths) is not DataPaths:
        raise TypeError("A DataPaths instance is required.")
    if not callable(check) or (progress is not None and not callable(progress)):
        raise TypeError("Copy staging callbacks must be callable.")

    inputs = _validate_snapshot(snapshot, copy_id=copy_id, artifact_ids=artifact_ids)
    check()
    resolved_sources, total_bytes = _resolve_sources(paths, inputs, check)
    check()
    attempt_path, directory_relative_path, directory_device, directory_inode = (
        _create_attempt_directory(paths, copy_id)
    )
    staged_files: list[StagedCopyFile] = []
    try:
        if progress is not None:
            progress(0, total_bytes)
        copied_bytes = 0
        for source in resolved_sources:
            check()
            output_path = attempt_path / (
                source.source.target_artifact_id + source.source.target_suffix
            )
            try:
                input_stream = _open_source(source)
            except _SourceChangedError as error:
                raise _error("copy_file_unavailable") from error
            except OSError as error:
                raise _error("copy_file_unavailable") from error

            try:
                output_descriptor = os.open(
                    output_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except OSError as error:
                try:
                    input_stream.close()
                except OSError:
                    pass
                raise _error("copy_staging_failed") from error

            try:
                output_stream = os.fdopen(output_descriptor, "wb")
            except OSError as error:
                os.close(output_descriptor)
                try:
                    input_stream.close()
                except OSError:
                    pass
                raise _error("copy_staging_failed") from error

            digest = hashlib.sha256()
            observed_size = 0
            pending_error: BaseException | None = None
            try:
                while True:
                    check()
                    try:
                        chunk = input_stream.read(_CHUNK_BYTES)
                    except OSError as error:
                        raise _error("copy_file_unavailable") from error
                    if not chunk:
                        break
                    digest.update(chunk)
                    try:
                        written = output_stream.write(chunk)
                    except OSError as error:
                        raise _error("copy_staging_failed") from error
                    if written != len(chunk):
                        raise _error("copy_staging_failed") from OSError(
                            "A staged file write was incomplete."
                        )
                    observed_size += len(chunk)
                    copied_bytes += len(chunk)
                    if progress is not None:
                        progress(copied_bytes, total_bytes)
                if (
                    source.source.size_bytes is not None
                    and observed_size != source.source.size_bytes
                ):
                    raise _error("copy_file_unavailable") from _SourceChangedError()
                observed_hash = digest.hexdigest()
                if observed_hash != source.source.content_hash.casefold():
                    raise _error("copy_file_unavailable") from _SourceChangedError()
                try:
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                except OSError as error:
                    raise _error("copy_staging_failed") from error
            except BaseException as error:
                pending_error = error
                raise
            finally:
                close_failure: tuple[str, BaseException] | None = None
                try:
                    output_stream.close()
                except BaseException as error:
                    if pending_error is None:
                        close_failure = ("copy_staging_failed", error)
                try:
                    input_stream.close()
                except BaseException as error:
                    if pending_error is None and close_failure is None:
                        close_failure = ("copy_file_unavailable", error)
                if pending_error is None and close_failure is not None:
                    code, error = close_failure
                    if isinstance(error, OSError):
                        raise _error(code) from error
                    raise error

            try:
                relative_path = paths.relative_managed_path(output_path)
            except (OSError, PathBoundaryError, RuntimeError, ValueError) as error:
                raise _error("copy_staging_failed") from error
            staged_files.append(
                StagedCopyFile(
                    source_artifact_id=source.source.source.artifact_id,
                    artifact_id=source.source.target_artifact_id,
                    relative_path=relative_path,
                    content_hash=observed_hash,
                    size_bytes=observed_size,
                )
            )

        if os.name != "nt":
            try:
                directory_descriptor = os.open(
                    attempt_path,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError as error:
                raise _error("copy_staging_failed") from error

        check()
        return StagedProjectFiles(
            snapshot_fingerprint=snapshot.fingerprint,
            directory_relative_path=directory_relative_path,
            directory_device=directory_device,
            directory_inode=directory_inode,
            files=tuple(staged_files),
        )
    except BaseException:
        _remove_attempt_directory(
            paths,
            attempt_path,
            directory_device,
            directory_inode,
            suppress_errors=True,
        )
        raise


def cleanup_staged_project_files(paths: DataPaths, staged: StagedProjectFiles) -> None:
    """Remove only the private attempt whose recorded directory identity matches."""
    if type(paths) is not DataPaths or type(staged) is not StagedProjectFiles:
        raise TypeError("Staged project files and DataPaths are required.")
    if (
        type(staged.directory_device) is not int
        or type(staged.directory_inode) is not int
        or staged.directory_device < 0
        or staged.directory_inode < 0
    ):
        raise TypeError("The staged directory identity is invalid.")
    path = _attempt_directory_if_safe(paths, staged.directory_relative_path)
    if path is None:
        return
    current_stat = path.lstat()
    if (current_stat.st_dev, current_stat.st_ino) != (
        staged.directory_device,
        staged.directory_inode,
    ):
        return
    _remove_attempt_directory(
        paths,
        path,
        staged.directory_device,
        staged.directory_inode,
        suppress_errors=False,
    )


__all__ = [
    "StagedCopyFile",
    "StagedProjectFiles",
    "cleanup_staged_project_files",
    "stage_project_files",
]
