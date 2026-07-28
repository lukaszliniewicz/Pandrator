"""Whole-product lifecycle planning independent of UI and transport."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .context import ManagerContext, WorkspaceLayout
from .errors import ManagerError, RevisionConflict
from .launcher import external_cleanup_runtime
from .legacy_data import legacy_data_inventory
from .models import (
    ConfirmationRequirement,
    OperationKind,
    OperationPlan,
    PreflightCheck,
    TaskSpec,
)


def external_cleanup_runtime_available(
    context_or_layout: ManagerContext | WorkspaceLayout,
) -> bool:
    """A Python runtime or staged native launcher must survive root removal."""

    layout = (
        context_or_layout.layout
        if isinstance(context_or_layout, ManagerContext)
        else context_or_layout
    )
    return external_cleanup_runtime(layout) is not None


def resolve_export_destination(
    context: ManagerContext,
    destination: str | os.PathLike[str] | None,
) -> Path | None:
    if destination is None:
        return None
    selected = Path(destination).expanduser().resolve(strict=False)
    if selected == context.layout.workspace or context.layout.contains(
        context.layout.root,
        selected,
    ):
        raise ManagerError(
            "unsafe_export_destination",
            "The data export must be outside the installation being removed.",
            {"path": str(selected)},
            409,
        )
    if selected.exists():
        raise ManagerError(
            "export_destination_exists",
            "The data export destination already exists and will not be overwritten.",
            {"path": str(selected)},
            409,
        )
    if not selected.parent.is_dir() or not os.access(selected.parent, os.W_OK):
        raise ManagerError(
            "export_destination_unwritable",
            "The data export parent directory is missing or not writable.",
            {"path": str(selected.parent)},
            409,
        )
    return selected


def _tree_size(root: Path) -> tuple[int, int]:
    total = 0
    files = 0
    if not root.is_dir():
        return 0, 0
    for directory, names, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in (*names, *filenames):
            selected = current / name
            junction = getattr(selected, "is_junction", None)
            if selected.is_symlink() or bool(
                junction is not None and junction()
            ):
                raise ManagerError(
                    "unsafe_data_export",
                    "Data export refuses symbolic links.",
                    {"path": str(selected)},
                    409,
                )
        for name in filenames:
            selected = current / name
            try:
                total += selected.stat().st_size
            except OSError as error:
                raise ManagerError(
                    "data_export_inspection_failed",
                    "A data file could not be inspected for export.",
                    {
                        "path": str(selected),
                        "error_type": type(error).__name__,
                    },
                    409,
                ) from error
            files += 1
    return total, files


class LifecyclePlanner:
    def __init__(
        self,
        context: ManagerContext,
        *,
        plan_ttl_seconds: int = 15 * 60,
    ) -> None:
        self.context = context
        self.plan_ttl_seconds = max(60, int(plan_ttl_seconds))

    def create_uninstall_plan(
        self,
        *,
        expected_revision: int,
        actual_revision: int,
        purge_data: bool = False,
        export_data: str | os.PathLike[str] | None = None,
    ) -> OperationPlan:
        if expected_revision != actual_revision:
            raise RevisionConflict(expected_revision, actual_revision)
        cleanup_runtime = external_cleanup_runtime(self.context.layout)
        if cleanup_runtime is None:
            raise ManagerError(
                "stable_uninstall_helper_unavailable",
                "This native manager build cannot uninstall itself until its "
                "stable external cleanup launcher is installed.",
                {
                    "managed_root": str(self.context.layout.root),
                    "required": "external stable cleanup launcher",
                },
                409,
            )
        destination = resolve_export_destination(
            self.context,
            export_data,
        )
        data_bytes, data_files = _tree_size(self.context.layout.data)
        legacy_inventory = legacy_data_inventory(self.context.layout)
        reconcile_legacy = bool(
            legacy_inventory.items
            and (not purge_data or destination is not None)
        )
        preflight: list[PreflightCheck] = [
            PreflightCheck(
                code="uninstall.external_helper",
                status="pass",
                message=(
                    "A stable cleanup runtime can operate outside the managed "
                    "installation."
                ),
                details={
                    "mode": cleanup_runtime.mode,
                    "executable": str(cleanup_runtime.executable),
                },
            )
        ]
        if destination is not None:
            free = shutil.disk_usage(destination.parent).free
            # ZIP compression is content-dependent. Reserve the uncompressed
            # size plus a bounded margin rather than assuming compression.
            required = (
                data_bytes
                + (
                    legacy_inventory.size_bytes
                    if reconcile_legacy
                    else 0
                )
                + 64 * 1024 * 1024
            )
            if free < required:
                raise ManagerError(
                    "export_disk_space_insufficient",
                    "The export destination does not have enough free space.",
                    {
                        "free_bytes": free,
                        "required_bytes": required,
                        "path": str(destination.parent),
                    },
                    409,
                )
            preflight.append(
                PreflightCheck(
                    code="uninstall.data_export",
                    status="pass",
                    message="The data export destination is writable and has sufficient headroom.",
                    details={
                        "destination": str(destination),
                        "data_bytes": data_bytes,
                        "file_count": (
                            data_files
                            + (
                                legacy_inventory.file_count
                                if reconcile_legacy
                                else 0
                            )
                        ),
                        "free_bytes": free,
                    },
                )
            )
        stop = TaskSpec(
            id="uninstall:stop-services",
            kind="stop_all_services",
            label="Stop all positively owned managed services",
            resource_locks=("service:all", "lifecycle:uninstall"),
            rollback={"strategy": "restore_prior_desired_services"},
        )
        tasks: list[TaskSpec] = [stop]
        previous = stop.id
        if reconcile_legacy:
            reconcile = TaskSpec(
                id="uninstall:legacy-data",
                kind="reconcile_legacy_data",
                label="Preserve known data embedded in the legacy installation",
                dependencies=(previous,),
                resource_locks=(
                    "data:pandrator",
                    "lifecycle:uninstall",
                ),
                estimated_disk_bytes=legacy_inventory.size_bytes,
                inputs={"inventory": legacy_inventory.as_dict()},
                expected_outputs=(
                    "known legacy data copied without overwrites",
                ),
                verification={
                    "strategy": "atomic_copy_and_digest_or_sqlite_backup",
                    "sources_retained_until_uninstall_commit": True,
                },
                rollback={
                    "strategy": "remove_only_operation_created_copies",
                },
            )
            tasks.append(reconcile)
            previous = reconcile.id
        if destination is not None:
            export = TaskSpec(
                id="uninstall:export-data",
                kind="export_uninstall_data",
                label="Export preserved Pandrator data",
                dependencies=(previous,),
                resource_locks=("data:pandrator", "lifecycle:uninstall"),
                estimated_disk_bytes=(
                    data_bytes
                    + (
                        legacy_inventory.size_bytes
                        if reconcile_legacy
                        else 0
                    )
                ),
                inputs={
                    "destination": str(destination),
                    "source": str(self.context.layout.data),
                },
                expected_outputs=("atomic ZIP data export",),
                verification={
                    "strategy": "zip_crc_and_member_containment",
                    "source_bytes": (
                        data_bytes
                        + (
                            legacy_inventory.size_bytes
                            if reconcile_legacy
                            else 0
                        )
                    ),
                    "source_files": (
                        data_files
                        + (
                            legacy_inventory.file_count
                            if reconcile_legacy
                            else 0
                        )
                    ),
                },
                rollback={"strategy": "remove_operation_created_export"},
            )
            tasks.append(export)
            previous = export.id
        handoff = TaskSpec(
            id="uninstall:handoff",
            kind="prepare_uninstall_handoff",
            label="Hand uninstall to the external cleanup runtime",
            dependencies=(previous,),
            resource_locks=("lifecycle:uninstall",),
            inputs={
                "purge_data": bool(purge_data),
                "export_data": (
                    str(destination) if destination is not None else None
                ),
            },
            expected_outputs=("authenticated pending uninstall handoff",),
            verification={"strategy": "external_runtime_and_hmac_descriptor"},
            rollback={"strategy": "remove_pending_uninstall_descriptor"},
            cancellation_boundary=False,
        )
        tasks.append(handoff)
        confirmations = [
            ConfirmationRequirement(
                kind="destructive",
                key="uninstall:software",
                message=(
                    "Remove manager-owned Pandrator application, component, "
                    "environment, cache, log, state, and autostart files."
                ),
            )
        ]
        if purge_data:
            confirmations.append(
                ConfirmationRequirement(
                    kind="destructive",
                    key="uninstall:purge-data",
                    message=(
                        "Permanently delete Pandrator user data after any "
                        "requested export completes."
                    ),
                )
            )
        created_at = datetime.fromtimestamp(
            self.context.clock.time(),
            timezone.utc,
        )
        expires_at = created_at + timedelta(seconds=self.plan_ttl_seconds)
        plan_id = str(uuid.uuid4())
        impacts = {
            "uninstall": {
                "purge_data": bool(purge_data),
                "preserve_data": not purge_data,
                "export_data": (
                    str(destination) if destination is not None else None
                ),
                "data_bytes": data_bytes,
                "data_files": data_files,
                "legacy_data": (
                    legacy_inventory.as_dict()
                    if legacy_inventory.items
                    else None
                ),
                "legacy_data_reconciled": reconcile_legacy,
                "package_distribution_retained": True,
            }
        }
        digest_payload: dict[str, Any] = {
            "id": plan_id,
            "kind": OperationKind.UNINSTALL.value,
            "workspace": str(self.context.layout.workspace),
            "expected_revision": expected_revision,
            "desired": {},
            "inspections": {},
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "preflight": [
                check.model_dump(mode="json") for check in preflight
            ],
            "confirmations": [
                confirmation.model_dump(mode="json")
                for confirmation in confirmations
            ],
            "warnings": [],
            "impacts": impacts,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return OperationPlan(
            id=plan_id,
            kind=OperationKind.UNINSTALL,
            workspace=str(self.context.layout.workspace),
            expected_revision=expected_revision,
            desired={},
            inspections={},
            tasks=tuple(tasks),
            preflight=tuple(preflight),
            confirmations=tuple(confirmations),
            impacts=impacts,
            estimated_disk_bytes=data_bytes if destination is not None else 0,
            created_at=created_at,
            expires_at=expires_at,
            digest=digest,
        )
