"""Durable process identity helpers used by installer lifecycle operations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

import psutil


PROCESS_CREATE_TIME_TOLERANCE_SECONDS = 0.01


class ProcessIdentityError(RuntimeError):
    """Raised when durable metadata does not identify the current process."""


class ProcessInspectionError(ProcessIdentityError):
    """Raised when the current process cannot be inspected safely."""


class ProcessIdentityMismatch(ProcessIdentityError):
    """Raised when a PID has been reused by a different process."""


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    create_time: float
    executable: str
    instance_id: str = ""


def normalized_executable(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def identity_from_mapping(
    payload: Mapping[str, Any],
    *,
    pid_key: str = "pid",
    create_time_key: str = "process_create_time",
    executable_key: str = "executable",
    instance_id_key: str | None = "instance_id",
    require_instance_id: bool = False,
) -> ProcessIdentity:
    if not isinstance(payload, Mapping):
        raise ProcessIdentityError("Process identity must be a JSON object.")
    try:
        pid = int(payload.get(pid_key) or 0)
        create_time = float(payload.get(create_time_key))
    except (TypeError, ValueError):
        raise ProcessIdentityError("Process identity has an invalid PID or creation time.") from None

    executable = str(payload.get(executable_key) or "").strip()
    instance_id = (
        str(payload.get(instance_id_key) or "").strip()
        if instance_id_key is not None
        else ""
    )
    if pid <= 0 or not executable or (require_instance_id and not instance_id):
        raise ProcessIdentityError("Process identity is incomplete.")
    return ProcessIdentity(
        pid=pid,
        create_time=create_time,
        executable=executable,
        instance_id=instance_id,
    )


def capture_process_identity(
    process: psutil.Process,
    *,
    instance_id: str = "",
) -> ProcessIdentity:
    return ProcessIdentity(
        pid=int(process.pid),
        create_time=float(process.create_time()),
        executable=str(process.exe()),
        instance_id=str(instance_id or ""),
    )


def validated_process(identity: ProcessIdentity) -> psutil.Process | None:
    """Return the live process only if PID, creation time, and executable match."""
    try:
        process = psutil.Process(identity.pid)
        actual_create_time = float(process.create_time())
        actual_executable = str(process.exe())
    except psutil.NoSuchProcess:
        return None
    except (psutil.AccessDenied, OSError) as error:
        raise ProcessInspectionError(
            f"Could not inspect process PID {identity.pid}."
        ) from error

    if (
        abs(actual_create_time - identity.create_time)
        > PROCESS_CREATE_TIME_TOLERANCE_SECONDS
        or normalized_executable(actual_executable)
        != normalized_executable(identity.executable)
    ):
        raise ProcessIdentityMismatch(
            f"PID {identity.pid} belongs to a different process."
        )
    return process


def identity_payload(identity: ProcessIdentity) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pid": identity.pid,
        "process_create_time": identity.create_time,
        "executable": identity.executable,
    }
    if identity.instance_id:
        payload["instance_id"] = identity.instance_id
    return payload
