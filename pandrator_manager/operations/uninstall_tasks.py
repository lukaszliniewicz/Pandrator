"""Whole-product uninstall task execution and rollback without instance state."""

from __future__ import annotations

import os
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..context import WorkspaceLayout
from ..errors import ManagerError
from ..models import TaskSpec
from ..state import ManagerStore
from ..uninstall import prepare_uninstall_handoff, rollback_prepared_uninstall
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
            os.replace(temporary, destination)
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
        del task
        if not result.get("created"):
            return
        destination_value = result.get("destination")
        if not isinstance(destination_value, str):
            return
        destination = Path(destination_value).expanduser().resolve(strict=False)
        impact = execution.plan.impacts.get("uninstall")
        if isinstance(impact, dict) and impact.get("export_data") == str(destination):
            try:
                destination.unlink()
            except FileNotFoundError:
                pass

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
