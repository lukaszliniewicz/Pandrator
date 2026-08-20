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
    ownership_token: str | None = None,
) -> ProcessIdentity:
    process_group_id: int | None = None
    session_id: int | None = None
    if os.name != "nt":
        try:
            process_group_id = os.getpgid(process.pid)
            session_id = os.getsid(process.pid)
        except OSError:
            # Identity validation remains PID/create-time/executable based;
            # group metadata is only a helpful diagnostic and must not make a
            # successful launch fail on a short-lived wrapper.
            pass
    return ProcessIdentity(
        pid=process.pid,
        create_time=float(process.create_time()),
        executable=str(process.exe()),
        manager_instance_id=manager_instance_id,
        ownership_token=ownership_token,
        process_group_id=process_group_id,
        session_id=session_id,
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
