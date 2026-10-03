"""Whole-product uninstall task execution and rollback without instance state."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import stat
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..context import WorkspaceLayout
from ..errors import ManagerError
from ..models import TaskSpec
from ..releases.slots import _atomic_json
from ..state import ManagerStore
from ..uninstall import _file_sha256, prepare_uninstall_handoff, rollback_prepared_uninstall
from .contracts import OperationTaskContext


class UninstallTasks:
    """Stateless whole-product uninstall task methods used by the handler."""

    @staticmethod
    def _prepare_uninstall_handoff(
        *,
        layout: WorkspaceLayout,
        store: ManagerStore,
        operation_id: str,
        expected_revision: int,
        purge_data: bool,
        export_data: str | None,
        prior_services: Mapping[str, Any],
    ) -> dict[str, Any]:
        return prepare_uninstall_handoff(
            layout=layout,
            store=store,
            operation_id=operation_id,
            expected_revision=expected_revision,
            purge_data=purge_data,
            export_data=export_data,
            prior_services=prior_services,
        )

    @staticmethod
    def _rollback_prepared_uninstall(
        *,
        layout: WorkspaceLayout,
        operation_id: str,
    ) -> None:
        rollback_prepared_uninstall(
            layout=layout,
            operation_id=operation_id,
        )

    def _execute_stop_all_services(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        del task
        supervisor = execution.supervisor
        if supervisor is None:
            raise ManagerError(
                "supervisor_unavailable",
                "Uninstall requires the live manager supervisor.",
                http_status=409,
            )
        before = {service.id: service.model_dump(mode="json") for service in supervisor.snapshot()}
        stopped = supervisor.stop_all()
        after = {service.id: service for service in supervisor.snapshot()}
        still_live = [
            service_id for service_id, service in after.items() if service.process is not None
        ]
        if still_live:
            raise ManagerError(
                "managed_services_still_running",
                "One or more managed services remained live after shutdown.",
                {"service_ids": sorted(still_live)},
                409,
            )
        return {
            "services": before,
            "stopped_service_ids": [service.id for service in stopped],
        }

    def _rollback_stop_all_services(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        del task
        supervisor = execution.supervisor
        if supervisor is None:
            return
        services = result.get("services")
        if not isinstance(services, dict):
            return
        for service_id, serialized in services.items():
            if not isinstance(serialized, dict):
                continue
            desired_running = bool(serialized.get("desired_running"))
            was_running = isinstance(serialized.get("process"), dict)
            if not (desired_running or was_running):
                continue
            current = next(
                (service for service in supervisor.snapshot() if service.id == service_id),
                None,
            )
            if current is None or current.process is None:
                supervisor.start(service_id)

    @staticmethod
    def _link_like(path: Path) -> bool:
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(junction is not None and junction())

    @staticmethod
    def _export_journal(execution: OperationTaskContext) -> Path:
        layout = execution.context.layout
        return layout.require_within(
            layout.staging / execution.operation.id / "uninstall-export.json",
            roots=(layout.staging,),
        )

    @staticmethod
    def _publish_export(temporary: Path, destination: Path) -> None:
        if os.name == "nt":
            os.rename(temporary, destination)
            return
        if sys.platform.startswith("linux"):
            libc = ctypes.CDLL(None, use_errno=True)
            rename = getattr(libc, "renameat2", None)
            if rename is not None:
                rename.argtypes = [
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                ]
                rename.restype = ctypes.c_int
                result = rename(
                    -100,
                    os.fsencode(temporary),
                    -100,
                    os.fsencode(destination),
                    1,
                )
                if result == 0:
                    return
                error_code = ctypes.get_errno()
                if error_code not in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                    raise OSError(error_code, os.strerror(error_code), str(destination))
        os.link(temporary, destination)

    def _execute_export_uninstall_data(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        impact = execution.plan.impacts.get("uninstall")
        destination_value = task.inputs.get("destination")
        source_value = task.inputs.get("source")
        if (
            not isinstance(impact, dict)
            or not isinstance(destination_value, str)
            or impact.get("export_data") != destination_value
            or not isinstance(source_value, str)
        ):
            raise ManagerError(
                "invalid_uninstall_operation",
                "The data export differs from the reviewed uninstall plan.",
                http_status=500,
            )
        destination = Path(destination_value).expanduser().resolve(strict=False)
        source = Path(source_value).expanduser().resolve(strict=False)
        if source != execution.context.layout.data.resolve(strict=False):
            raise ManagerError(
                "invalid_uninstall_operation",
                "The data export source is not the managed data directory.",
                http_status=500,
            )
        if destination.exists():
            raise ManagerError(
                "export_destination_exists",
                "The data export destination now exists and will not be overwritten.",
                {"path": str(destination)},
                409,
            )
        journal = self._export_journal(execution)
        if os.path.lexists(journal):
            raise ManagerError(
                "uninstall_export_recovery_pending",
                "A previous data export needs recovery. Resolve its journal before "
                "retrying; the recovery evidence will not be overwritten.",
                {"path": str(journal)},
                409,
            )
        destination.parent.mkdir(parents=False, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        files = 0
        source_bytes = 0
        try:
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=True,
            ) as archive:
                if source.is_dir():
                    for directory, names, filenames in os.walk(
                        source,
                        followlinks=False,
                    ):
                        current = Path(directory)
                        for name in (*names, *filenames):
                            selected = current / name
                            if self._link_like(selected):
                                raise ManagerError(
                                    "unsafe_data_export",
                                    "Data export refuses symbolic links and junctions.",
                                    {"path": str(selected)},
                                    409,
                                )
                        for name in sorted(filenames):
                            execution.check_cancelled()
                            selected = current / name
                            relative = selected.relative_to(source)
                            archive.write(
                                selected,
                                (Path("data") / relative).as_posix(),
                            )
                            source_bytes += selected.stat().st_size
                            files += 1
                archive.comment = b"Pandrator Manager data export"
            with zipfile.ZipFile(temporary, "r") as verification:
                bad_member = verification.testzip()
                if bad_member is not None:
                    raise RuntimeError(f"Export verification failed at {bad_member}.")
            journal.parent.mkdir(parents=True, exist_ok=True)
            temporary_stat = temporary.stat()
            _atomic_json(
                journal,
                {
                    "schema_version": 1,
                    "operation_id": execution.operation.id,
                    "task_id": task.id,
                    "destination": str(destination),
                    "sha256": _file_sha256(temporary),
                    "device": temporary_stat.st_dev,
                    "inode": temporary_stat.st_ino,
                },
            )
            try:
                self._publish_export(temporary, destination)
            except FileExistsError:
                journal.unlink()
                raise ManagerError(
                    "export_destination_exists",
                    "The data export destination now exists and will not be overwritten.",
                    {"path": str(destination)},
                    409,
                ) from None
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return {
            "created": True,
            "destination": str(destination),
            "source_bytes": source_bytes,
            "files": files,
        }

    def _rollback_export_uninstall_data(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        journal = self._export_journal(execution)
        journal_exists = os.path.lexists(journal)
        if not journal_exists and not result.get("created"):
            return
        invalid_journal = ManagerError(
            "invalid_uninstall_export_journal",
            "The data export recovery journal does not match the reviewed operation. "
            "The destination and journal have been preserved for recovery.",
            {"path": str(journal)},
            409,
        )
        impact = execution.plan.impacts.get("uninstall")
        destination_value = task.inputs.get("destination")
        if (
            not isinstance(impact, dict)
            or not isinstance(destination_value, str)
            or not isinstance(impact.get("export_data"), str)
            or impact.get("export_data") != destination_value
        ):
            raise invalid_journal
        try:
            destination = Path(destination_value).expanduser().resolve(strict=False)
        except (OSError, ValueError):
            raise invalid_journal from None
        if not journal_exists:
            if not os.path.lexists(destination):
                return
            raise ManagerError(
                "uninstall_export_ownership_unverified",
                "The data export ownership journal is missing. The destination "
                "has been preserved and needs recovery before rollback can continue.",
                {"path": str(destination)},
                409,
            )
        try:
            evidence = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise invalid_journal from None
        if not isinstance(evidence, dict):
            raise invalid_journal
        digest = evidence.get("sha256")
        device = evidence.get("device")
        inode = evidence.get("inode")
        if (
            type(evidence.get("schema_version")) is not int
            or evidence.get("schema_version") != 1
            or evidence.get("operation_id") != execution.operation.id
            or evidence.get("task_id") != task.id
            or evidence.get("destination") != str(destination)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(device) is not int
            or device < 0
            or type(inode) is not int
            or inode <= 0
        ):
            raise invalid_journal
        if not os.path.lexists(destination):
            journal.unlink()
            return
        changed = ManagerError(
            "uninstall_export_changed",
            "The data export destination has changed since publication. Its contents "
            "and ownership journal have been preserved for recovery.",
            {"path": str(destination)},
            409,
        )
        try:
            before_hash = destination.lstat()
            if (
                not stat.S_ISREG(before_hash.st_mode)
                or before_hash.st_dev != device
                or before_hash.st_ino != inode
            ):
                raise changed
            actual_digest = _file_sha256(destination)
            after_hash = destination.lstat()
        except OSError:
            raise changed from None
        if actual_digest != digest or (
            after_hash.st_dev,
            after_hash.st_ino,
            after_hash.st_size,
            after_hash.st_mtime_ns,
        ) != (
            before_hash.st_dev,
            before_hash.st_ino,
            before_hash.st_size,
            before_hash.st_mtime_ns,
        ):
            raise changed
        destination.unlink()
        journal.unlink()

    def _execute_prepare_uninstall_handoff(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        impact = execution.plan.impacts.get("uninstall")
        if not isinstance(impact, dict):
            raise ManagerError(
                "invalid_uninstall_operation",
                "The persisted operation has no uninstall impact record.",
                http_status=500,
            )
        purge_data = bool(task.inputs.get("purge_data"))
        export_data = task.inputs.get("export_data")
        if purge_data != bool(impact.get("purge_data")) or export_data != impact.get("export_data"):
            raise ManagerError(
                "invalid_uninstall_operation",
                "The uninstall handoff differs from the reviewed plan.",
                http_status=500,
            )
        stopped = execution.prior_results.get(
            "uninstall:stop-services",
            {},
        )
        prior_services = stopped.get("services")
        if not isinstance(prior_services, dict):
            raise ManagerError(
                "invalid_uninstall_operation",
                "The uninstall has no verified service shutdown record.",
                http_status=500,
            )
        return self._prepare_uninstall_handoff(
            layout=execution.context.layout,
            store=execution.store,
            operation_id=execution.operation.id,
            expected_revision=execution.plan.expected_revision,
            purge_data=purge_data,
            export_data=export_data if isinstance(export_data, str) else None,
            prior_services=prior_services,
        )

    def _rollback_prepare_uninstall_handoff(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        del task, result
        self._rollback_prepared_uninstall(
            layout=execution.context.layout,
            operation_id=execution.operation.id,
        )
