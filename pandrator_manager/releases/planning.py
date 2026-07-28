"""Immutable plans for signed application and manager releases."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..artifacts import ArtifactDownloader, ArtifactSpec
from ..components import ComponentRegistry
from ..context import ManagerContext
from ..errors import ManagerError, RevisionConflict
from ..launcher import native_manager_installation
from ..legacy_data import LegacyDataInventory, legacy_data_inventory
from ..models import (
    ConfirmationRequirement,
    OperationKind,
    OperationPlan,
    PreflightCheck,
    TaskSpec,
)
from ..preflight import HostPreflight
from .authority import ReleaseAuthority, VerifiedRelease
from .bundles import release_cache_path


class ReleasePlanner:
    def __init__(
        self,
        context: ManagerContext,
        registry: ComponentRegistry,
        authority: ReleaseAuthority,
        *,
        plan_ttl_seconds: int = 15 * 60,
    ) -> None:
        self.context = context
        self.registry = registry
        self.authority = authority
        self.plan_ttl_seconds = max(60, int(plan_ttl_seconds))
        self.host_preflight = HostPreflight(context, registry)

    def _manager_is_native(self) -> bool:
        return native_manager_installation(self.context.layout)

    def _tasks(
        self,
        release: VerifiedRelease,
        *,
        offline: bool,
        start_after_activation: bool,
        legacy_inventory: LegacyDataInventory | None = None,
    ) -> tuple[TaskSpec, ...]:
        manifest = release.manifest.payload
        artifact = release.artifact
        common = {
            "manifest": release.envelope,
            "artifact": artifact.model_dump(mode="json"),
            "offline": bool(offline),
        }
        verify = TaskSpec(
            id="release:verify",
            kind="verify_release",
            label=f"Recheck signed {manifest.product} {manifest.version}",
            resource_locks=(
                f"release:{manifest.product}",
                "host:preflight",
            ),
            estimated_disk_bytes=artifact.size_bytes * 4,
            inputs=common,
            verification={
                "strategy": "embedded_threshold_signature_and_host_target",
                "manifest_digest": release.manifest.digest,
            },
            rollback={"strategy": "none"},
        )
        download = TaskSpec(
            id="release:download",
            kind="download_release",
            label=f"Download {artifact.filename}",
            dependencies=(verify.id,),
            resource_locks=(f"release:{manifest.product}", "cache:release"),
            estimated_download_bytes=artifact.size_bytes,
            estimated_disk_bytes=artifact.size_bytes,
            inputs={
                "artifact": artifact.model_dump(mode="json"),
                "offline": bool(offline),
            },
            expected_outputs=("verified release cache artifact",),
            verification={
                "strategy": "sha256_and_exact_size",
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            },
            rollback={"strategy": "retain_verified_digest_cache"},
        )
        stage = TaskSpec(
            id="release:stage",
            kind="stage_release",
            label=f"Safely stage {manifest.product} {manifest.version}",
            dependencies=(download.id,),
            resource_locks=(f"release:{manifest.product}",),
            estimated_disk_bytes=artifact.size_bytes * 3,
            inputs={
                "product": manifest.product,
                "version": manifest.version,
                "artifact": artifact.model_dump(mode="json"),
            },
            expected_outputs=("validated private runtime bundle",),
            verification={
                "strategy": "safe_extract_and_bundle_contract",
                "manifest_digest": release.manifest.digest,
            },
            rollback={"strategy": "remove_operation_staging"},
        )
        if manifest.product == "pandrator":
            stop = TaskSpec(
                id="release:stop-application",
                kind="stop_application_release",
                label="Stop Pandrator for atomic activation",
                dependencies=(stage.id,),
                resource_locks=("release:pandrator", "service:pandrator"),
                inputs={},
                rollback={"strategy": "restore_prior_desired_services"},
            )
            previous = stop.id
            reconcile: TaskSpec | None = None
            if legacy_inventory is not None and legacy_inventory.items:
                reconcile = TaskSpec(
                    id="release:legacy-data",
                    kind="reconcile_legacy_data",
                    label="Reconcile legacy Pandrator data",
                    dependencies=(stop.id,),
                    resource_locks=(
                        "release:pandrator",
                        "database:pandrator",
                        "data:pandrator",
                    ),
                    estimated_disk_bytes=legacy_inventory.size_bytes,
                    inputs={"inventory": legacy_inventory.as_dict()},
                    expected_outputs=(
                        "known legacy data copied without overwrites",
                    ),
                    verification={
                        "strategy": "atomic_copy_and_digest_or_sqlite_backup",
                        "sources_retained": True,
                    },
                    rollback={
                        "strategy": "remove_only_operation_created_copies",
                    },
                )
                previous = reconcile.id
            activate = TaskSpec(
                id="release:activate",
                kind="activate_application_release",
                label=f"Activate and health-check Pandrator {manifest.version}",
                dependencies=(previous,),
                resource_locks=(
                    "release:pandrator",
                    "service:pandrator",
                    "database:pandrator",
                ),
                inputs={
                    **common,
                    "start_after_activation": bool(start_after_activation),
                },
                expected_outputs=(
                    "active application slot",
                    "healthy application API",
                ),
                verification={
                    "strategy": "migration_and_service_identity_health",
                    "version": manifest.version,
                },
                rollback={
                    "strategy": (
                        "restore_pointer_database_runtime_specs_and_services"
                    )
                },
                cancellation_boundary=False,
            )
            return (
                (verify, download, stage, stop, reconcile, activate)
                if reconcile is not None
                else (verify, download, stage, stop, activate)
            )
        handoff = TaskSpec(
            id="release:manager-handoff",
            kind="prepare_manager_handoff",
            label=f"Prepare Pandrator Manager {manifest.version} handoff",
            dependencies=(stage.id,),
            resource_locks=("release:pandrator-manager",),
            inputs=common,
            expected_outputs=("authenticated pending manager handoff",),
            verification={"strategy": "stable_helper_handoff"},
            rollback={"strategy": "remove_pending_manager_slot"},
            cancellation_boundary=False,
        )
        return verify, download, stage, handoff

    def create_plan(
        self,
        document: Mapping[str, Any],
        *,
        expected_revision: int,
        actual_revision: int,
        offline: bool = False,
        start_after_activation: bool = True,
    ) -> OperationPlan:
        if expected_revision != actual_revision:
            raise RevisionConflict(expected_revision, actual_revision)
        release = self.authority.verify(document)
        payload = release.manifest.payload
        legacy_inventory = (
            legacy_data_inventory(self.context.layout)
            if payload.product == "pandrator" and not release.exact_replay
            else None
        )
        if (
            payload.product == "pandrator-manager"
            and not release.exact_replay
            and not self._manager_is_native()
        ):
            raise ManagerError(
                "external_manager_update_required",
                "This manager is controlled by a Python tool installer and "
                "will not modify its active environment.",
                {
                    "product": payload.product,
                    "version": payload.version,
                    "commands": (
                        "pipx upgrade pandrator-manager",
                        "uv tool upgrade pandrator-manager",
                    ),
                    "restart": "pandrator-manager daemon restart",
                },
                409,
            )
        if not release.exact_replay and release.artifact.kind not in {
            "zip",
            "tar",
        }:
            raise ManagerError(
                "unsupported_release_artifact",
                "Atomic product activation requires a signed private-runtime "
                "ZIP or TAR bundle.",
                {
                    "filename": release.artifact.filename,
                    "kind": release.artifact.kind,
                },
                409,
            )
        tasks = (
            ()
            if release.exact_replay
            else self._tasks(
                release,
                offline=offline,
                start_after_activation=start_after_activation,
                legacy_inventory=legacy_inventory,
            )
        )
        checks: list[PreflightCheck] = [
            PreflightCheck(
                code="release.signature",
                status="pass",
                message="The release met the embedded signature threshold.",
                details={
                    "manifest_digest": release.manifest.digest,
                    "verified_key_ids": list(
                        release.manifest.verified_key_ids
                    ),
                    "sequence": payload.sequence,
                },
            ),
            PreflightCheck(
                code="release.target",
                status="pass",
                message="The signed release selects exactly one host artifact.",
                details={
                    "system": self.context.system,
                    "architecture": self.context.architecture,
                    "filename": release.artifact.filename,
                },
            ),
        ]
        checks.extend(
            self.host_preflight.evaluate(desired={}, tasks=tasks)
        )
        if offline and tasks:
            spec = ArtifactSpec(
                url=release.artifact.url,
                sha256=release.artifact.sha256,
                size_bytes=release.artifact.size_bytes,
                filename=release.artifact.filename,
            )
            cached = release_cache_path(
                self.context.layout,
                release.artifact,
            )
            available = ArtifactDownloader.matches(cached, spec)
            checks.append(
                PreflightCheck(
                    code="release.offline_cache",
                    status="pass" if available else "error",
                    message=(
                        "The exact signed artifact is available in the local cache."
                        if available
                        else "Offline release activation requires the exact "
                        "signed artifact in the local cache."
                    ),
                    details={"path": str(cached)},
                )
            )
        selected_checks = tuple(checks)
        self.host_preflight.require_success(selected_checks)
        warnings = [
            check.message
            for check in selected_checks
            if check.status == "warning"
        ]
        if payload.channel != "stable":
            warnings.append(
                f"This is a {payload.channel} release, not the stable channel."
            )
        if release.exact_replay:
            warnings.append(
                "This exact signed release is already active; no host mutation "
                "is planned."
            )
        confirmations = (
            (
                ConfirmationRequirement(
                    kind="restart",
                    key=f"activate-release:{payload.product}:{payload.version}",
                    message=(
                        f"Activate {payload.product} {payload.version}; the "
                        "application will restart and its database may migrate."
                    ),
                ),
            )
            if tasks
            else ()
        )
        created_at = datetime.fromtimestamp(
            self.context.clock.time(),
            timezone.utc,
        )
        expires_at = created_at + timedelta(seconds=self.plan_ttl_seconds)
        plan_id = str(uuid.uuid4())
        impacts = {
            "release": {
                "product": payload.product,
                "channel": payload.channel,
                "version": payload.version,
                "sequence": payload.sequence,
                "manifest_digest": release.manifest.digest,
                "artifact": release.artifact.model_dump(mode="json"),
                "exact_replay": release.exact_replay,
                "database_snapshot": payload.product == "pandrator",
                "service_restart": payload.product == "pandrator",
                "legacy_data": (
                    legacy_inventory.as_dict()
                    if legacy_inventory is not None
                    and legacy_inventory.items
                    else None
                ),
            }
        }
        digest_payload = {
            "id": plan_id,
            "kind": OperationKind.UPDATE.value,
            "workspace": str(self.context.layout.workspace),
            "expected_revision": expected_revision,
            "desired": {},
            "inspections": {},
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "preflight": [
                check.model_dump(mode="json") for check in selected_checks
            ],
            "confirmations": [
                item.model_dump(mode="json") for item in confirmations
            ],
            "warnings": warnings,
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
            kind=OperationKind.UPDATE,
            workspace=str(self.context.layout.workspace),
            expected_revision=expected_revision,
            desired={},
            inspections={},
            tasks=tasks,
            preflight=selected_checks,
            confirmations=confirmations,
            warnings=tuple(warnings),
            impacts=impacts,
            estimated_download_bytes=sum(
                task.estimated_download_bytes for task in tasks
            ),
            estimated_disk_bytes=sum(
                task.estimated_disk_bytes for task in tasks
            ),
            created_at=created_at,
            expires_at=expires_at,
            digest=digest,
        )
