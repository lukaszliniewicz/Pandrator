"""Conservative cleanup of installer runtime metadata."""

import logging
import os
from pathlib import Path

import psutil

from .process_identity import (
    ProcessIdentityError,
    ProcessIdentityMismatch,
    ProcessInspectionError,
    identity_from_mapping,
    validated_process,
)
from .runtime_metadata_files import (
    RuntimeMetadataSnapshot,
    discard_runtime_metadata,
    read_runtime_metadata,
)


def _metadata_record_is_live(
    record: object,
    *,
    pid_key: str = "pid",
    create_time_key: str = "process_create_time",
    executable_key: str = "executable",
    require_instance_id: bool = True,
) -> bool:
    try:
        raw_pid = int(record.get(pid_key) or 0) if isinstance(record, dict) else 0
    except (OverflowError, TypeError, ValueError):
        raw_pid = 0
    if not isinstance(record, dict):
        return False
    try:
        identity = identity_from_mapping(
            record,
            pid_key=pid_key,
            create_time_key=create_time_key,
            executable_key=executable_key,
            require_instance_id=require_instance_id,
        )
    except ProcessIdentityError:
        # Legacy or partially-written metadata cannot authorize a stop,
        # but a live PID is enough reason not to delete evidence that an
        # active supervisor may shortly replace atomically.
        return raw_pid > 0 and psutil.pid_exists(raw_pid)
    try:
        return validated_process(identity) is not None
    except ProcessIdentityMismatch:
        # A complete record whose PID now belongs to another process is
        # stale and may be removed without touching that process.
        return False
    except ProcessInspectionError:
        # AccessDenied and transient inspection failures are not proof
        # that the owner is stale.
        return True


def _remove_file(snapshot: RuntimeMetadataSnapshot | None) -> None:
    if snapshot is None:
        return
    try:
        discard_runtime_metadata(snapshot)
    except OSError as error:
        logging.warning("Could not remove stale runtime metadata %s: %s", snapshot.path, error)


def remove_stale_runtime_metadata(pandrator_path: str | os.PathLike[str]) -> None:
    runtime_state = os.path.join(pandrator_path, "runtime-processes.json")
    runtime_snapshot = read_runtime_metadata(runtime_state)
    payload = runtime_snapshot.payload if runtime_snapshot is not None else None

    runtime_is_live = False
    if isinstance(payload, dict):
        runtime_is_live = _metadata_record_is_live(
            payload,
            pid_key="supervisor_pid",
            create_time_key="supervisor_create_time",
            executable_key="supervisor_executable",
        )
        raw_processes = payload.get("processes")
        if isinstance(raw_processes, dict):
            runtime_is_live = runtime_is_live or any(
                _metadata_record_is_live(process)
                for process in raw_processes.values()
                if isinstance(process, dict)
            )
    if not runtime_is_live:
        _remove_file(runtime_snapshot)

    lock_path = os.path.join(pandrator_path, "pandrator.instance.lock")
    lock_snapshot = read_runtime_metadata(lock_path)
    lock_payload = lock_snapshot.payload if lock_snapshot is not None else None
    if not _metadata_record_is_live(lock_payload):
        _remove_file(lock_snapshot)


def uninstall_runtime_may_be_active(root: str | os.PathLike[str]) -> bool:
    """Read one-time uninstall evidence without preventing a concurrent launch."""

    def record_may_be_active(
        record: object,
        *,
        pid_key: str = "pid",
        create_time_key: str = "process_create_time",
        executable_key: str = "executable",
    ) -> bool:
        if not isinstance(record, dict):
            return True
        try:
            raw_pid = int(record.get(pid_key) or 0)
        except (OverflowError, TypeError, ValueError):
            return True
        if raw_pid <= 0:
            return True
        return _metadata_record_is_live(
            record,
            pid_key=pid_key,
            create_time_key=create_time_key,
            executable_key=executable_key,
            require_instance_id=True,
        )

    metadata_root = Path(root)
    for name in ("runtime-processes.json", "pandrator.instance.lock"):
        path = metadata_root / name
        snapshot = read_runtime_metadata(path)
        if snapshot is None:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return True
            return True
        payload = snapshot.payload
        if snapshot.parse_error is not None or not isinstance(payload, dict):
            return True
        if name == "runtime-processes.json":
            if record_may_be_active(
                payload,
                pid_key="supervisor_pid",
                create_time_key="supervisor_create_time",
                executable_key="supervisor_executable",
            ):
                return True
            processes = payload.get("processes", {})
            if not isinstance(processes, dict):
                return True
            for record in processes.values():
                if record_may_be_active(record):
                    return True
        elif record_may_be_active(payload):
            return True
    return False
