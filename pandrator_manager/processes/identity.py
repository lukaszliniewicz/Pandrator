"""Durable PID identity validation for manager-owned processes."""

from __future__ import annotations

import os

import psutil

from ..models import ProcessIdentity


class IdentityMismatch(RuntimeError):
    pass


class IdentityInspectionFailed(RuntimeError):
    pass


def normalized_executable(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(
        os.path.realpath(os.path.abspath(os.fspath(value)))
    )


def capture_identity(
    process: psutil.Process,
    *,
    manager_instance_id: str,
) -> ProcessIdentity:
    return ProcessIdentity(
        pid=process.pid,
        create_time=float(process.create_time()),
        executable=str(process.exe()),
        manager_instance_id=manager_instance_id,
    )


def validate_identity(identity: ProcessIdentity) -> psutil.Process | None:
    try:
        process = psutil.Process(identity.pid)
        create_time = float(process.create_time())
        executable = str(process.exe())
    except psutil.NoSuchProcess:
        return None
    except (psutil.AccessDenied, OSError) as error:
        raise IdentityInspectionFailed(
            f"Could not inspect process PID {identity.pid}."
        ) from error
    if (
        abs(create_time - identity.create_time) > 0.01
        or normalized_executable(executable)
        != normalized_executable(identity.executable)
    ):
        raise IdentityMismatch(
            f"PID {identity.pid} no longer identifies the recorded process."
        )
    return process
