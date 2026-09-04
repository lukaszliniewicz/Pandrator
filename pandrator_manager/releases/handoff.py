"""Authenticated, external manager self-update handoff.

The running daemon may stage a new manager but never changes its own active
environment.  This module is launched as a detached helper, waits for the old
daemon to exit, probes the new private runtime against a database copy, swaps
the pointer, starts the new daemon, and restores the previous manager on any
failure.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import psutil
import requests
from pydantic import Field

from .. import __version__
from ..auth import protect_path, read_client_secret
from ..context import WorkspaceLayout
from ..errors import ConflictError, ManagerError, UnsafePathError
from ..launcher import (
    LauncherRuntime,
    current_runtime_executable,
    installed_launcher,
    runtime_command,
)
from ..models import (
    ConnectionDescriptor,
    OperationKind,
    OperationState,
    StrictModel,
    TaskState,
)
from ..state import ManagerStore
from .authority import VerifiedRelease
from .bundles import validate_release_bundle
from .slots import (
    _atomic_json,
    _durable_replace,
    _restore_sqlite,
    _snapshot_sqlite,
)
from .trust import canonical_json

_OPERATION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
_OPERATION_ID = re.compile(_OPERATION_ID_PATTERN)


class ManagerHandoffPayload(StrictModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(pattern=_OPERATION_ID_PATTERN)
    workspace: str
    expected_revision: int = Field(ge=0)
    channel: Literal["stable", "beta", "nightly"]
    version: str
    sequence: int = Field(ge=1)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    envelope: dict[str, Any]
    artifact: dict[str, Any]
    verified_key_ids: tuple[str, ...]
    slot_path: str
    new_python: str
    new_runtime_mode: Literal["native_launcher", "python"]
    new_application_root: str
    old_python: str
    old_runtime_mode: Literal["native_launcher", "python"]
    old_cwd: str
    old_pid: int = Field(gt=0)
    old_create_time: float = Field(gt=0)
    previous_pointer: dict[str, Any] | None = None
    created_at: datetime


class ManagerHandoffEnvelope(StrictModel):
    payload: ManagerHandoffPayload
    authentication: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManagerPreparationPayload(StrictModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(pattern=_OPERATION_ID_PATTERN)
    workspace: str
    expected_revision: int = Field(ge=0)
    product: Literal["pandrator-manager"]
    channel: Literal["stable", "beta", "nightly"]
    version: str
    sequence: int = Field(ge=1)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    envelope: dict[str, Any]
    artifact: dict[str, Any]
    verified_key_ids: tuple[str, ...]
    staged_path: str
    destination_path: str
    created_at: datetime


class ManagerPreparationEnvelope(StrictModel):
    payload: ManagerPreparationPayload
    authentication: str = Field(pattern=r"^[0-9a-f]{64}$")


def handoff_directory(layout: WorkspaceLayout) -> Path:
    return layout.state / "handoffs"


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction is not None and junction())


def _safe_handoff_directory(
    layout: WorkspaceLayout,
    *,
    create: bool = False,
) -> Path:
    directory = handoff_directory(layout)
    if directory.parent != layout.state:
        raise ManagerError(
            "unsafe_manager_handoff",
            "The manager handoff directory is outside manager state.",
            {"path": str(directory)},
            500,
        )
    if os.path.lexists(directory):
        if _is_link_or_junction(directory) or not directory.is_dir():
            raise ManagerError(
                "unsafe_manager_handoff",
                "The manager handoff path is not a real directory.",
                {"path": str(directory)},
                409,
            )
        if directory.resolve(strict=True) != directory:
            raise ManagerError(
                "unsafe_manager_handoff",
                "The manager handoff directory resolves unexpectedly.",
                {"path": str(directory)},
                409,
            )
    elif create:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            return _safe_handoff_directory(layout)
    if create:
        protect_path(directory, directory=True)
    return directory


def _require_regular_control_file(path: Path, *, label: str) -> None:
    if os.path.lexists(path) and (_is_link_or_junction(path) or not path.is_file()):
        raise ManagerError(
            "unsafe_manager_handoff",
            f"The {label} is not a regular file.",
            {"path": str(path)},
            409,
        )


def _validated_operation_id(operation_id: str) -> str:
    selected = str(operation_id)
    if not _OPERATION_ID.fullmatch(selected) or selected in {".", ".."}:
        raise ManagerError(
            "invalid_manager_handoff",
            "The manager handoff operation identifier is not filesystem-safe.",
            {"operation_id": selected[:120]},
            400,
        )
    return selected


def handoff_descriptor_path(
    layout: WorkspaceLayout,
    operation_id: str,
) -> Path:
    selected = _validated_operation_id(operation_id)
    path = _safe_handoff_directory(layout) / f"{selected}.json"
    return layout.require_within(path, roots=(layout.state,))


def preparation_journal_path(
    layout: WorkspaceLayout,
    operation_id: str,
) -> Path:
    selected = _validated_operation_id(operation_id)
    path = _safe_handoff_directory(layout) / f"{selected}.prepare"
    return layout.require_within(path, roots=(layout.state,))


def _authentication(payload: Mapping[str, Any], secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        canonical_json(payload),
        hashlib.sha256,
    ).hexdigest()


def _write_envelope(
    path: Path,
    payload: ManagerHandoffPayload,
    secret: str,
) -> None:
    raw_payload = payload.model_dump(mode="json")
    _atomic_json(
        path,
        {
            "payload": raw_payload,
            "authentication": _authentication(raw_payload, secret),
        },
    )
    protect_path(path.parent, directory=True)
    protect_path(path)


def _write_preparation_journal(
    path: Path,
    payload: ManagerPreparationPayload,
    secret: str,
) -> None:
    raw_payload = payload.model_dump(mode="json")
    _atomic_json(
        path,
        {
            "payload": raw_payload,
            "authentication": _authentication(raw_payload, secret),
        },
    )
    protect_path(path.parent, directory=True)
    protect_path(path)


def read_handoff(
    layout: WorkspaceLayout,
    operation_id: str,
) -> tuple[ManagerHandoffEnvelope, Path]:
    path = handoff_descriptor_path(layout, operation_id)
    _require_regular_control_file(
        path,
        label="manager handoff descriptor",
    )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        envelope = ManagerHandoffEnvelope.model_validate(raw)
    except Exception as error:
        raise ManagerError(
            "invalid_manager_handoff",
            "The pending manager handoff descriptor is invalid.",
            {"operation_id": operation_id, "reason": str(error)},
            500,
        ) from error
    if (
        envelope.payload.operation_id != operation_id
        or Path(envelope.payload.workspace).resolve(strict=False) != layout.workspace
    ):
        raise ManagerError(
            "invalid_manager_handoff",
            "The pending manager handoff belongs to another operation or workspace.",
            {"operation_id": operation_id},
            500,
        )
    secret = read_client_secret(layout.credential)
    expected = _authentication(
        envelope.payload.model_dump(mode="json"),
        secret,
    )
    if not hmac.compare_digest(expected, envelope.authentication):
        raise ManagerError(
            "invalid_manager_handoff",
            "The pending manager handoff failed authentication.",
            {"operation_id": operation_id},
            500,
        )
    return envelope, path


def read_preparation_journal(
    layout: WorkspaceLayout,
    operation_id: str,
) -> tuple[ManagerPreparationEnvelope, Path]:
    path = preparation_journal_path(layout, operation_id)
    _require_regular_control_file(
        path,
        label="manager handoff preparation journal",
    )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        envelope = ManagerPreparationEnvelope.model_validate(raw)
    except Exception as error:
        raise ManagerError(
            "invalid_manager_handoff",
            "The manager handoff preparation journal is invalid.",
            {"operation_id": operation_id, "reason": str(error)},
            500,
        ) from error
    if (
        envelope.payload.operation_id != operation_id
        or Path(envelope.payload.workspace).resolve(strict=False) != layout.workspace
    ):
        raise ManagerError(
            "invalid_manager_handoff",
            "The manager handoff preparation journal belongs to another operation or workspace.",
            {"operation_id": operation_id},
            500,
        )
    try:
        layout.require_within(
            envelope.payload.staged_path,
            roots=(layout.staging,),
        )
        layout.require_within(
            envelope.payload.destination_path,
            roots=(layout.manager_versions,),
        )
    except UnsafePathError as error:
        raise ManagerError(
            "invalid_manager_handoff",
            "The manager handoff preparation journal has unsafe paths.",
            {"operation_id": operation_id},
            500,
        ) from error
    secret = read_client_secret(layout.credential)
    expected = _authentication(
        envelope.payload.model_dump(mode="json"),
        secret,
    )
    if not hmac.compare_digest(expected, envelope.authentication):
        raise ManagerError(
            "invalid_manager_handoff",
            "The manager handoff preparation journal failed authentication.",
            {"operation_id": operation_id},
            500,
        )
    return envelope, path


def _validate_reviewed_manager_preparation(
    *,
    layout: WorkspaceLayout,
    store: ManagerStore,
    operation_id: str,
    expected_revision: int,
    release: VerifiedRelease,
    staged: Path,
    destination: Path,
    preparation: ManagerPreparationPayload | None = None,
) -> None:
    if release.manifest.payload.product != "pandrator-manager":
        raise ValueError("Manager handoff requires a manager release.")
    operation = store.get_operation(operation_id)
    plan = store.get_plan(operation.plan_id)
    release_impact = plan.impacts.get("release")
    artifact = release.artifact.model_dump(mode="json")
    if (
        operation.kind != OperationKind.UPDATE
        or operation.state != OperationState.RUNNING
        or plan.kind != OperationKind.UPDATE
        or plan.workspace != str(layout.workspace)
        or plan.expected_revision != expected_revision
        or not isinstance(release_impact, dict)
        or release_impact.get("product") != "pandrator-manager"
        or release_impact.get("channel") != release.manifest.payload.channel
        or release_impact.get("version") != release.manifest.payload.version
        or release_impact.get("sequence") != release.manifest.payload.sequence
        or release_impact.get("manifest_digest") != release.manifest.digest
        or release_impact.get("artifact") != artifact
    ):
        raise ManagerError(
            "invalid_manager_handoff",
            "The pending handoff does not match its reviewed release plan.",
            {"operation_id": operation_id},
            500,
        )
    if preparation is not None and (
        preparation.operation_id != operation_id
        or Path(preparation.workspace).resolve(strict=False) != layout.workspace
        or preparation.expected_revision != expected_revision
        or preparation.product != "pandrator-manager"
        or preparation.channel != release.manifest.payload.channel
        or preparation.version != release.manifest.payload.version
        or preparation.sequence != release.manifest.payload.sequence
        or preparation.manifest_digest != release.manifest.digest
        or preparation.envelope != release.envelope
        or preparation.artifact != artifact
        or preparation.verified_key_ids != release.manifest.verified_key_ids
        or layout.require_within(
            preparation.staged_path,
            roots=(layout.staging,),
        ).resolve(strict=False)
        != staged.resolve(strict=False)
        or layout.require_within(
            preparation.destination_path,
            roots=(layout.manager_versions,),
        ).resolve(strict=False)
        != destination.resolve(strict=False)
    ):
        raise ManagerError(
            "invalid_manager_handoff",
            "The manager handoff preparation journal does not match the reviewed release.",
            {"operation_id": operation_id},
            500,
        )


def _move_prepared_manager_release(staged: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _durable_replace(staged, destination)


def _handoff_payload(
    *,
    layout: WorkspaceLayout,
    operation_id: str,
    expected_revision: int,
    release: VerifiedRelease,
    validated,
) -> ManagerHandoffPayload:
    pointer = layout.root / "manager" / "current.json"
    try:
        previous = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        previous = None
    current = psutil.Process()
    return ManagerHandoffPayload(
        operation_id=operation_id,
        workspace=str(layout.workspace),
        expected_revision=expected_revision,
        channel=release.manifest.payload.channel,
        version=release.manifest.payload.version,
        sequence=release.manifest.payload.sequence,
        manifest_digest=release.manifest.digest,
        envelope=release.envelope,
        artifact=release.artifact.model_dump(mode="json"),
        verified_key_ids=release.manifest.verified_key_ids,
        slot_path=str(validated.root),
        new_python=str(validated.python),
        new_runtime_mode=validated.metadata.runtime_kind,
        new_application_root=str(validated.application_root),
        old_python=str(current_runtime_executable()),
        old_runtime_mode=("native_launcher" if bool(getattr(sys, "frozen", False)) else "python"),
        old_cwd=str(Path.cwd().resolve(strict=False)),
        old_pid=current.pid,
        old_create_time=current.create_time(),
        previous_pointer=previous if isinstance(previous, dict) else None,
        created_at=datetime.now(timezone.utc),
    )


def _remove_preparation_journal(path: Path) -> None:
    _require_regular_control_file(path, label="manager handoff preparation journal")
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _cleanup_preparation_journal(path: Path) -> None:
    try:
        _remove_preparation_journal(path)
    except OSError:
        # The final handoff descriptor is already durable and authenticated.
        # Retain redundant cleanup state rather than rolling back that handoff.
        pass


def prepare_manager_handoff(
    *,
    layout: WorkspaceLayout,
    store: ManagerStore,
    operation_id: str,
    expected_revision: int,
    release: VerifiedRelease,
    staged_directory: Path,
) -> dict[str, Any]:
    if release.manifest.payload.product != "pandrator-manager":
        raise ValueError("Manager handoff requires a manager release.")
    _safe_handoff_directory(layout, create=True)
    descriptor = handoff_descriptor_path(layout, operation_id)
    journal = preparation_journal_path(layout, operation_id)
    staged = layout.require_within(staged_directory, roots=(layout.staging,))
    destination = layout.require_within(
        layout.manager_versions / release.manifest.payload.version,
        roots=(layout.manager_versions,),
    )
    secret = read_client_secret(layout.credential)
    if descriptor.is_file():
        envelope, _ = read_handoff(layout, operation_id)
        payload = envelope.payload
        if (
            payload.version != release.manifest.payload.version
            or payload.manifest_digest != release.manifest.digest
            or Path(payload.slot_path).resolve(strict=False) != destination
        ):
            raise ManagerError(
                "invalid_manager_handoff",
                "The existing handoff does not match the signed release.",
                {"operation_id": operation_id},
                500,
            )
        if os.path.lexists(journal):
            preparation, journal_path = read_preparation_journal(layout, operation_id)
            _validate_reviewed_manager_preparation(
                layout=layout,
                store=store,
                operation_id=operation_id,
                expected_revision=expected_revision,
                release=release,
                staged=staged,
                destination=destination,
                preparation=preparation.payload,
            )
            _cleanup_preparation_journal(journal_path)
    else:
        preparation: ManagerPreparationPayload
        if os.path.lexists(journal):
            preparation, _ = read_preparation_journal(layout, operation_id)
            _validate_reviewed_manager_preparation(
                layout=layout,
                store=store,
                operation_id=operation_id,
                expected_revision=expected_revision,
                release=release,
                staged=staged,
                destination=destination,
                preparation=preparation.payload,
            )
            if staged.exists() and destination.exists():
                raise ManagerError(
                    "invalid_manager_handoff",
                    "The manager handoff preparation has both staging and destination slots.",
                    {"operation_id": operation_id},
                    409,
                )
            if staged.exists():
                if not staged.is_dir():
                    raise ManagerError(
                        "invalid_release_bundle",
                        "The staged manager bundle is missing.",
                        http_status=409,
                    )
                _move_prepared_manager_release(staged, destination)
            elif not destination.exists():
                raise ManagerError(
                    "invalid_manager_handoff",
                    "The manager handoff preparation has neither staging nor destination slot.",
                    {"operation_id": operation_id},
                    409,
                )
        else:
            _validate_reviewed_manager_preparation(
                layout=layout,
                store=store,
                operation_id=operation_id,
                expected_revision=expected_revision,
                release=release,
                staged=staged,
                destination=destination,
            )
            if destination.exists():
                raise ConflictError(
                    "The manager release version already has a slot.",
                    {"version": release.manifest.payload.version},
                )
            if not staged.is_dir():
                raise ManagerError(
                    "invalid_release_bundle",
                    "The staged manager bundle is missing.",
                    http_status=409,
                )
            preparation = ManagerPreparationPayload(
                operation_id=operation_id,
                workspace=str(layout.workspace),
                expected_revision=expected_revision,
                product="pandrator-manager",
                channel=release.manifest.payload.channel,
                version=release.manifest.payload.version,
                sequence=release.manifest.payload.sequence,
                manifest_digest=release.manifest.digest,
                envelope=release.envelope,
                artifact=release.artifact.model_dump(mode="json"),
                verified_key_ids=release.manifest.verified_key_ids,
                staged_path=str(staged),
                destination_path=str(destination),
                created_at=datetime.now(timezone.utc),
            )
            _write_preparation_journal(journal, preparation, secret)
            _move_prepared_manager_release(staged, destination)
        validated = validate_release_bundle(
            destination,
            product="pandrator-manager",
            version=release.manifest.payload.version,
        )
        payload = _handoff_payload(
            layout=layout,
            operation_id=operation_id,
            expected_revision=expected_revision,
            release=release,
            validated=validated,
        )
        _write_envelope(descriptor, payload, secret)
        _cleanup_preparation_journal(journal)
    # Revalidate all persisted paths on resume.
    slot = layout.require_within(
        payload.slot_path,
        roots=(layout.manager_versions,),
    )
    validated = validate_release_bundle(
        slot,
        product="pandrator-manager",
        version=payload.version,
    )
    if (
        validated.python.resolve(strict=False) != Path(payload.new_python).resolve(strict=False)
        or validated.metadata.runtime_kind != payload.new_runtime_mode
        or validated.application_root.resolve(strict=False)
        != Path(payload.new_application_root).resolve(strict=False)
    ):
        raise ManagerError(
            "invalid_manager_handoff",
            "The manager slot no longer matches the authenticated handoff.",
            {"operation_id": operation_id},
            500,
        )
    operation = store.get_operation(operation_id)
    plan = store.get_plan(operation.plan_id)
    release_impact = plan.impacts.get("release")
    if (
        operation.kind != OperationKind.UPDATE
        or operation.state != OperationState.RUNNING
        or plan.kind != OperationKind.UPDATE
        or plan.workspace != str(layout.workspace)
        or plan.expected_revision != expected_revision
        or payload.expected_revision != expected_revision
        or not isinstance(release_impact, dict)
        or release_impact.get("product") != "pandrator-manager"
        or release_impact.get("channel") != payload.channel
        or release_impact.get("version") != payload.version
        or release_impact.get("sequence") != payload.sequence
        or release_impact.get("manifest_digest") != payload.manifest_digest
        or release_impact.get("artifact") != payload.artifact
    ):
        raise ManagerError(
            "invalid_manager_handoff",
            "The pending handoff does not match its reviewed release plan.",
            {"operation_id": operation_id},
            500,
        )
    release_activation = {
        "product": "pandrator-manager",
        "channel": payload.channel,
        "version": payload.version,
        "sequence": payload.sequence,
        "manifest_digest": payload.manifest_digest,
        "slot_path": str(validated.root),
        "envelope": payload.envelope,
        "artifact": payload.artifact,
        "verified_key_ids": list(payload.verified_key_ids),
    }
    return {
        "manager_handoff_pending": True,
        "handoff_descriptor": str(descriptor),
        "version": payload.version,
        "slot_path": str(validated.root),
        "release_activation": release_activation,
        "ownership": {
            "path": str(layout.root / "manager"),
            "owner_kind": "release",
            "owner_id": "pandrator-manager",
            "evidence": {
                "operation_id": operation_id,
                "version": payload.version,
                "sequence": payload.sequence,
                "manifest_digest": payload.manifest_digest,
            },
        },
    }


def rollback_prepared_manager_handoff(
    *,
    layout: WorkspaceLayout,
    operation_id: str,
    result: Mapping[str, Any] | None = None,
) -> None:
    descriptor = handoff_descriptor_path(layout, operation_id)
    journal = preparation_journal_path(layout, operation_id)
    slots: list[Path] = []
    slot_value = (result or {}).get("slot_path")
    if isinstance(slot_value, str):
        slots.append(
            layout.require_within(
                slot_value,
                roots=(layout.manager_versions,),
            )
        )
    if os.path.lexists(descriptor):
        envelope, _ = read_handoff(layout, operation_id)
        slots.append(
            layout.require_within(
                envelope.payload.slot_path,
                roots=(layout.manager_versions,),
            )
        )
    journal_path: Path | None = None
    if os.path.lexists(journal):
        preparation, journal_path = read_preparation_journal(
            layout,
            operation_id,
        )
        slots.append(
            layout.require_within(
                preparation.payload.destination_path,
                roots=(layout.manager_versions,),
            )
        )
    if slots:
        slot = slots[0]
        if any(
            candidate.resolve(strict=False) != slot.resolve(strict=False) for candidate in slots[1:]
        ):
            raise ManagerError(
                "invalid_manager_handoff",
                "Manager handoff rollback state does not agree on the prepared slot.",
                {"operation_id": operation_id},
                500,
            )
        if slot.exists():
            shutil.rmtree(slot)
    if os.path.lexists(descriptor):
        _require_regular_control_file(
            descriptor,
            label="manager handoff descriptor",
        )
        descriptor.unlink()
    if journal_path is not None:
        _remove_preparation_journal(journal_path)


class ManagerHandoffCoordinator:
    """Launch the helper only after HANDOFF_PENDING is durable."""

    def __init__(
        self,
        layout: WorkspaceLayout,
        *,
        shutdown_callback: Callable[[], None],
    ) -> None:
        self.layout = layout
        self.shutdown_callback = shutdown_callback

    def __call__(self, _execution, result: dict) -> None:
        operation_id = str(Path(str(result["handoff_descriptor"])).stem)
        # Authentication and containment are checked before a helper process is
        # permitted to outlive the retiring daemon.
        read_handoff(self.layout, operation_id)
        log_path = self.layout.logs / "manager-handoff.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab", buffering=0)
        options = (
            {"creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            stable = installed_launcher(self.layout)
            fallback = LauncherRuntime(
                mode=("native_launcher" if bool(getattr(sys, "frozen", False)) else "python"),
                executable=current_runtime_executable(),
            )
            process = subprocess.Popen(
                runtime_command(
                    stable or fallback,
                    action="handoff",
                    workspace=self.layout.workspace,
                    operation_id=operation_id,
                ),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=False,
                cwd=str(Path.cwd()),
                **options,
            )
        finally:
            log.close()
        if process.poll() is not None:
            raise RuntimeError("Manager handoff helper exited before startup.")
        self.shutdown_callback()


def _wait_for_old_manager(payload: ManagerHandoffPayload, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(payload.old_pid)
            matches = abs(process.create_time() - payload.old_create_time) <= 0.01 and Path(
                process.exe()
            ).resolve(strict=False) == Path(payload.old_python).resolve(strict=False)
        except psutil.NoSuchProcess:
            return
        except (psutil.AccessDenied, OSError) as error:
            raise RuntimeError(
                "The retiring manager process identity could not be verified."
            ) from error
        if not matches:
            return
        time.sleep(0.1)
    raise TimeoutError("The retiring manager did not exit before handoff.")


def _process_options() -> dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        }
    return {"start_new_session": True}


def _probe_new_runtime(
    payload: ManagerHandoffPayload,
    probe_database: Path,
) -> None:
    command = (
        [
            payload.new_python,
            "probe",
            "--workspace",
            payload.workspace,
            "--probe-database",
            str(probe_database),
            "--expected-version",
            payload.version,
            "--operation-id",
            payload.operation_id,
        ]
        if payload.new_runtime_mode == "native_launcher"
        else [
            payload.new_python,
            "-m",
            "pandrator_manager.releases.handoff",
            "--probe",
            "--workspace",
            payload.workspace,
            "--probe-database",
            str(probe_database),
            "--expected-version",
            payload.version,
            "--operation-id",
            payload.operation_id,
        ]
    )
    result = subprocess.run(
        command,
        cwd=payload.new_application_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        timeout=180,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    if result.returncode != 0:
        raise RuntimeError(
            "New manager runtime probe failed: "
            + (result.stderr.strip() or result.stdout.strip())[:1000]
        )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("New manager runtime probe returned invalid JSON.") from error
    if report.get("version") != payload.version or not report.get("ok"):
        raise RuntimeError("New manager runtime probe reported another version.")


def _commit_handoff(
    store: ManagerStore,
    layout: WorkspaceLayout,
    payload: ManagerHandoffPayload,
) -> None:
    operation = store.get_operation(payload.operation_id)
    if operation.state == OperationState.SUCCEEDED:
        accepted = store.accepted_release("pandrator-manager")
        if accepted is None or accepted["manifest_digest"] != payload.manifest_digest:
            raise RuntimeError("Succeeded handoff does not match accepted release state.")
        return
    if operation.state != OperationState.HANDOFF_PENDING:
        raise RuntimeError(f"Manager handoff operation has unexpected state {operation.state}.")
    operation.state = OperationState.SUCCEEDED
    operation.progress = 1.0
    operation.current_task_id = None
    operation.finished_at = datetime.now(timezone.utc)
    operation.updated_at = operation.finished_at
    store.commit_operation_success(
        operation,
        inspections={},
        desired={},
        expected_revision=payload.expected_revision,
        claimed_owned_paths=(
            (
                layout.root / "manager",
                "release",
                "pandrator-manager",
                {
                    "operation_id": payload.operation_id,
                    "version": payload.version,
                    "sequence": payload.sequence,
                    "manifest_digest": payload.manifest_digest,
                },
            ),
        ),
        release_activation={
            "product": "pandrator-manager",
            "channel": payload.channel,
            "version": payload.version,
            "sequence": payload.sequence,
            "manifest_digest": payload.manifest_digest,
            "slot_path": payload.slot_path,
            "envelope": payload.envelope,
            "artifact": payload.artifact,
            "verified_key_ids": list(payload.verified_key_ids),
        },
    )


def _wait_for_new_manager(
    layout: WorkspaceLayout,
    payload: ManagerHandoffPayload,
    secret: str,
    process: subprocess.Popen,
    timeout: float,
) -> None:
    def loopback_base_url(value: str) -> bool:
        try:
            parsed = urlsplit(value)
            host = str(parsed.hostname or "").split("%", 1)[0]
            address = ipaddress.ip_address(host)
        except (ValueError, TypeError):
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        return bool(
            parsed.scheme == "http"
            and parsed.port is not None
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and (address.is_loopback or (mapped is not None and mapped.is_loopback))
        )

    try:
        launch_root = psutil.Process(process.pid)
        launch_create_time = launch_root.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        launch_create_time = time.time()

    def belongs_to_launch(candidate: psutil.Process) -> bool:
        try:
            if (
                candidate.pid == process.pid
                and abs(candidate.create_time() - launch_create_time) <= 0.01
            ):
                return True
            current = candidate
            for _depth in range(32):
                parent_pid = current.ppid()
                if parent_pid == process.pid:
                    return candidate.create_time() >= launch_create_time - 1.0
                if parent_pid <= 0 or parent_pid == current.pid:
                    return False
                try:
                    current = current.parent()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return False
                if current is None:
                    return False
            return False
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False

    deadline = time.monotonic() + timeout
    last_error = "not started"
    while time.monotonic() < deadline:
        try:
            descriptor = ConnectionDescriptor.model_validate_json(
                layout.descriptor.read_text(encoding="utf-8")
            )
            if descriptor.manager_version != payload.version:
                raise ValueError("descriptor reports another version")
            if Path(descriptor.workspace).resolve(strict=False) != layout.workspace:
                raise ValueError("descriptor reports another workspace")
            if not loopback_base_url(descriptor.base_url):
                raise ValueError("descriptor endpoint is not safe loopback HTTP")
            daemon_process = psutil.Process(descriptor.pid)
            if (
                not belongs_to_launch(daemon_process)
                or abs(daemon_process.create_time() - descriptor.process_create_time) > 0.01
                or Path(daemon_process.exe()).resolve(strict=False)
                != Path(descriptor.executable).resolve(strict=False)
                or Path(descriptor.executable).resolve(strict=False)
                != Path(payload.new_python).resolve(strict=False)
            ):
                raise ValueError("descriptor process identity mismatch")
            response = requests.get(
                f"{descriptor.base_url.rstrip('/')}/v1/health",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=2,
            )
            response.raise_for_status()
            health = response.json()
            if (
                health.get("service") == "pandrator-manager"
                and health.get("version") == payload.version
                and hmac.compare_digest(
                    str(health.get("instance_id") or ""),
                    descriptor.instance_id,
                )
                and hmac.compare_digest(
                    str(response.headers.get("X-Pandrator-Manager-Instance") or ""),
                    descriptor.instance_id,
                )
            ):
                return
            last_error = "health identity/version mismatch"
        except Exception as error:
            last_error = str(error)
        if process.poll() is not None:
            # PyInstaller one-file bootloaders can hand execution to a child
            # process. Give an authenticated descriptor one iteration to prove
            # that lineage before treating the bootloader exit as fatal.
            try:
                descriptor_available = layout.descriptor.is_file()
            except OSError:
                descriptor_available = False
            if not descriptor_available:
                raise RuntimeError(f"New manager exited with code {process.returncode}.")
        time.sleep(0.2)
    raise TimeoutError(f"New manager health timed out: {last_error}")


def _mark_handoff_failed(
    store: ManagerStore,
    operation_id: str,
    error: Exception,
) -> None:
    operation = store.get_operation(operation_id)
    operation.state = OperationState.FAILED
    operation.current_task_id = None
    operation.error_code = "manager_handoff_failed"
    operation.error_message = str(error)[:2000]
    operation.finished_at = datetime.now(timezone.utc)
    operation.updated_at = operation.finished_at
    operation.recovery = {"manager_handoff_rolled_back": True}
    store.update_operation(operation)
    for record in store.operation_tasks(operation_id):
        if record.task.kind != "prepare_manager_handoff":
            continue
        store.update_operation_task(
            operation_id,
            record.task.id,
            state=TaskState.ROLLED_BACK,
            attempt=record.attempt,
            result=record.result,
            error={
                "code": "manager_handoff_failed",
                "message": str(error)[:2000],
            },
            started_at=record.started_at,
            finished_at=operation.finished_at,
        )
    store.append_event(
        "operation.failed",
        {
            "operation_id": operation_id,
            "error": {
                "code": "manager_handoff_failed",
                "message": str(error)[:2000],
            },
            "rollback_errors": [],
        },
        operation_id=operation_id,
    )


def _terminate_launched(
    process: subprocess.Popen | None,
    *,
    layout: WorkspaceLayout | None = None,
    payload: ManagerHandoffPayload | None = None,
) -> None:
    """Terminate a launched Python process or PyInstaller bootloader tree."""

    targets: dict[tuple[int, float], psutil.Process] = {}

    def add(candidate: psutil.Process) -> None:
        try:
            key = (candidate.pid, candidate.create_time())
            targets[key] = candidate
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return

    if process is not None:
        try:
            root = psutil.Process(int(process.pid))
            for child in root.children(recursive=True):
                add(child)
            add(root)
        except (
            AttributeError,
            TypeError,
            ValueError,
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            OSError,
        ):
            pass
    if layout is not None and payload is not None:
        try:
            descriptor = ConnectionDescriptor.model_validate_json(
                layout.descriptor.read_text(encoding="utf-8")
            )
            candidate = psutil.Process(descriptor.pid)
            if (
                descriptor.manager_version == payload.version
                and Path(descriptor.executable).resolve(strict=False)
                == Path(payload.new_python).resolve(strict=False)
                and abs(candidate.create_time() - descriptor.process_create_time) <= 0.01
                and Path(candidate.exe()).resolve(strict=False)
                == Path(payload.new_python).resolve(strict=False)
            ):
                for child in candidate.children(recursive=True):
                    add(child)
                add(candidate)
        except (
            OSError,
            ValueError,
            psutil.NoSuchProcess,
            psutil.AccessDenied,
        ):
            pass
    ordered = list(targets.values())
    if not ordered:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=10)
            except Exception:
                pass
        return
    for candidate in ordered:
        try:
            candidate.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _gone, alive = psutil.wait_procs(ordered, timeout=10)
    for candidate in alive:
        try:
            candidate.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if alive:
        psutil.wait_procs(alive, timeout=10)
    if process is not None:
        try:
            process.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _cleanup_success(
    layout: WorkspaceLayout,
    operation_id: str,
    descriptor: Path,
    backup_root: Path,
) -> None:
    for path in (
        descriptor,
        backup_root / "manager.sqlite3",
        backup_root / "probe.sqlite3",
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    try:
        backup_root.rmdir()
    except OSError:
        pass
    operation_staging = layout.staging / operation_id
    if operation_staging.exists():
        layout.require_within(
            operation_staging,
            roots=(layout.staging,),
        )
        shutil.rmtree(operation_staging)


def _acquire_handoff_lock(path: Path) -> int | None:
    for _attempt in range(2):
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                raw_owner = path.read_text(encoding="ascii").strip()
                try:
                    lock_owner = json.loads(raw_owner)
                    owner = int(lock_owner["pid"])
                    create_time = float(lock_owner["create_time"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    # Compatibility with the first lock format, which stored
                    # only a PID. A live legacy owner remains conservatively
                    # authoritative because its identity cannot be proven.
                    owner = int(raw_owner)
                    create_time = None
                if create_time is None:
                    alive = psutil.pid_exists(owner)
                else:
                    process = psutil.Process(owner)
                    alive = abs(process.create_time() - create_time) <= 0.01
            except (OSError, TypeError, ValueError):
                alive = False
            except psutil.NoSuchProcess:
                alive = False
            except psutil.AccessDenied:
                alive = True
            if alive:
                return None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        owner_payload = json.dumps(
            {
                "pid": os.getpid(),
                "create_time": psutil.Process().create_time(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        os.write(descriptor, owner_payload.encode("ascii"))
        os.fsync(descriptor)
        try:
            protect_path(path)
        except Exception:
            os.close(descriptor)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        return descriptor
    return None


def _restart_previous_manager(
    payload: ManagerHandoffPayload,
) -> subprocess.Popen:
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
    return subprocess.Popen(
        command,
        cwd=payload.old_cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        **_process_options(),
    )


def _rollback_handoff(
    *,
    layout: WorkspaceLayout,
    payload: ManagerHandoffPayload,
    descriptor: Path,
    database_backup: Path,
    launched: subprocess.Popen | None,
    error: Exception,
) -> None:
    _terminate_launched(
        launched,
        layout=layout,
        payload=payload,
    )
    try:
        layout.descriptor.unlink()
    except FileNotFoundError:
        pass
    pointer = layout.root / "manager" / "current.json"
    if isinstance(payload.previous_pointer, dict):
        _atomic_json(pointer, payload.previous_pointer)
    else:
        try:
            pointer.unlink()
        except FileNotFoundError:
            pass
    if database_backup.is_file():
        _restore_sqlite(database_backup, layout.database)
    restored = ManagerStore(layout.database)
    _mark_handoff_failed(restored, payload.operation_id, error)
    slot = layout.require_within(
        payload.slot_path,
        roots=(layout.manager_versions,),
    )
    if slot.exists():
        shutil.rmtree(slot)
    failure_report = database_backup.parent / "failure.json"
    _atomic_json(
        failure_report,
        {
            "operation_id": payload.operation_id,
            "version": payload.version,
            "error_type": type(error).__name__,
            "message": str(error)[:2000],
            "rolled_back_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    try:
        descriptor.unlink()
    except FileNotFoundError:
        pass
    _restart_previous_manager(payload)


def run_handoff(
    workspace: str | Path,
    operation_id: str,
    *,
    shutdown_timeout: float = 60,
    health_timeout: float = 90,
) -> int:
    layout = WorkspaceLayout.from_value(workspace)
    envelope, descriptor = read_handoff(layout, operation_id)
    payload = envelope.payload
    lock_path = descriptor.with_suffix(".lock")
    _require_regular_control_file(
        lock_path,
        label="manager handoff lock",
    )
    lock_descriptor = _acquire_handoff_lock(lock_path)
    if lock_descriptor is None:
        return 0
    backup_root = layout.backups / operation_id / "manager-handoff"
    database_backup = backup_root / "manager.sqlite3"
    old_exited = False
    launched: subprocess.Popen | None = None
    try:
        _wait_for_old_manager(payload, shutdown_timeout)
        old_exited = True
        backup_root.mkdir(parents=True, exist_ok=True)
        if not database_backup.is_file() and not _snapshot_sqlite(
            layout.database,
            database_backup,
        ):
            raise RuntimeError("Manager state database is missing.")
        probe_database = backup_root / "probe.sqlite3"
        shutil.copy2(database_backup, probe_database)
        _probe_new_runtime(payload, probe_database)
        pointer = layout.root / "manager" / "current.json"
        _atomic_json(
            pointer,
            {
                "product": "pandrator-manager",
                "version": payload.version,
                "path": payload.slot_path,
                "manifest_digest": payload.manifest_digest,
                "sequence": payload.sequence,
                "activated_by": payload.operation_id,
            },
        )
        store = ManagerStore(layout.database)
        _commit_handoff(store, layout, payload)
        secret = read_client_secret(layout.credential)
        launch_command = (
            [
                payload.new_python,
                "daemon",
                "--workspace",
                payload.workspace,
                "--handoff-child",
                payload.operation_id,
            ]
            if payload.new_runtime_mode == "native_launcher"
            else [
                payload.new_python,
                "-m",
                "pandrator_manager.daemon",
                "--workspace",
                payload.workspace,
                "--handoff-child",
                payload.operation_id,
            ]
        )
        launched = subprocess.Popen(
            launch_command,
            cwd=payload.new_application_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            **_process_options(),
        )
        _wait_for_new_manager(
            layout,
            payload,
            secret,
            launched,
            health_timeout,
        )
        _cleanup_success(
            layout,
            operation_id,
            descriptor,
            backup_root,
        )
        return 0
    except Exception as error:
        if old_exited:
            try:
                _rollback_handoff(
                    layout=layout,
                    payload=payload,
                    descriptor=descriptor,
                    database_backup=database_backup,
                    launched=launched,
                    error=error,
                )
            except Exception as rollback_error:
                try:
                    store = ManagerStore(layout.database)
                    operation = store.get_operation(payload.operation_id)
                    operation.state = OperationState.RECOVERY_REQUIRED
                    operation.error_code = "manager_handoff_recovery_required"
                    operation.error_message = str(error)[:2000]
                    operation.recovery = {
                        "handoff_error": str(error)[:2000],
                        "rollback_error": str(rollback_error)[:2000],
                    }
                    operation.updated_at = datetime.now(timezone.utc)
                    store.update_operation(operation)
                except Exception:
                    pass
                raise RuntimeError(
                    f"Manager handoff and rollback both failed: {error}; rollback: {rollback_error}"
                ) from rollback_error
            return 2
        raise
    finally:
        os.close(lock_descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def probe_runtime(
    workspace: str | Path,
    database: Path,
    expected_version: str,
    operation_id: str,
) -> int:
    operation_id = _validated_operation_id(operation_id)
    if __version__ != expected_version:
        print(
            json.dumps(
                {
                    "ok": False,
                    "version": __version__,
                    "reason": "version mismatch",
                }
            )
        )
        return 2
    layout = WorkspaceLayout.from_value(workspace)
    probe = database.expanduser().resolve(strict=False)
    if not probe.is_file():
        print(
            json.dumps(
                {
                    "ok": False,
                    "version": __version__,
                    "reason": "probe database missing",
                }
            )
        )
        return 2
    store = ManagerStore(probe)
    operation = store.get_operation(operation_id)
    plan = store.get_plan(operation.plan_id)
    release = plan.impacts.get("release")
    operation_valid = bool(
        operation.state == OperationState.HANDOFF_PENDING
        and operation.kind == OperationKind.UPDATE
        and plan.kind == OperationKind.UPDATE
        and plan.workspace == str(layout.workspace)
        and isinstance(release, dict)
        and release.get("product") == "pandrator-manager"
        and release.get("version") == expected_version
    )
    report = {
        "ok": store.schema_version() >= 1 and operation_valid,
        "version": __version__,
        "schema_version": store.schema_version(),
        "workspace": str(layout.workspace),
        "operation_id": operation_id,
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 2


def pending_handoffs(layout: WorkspaceLayout) -> tuple[str, ...]:
    directory = _safe_handoff_directory(layout)
    if not os.path.lexists(directory):
        return ()
    pending: list[str] = []
    for path in directory.glob("*.json"):
        _require_regular_control_file(
            path,
            label="manager handoff descriptor",
        )
        pending.append(_validated_operation_id(path.stem))
    return tuple(sorted(pending))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pandrator-manager-handoff")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--operation-id")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--probe-database", type=Path)
    parser.add_argument("--expected-version")
    args = parser.parse_args(argv)
    if args.probe:
        if args.probe_database is None or not args.expected_version or not args.operation_id:
            parser.error(
                "--probe requires --probe-database, --expected-version, and --operation-id"
            )
        return probe_runtime(
            args.workspace,
            args.probe_database,
            args.expected_version,
            args.operation_id,
        )
    if not args.operation_id:
        parser.error("--operation-id is required")
    try:
        return run_handoff(args.workspace, args.operation_id)
    except Exception as error:
        print(f"Manager handoff failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
