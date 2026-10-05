"""Signed release task execution and rollback without per-instance state."""

from __future__ import annotations

import shutil
from typing import Any

from ..artifacts import ArtifactDownloader, ArtifactSpec, SafeExtractor
from ..context import WorkspaceLayout
from ..errors import ManagerError
from ..legacy_data import (
    legacy_data_inventory,
    reconcile_legacy_data,
    rollback_legacy_data,
)
from ..models import ManagedProcessSpec, PreflightCheck, TaskSpec
from ..preflight import HostPreflight
from ..releases.authority import ReleaseAuthority
from ..releases.bundles import release_cache_path, validate_release_bundle
from ..releases.handoff import (
    prepare_manager_handoff,
    rollback_prepared_manager_handoff,
)
from ..releases.models import ReleaseArtifact
from ..releases.slots import ReleaseSlotManager
from ..runtime_specs import (
    PANDRATOR_API_SERVICE,
    PANDRATOR_CORE_SERVICES,
    PANDRATOR_MCP_SERVICE,
    PANDRATOR_SERVICE_START_ORDER,
    PANDRATOR_SERVICE_STOP_ORDER,
    PANDRATOR_WORKER_SERVICE,
    pandrator_runtime_specs,
)
from .contracts import OperationTaskContext, UnsupportedTask


class ReleaseTasks:
    """Stateless signed-release task methods used by the filesystem handler."""

    @staticmethod
    def _release_runtime_specs(layout: WorkspaceLayout) -> tuple[ManagedProcessSpec, ...]:
        return pandrator_runtime_specs(layout)

    @staticmethod
    def _authority(execution: OperationTaskContext) -> ReleaseAuthority:
        if execution.release_authority is None:
            raise UnsupportedTask(
                "Signed release operations are unavailable in this manager process."
            )
        return execution.release_authority

    @staticmethod
    def _release_artifact(value: object) -> ReleaseArtifact:
        try:
            return ReleaseArtifact.model_validate(value)
        except Exception as error:
            raise ManagerError(
                "invalid_release_operation",
                "The persisted release artifact contract is invalid.",
                {"reason": str(error)},
                500,
            ) from error

    @staticmethod
    def _prior(
        execution: OperationTaskContext,
        task_id: str,
    ) -> dict:
        try:
            return execution.prior_results[task_id]
        except KeyError:
            raise RuntimeError(f"Missing successful prerequisite result: {task_id}") from None

    def _execute_verify_release(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        execution.check_cancelled()
        manifest_value = task.inputs.get("manifest")
        if not isinstance(manifest_value, dict):
            raise ManagerError(
                "invalid_release_operation",
                "The persisted release operation has no signed manifest.",
                http_status=500,
            )
        release = self._authority(execution).verify(manifest_value)
        planned_artifact = self._release_artifact(task.inputs.get("artifact"))
        if planned_artifact.model_dump(mode="json") != release.artifact.model_dump(mode="json"):
            raise ManagerError(
                "release_plan_changed",
                "The host artifact selected during execution differs from "
                "the reviewed release plan.",
                http_status=409,
            )
        checks = list(
            HostPreflight(
                execution.context,
                execution.registry,
            ).evaluate(
                desired={},
                tasks=execution.plan.tasks,
            )
        )
        if bool(task.inputs.get("offline")):
            spec = ArtifactSpec(
                url=release.artifact.url,
                sha256=release.artifact.sha256,
                size_bytes=release.artifact.size_bytes,
                filename=release.artifact.filename,
            )
            cached = release_cache_path(
                execution.context.layout,
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
        HostPreflight.require_success(selected_checks)
        return {
            "product": release.manifest.payload.product,
            "channel": release.manifest.payload.channel,
            "version": release.manifest.payload.version,
            "sequence": release.manifest.payload.sequence,
            "manifest_digest": release.manifest.digest,
            "verified_key_ids": list(release.manifest.verified_key_ids),
            "artifact": release.artifact.model_dump(mode="json"),
            "checks": [check.model_dump(mode="json") for check in selected_checks],
        }

    def _execute_download_release(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        execution.check_cancelled()
        artifact = self._release_artifact(task.inputs.get("artifact"))
        specification = ArtifactSpec(
            url=artifact.url,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            filename=artifact.filename,
        )
        destination = release_cache_path(
            execution.context.layout,
            artifact,
        )
        selected = ArtifactDownloader(
            cancellation=execution.cancellation,
            environment=execution.context.environment,
        ).download_with_result(
            specification,
            destination,
            offline=bool(task.inputs.get("offline")),
        )
        return {
            "artifact_path": str(selected.path),
            "artifact": artifact.model_dump(mode="json"),
            "cache_reused": selected.cache_reused,
        }

    def _execute_stage_release(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        execution.check_cancelled()
        artifact = self._release_artifact(task.inputs.get("artifact"))
        download = self._prior(execution, "release:download")
        source = execution.context.layout.require_within(
            str(download.get("artifact_path") or ""),
            roots=(execution.context.layout.cache,),
        )
        specification = ArtifactSpec(
            url=artifact.url,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            filename=artifact.filename,
        )
        if not ArtifactDownloader.matches(source, specification):
            raise ManagerError(
                "release_cache_corrupt",
                "The cached release artifact no longer matches its signed digest.",
                {"path": str(source)},
                409,
            )
        product = str(task.inputs.get("product") or "")
        version = str(task.inputs.get("version") or "")
        target = execution.context.layout.staging / execution.operation.id / "release" / "bundle"
        target = execution.context.layout.require_within(
            target,
            roots=(execution.context.layout.staging,),
        )
        if target.is_dir():
            try:
                validated = validate_release_bundle(
                    target,
                    product=product,
                    version=version,
                )
            except ManagerError:
                shutil.rmtree(target)
            else:
                return {
                    "staged_path": str(validated.root),
                    "application_root": str(validated.application_root),
                    "python": str(validated.python),
                    "bundle": validated.metadata.model_dump(mode="json"),
                    "reused": True,
                }
        target.parent.mkdir(parents=True, exist_ok=True)
        SafeExtractor().extract(source, target)
        execution.check_cancelled()
        validated = validate_release_bundle(
            target,
            product=product,
            version=version,
        )
        return {
            "staged_path": str(validated.root),
            "application_root": str(validated.application_root),
            "python": str(validated.python),
            "bundle": validated.metadata.model_dump(mode="json"),
            "reused": False,
        }

    def _execute_stop_application_release(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        del task
        if execution.supervisor is None:
            raise UnsupportedTask("Application release activation requires the process supervisor.")
        snapshots = {service.id: service for service in execution.supervisor.snapshot()}
        services: dict[str, dict] = {}
        for service_id in PANDRATOR_SERVICE_STOP_ORDER:
            snapshot = snapshots.get(service_id)
            spec = execution.supervisor.spec(service_id)
            if snapshot is None and spec is None:
                continue
            services[service_id] = {
                "was_running": bool(snapshot is not None and snapshot.process is not None),
                "desired_running": bool(snapshot is not None and snapshot.desired_running),
                "spec": (spec.model_dump(mode="json") if spec is not None else None),
            }
            if snapshot is not None and (snapshot.process is not None or snapshot.desired_running):
                execution.supervisor.stop(service_id)
        return {"services": services}

    def _execute_reconcile_legacy_data(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        execution.check_cancelled()
        inventory = legacy_data_inventory(execution.context.layout)
        reviewed = task.inputs.get("inventory")
        if not isinstance(reviewed, dict) or inventory.as_dict() != reviewed:
            raise ManagerError(
                "legacy_data_changed",
                "Legacy data changed after the operation plan was reviewed.",
                {
                    "reviewed": reviewed if isinstance(reviewed, dict) else {},
                    "current": inventory.as_dict(),
                },
                409,
            )
        result = reconcile_legacy_data(
            execution.context.layout,
            inventory=inventory,
        )
        execution.check_cancelled()
        return result

    def _rollback_reconcile_legacy_data(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        del task
        rollback_legacy_data(execution.context.layout, result)

    def _rollback_stop_application_release(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        del task
        if execution.supervisor is None:
            return
        raw_services: object = result.get("services")
        services: dict[str, Any] = raw_services if isinstance(raw_services, dict) else {}
        for service_id in PANDRATOR_SERVICE_START_ORDER:
            previous = services.get(service_id)
            if not isinstance(previous, dict):
                continue
            if previous.get("was_running") or previous.get("desired_running"):
                execution.supervisor.start(service_id)

    def _execute_activate_application_release(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        if execution.supervisor is None:
            raise UnsupportedTask("Application release activation requires the process supervisor.")
        manifest_value = task.inputs.get("manifest")
        if not isinstance(manifest_value, dict):
            raise ManagerError(
                "invalid_release_operation",
                "The persisted release operation has no signed manifest.",
                http_status=500,
            )
        release = self._authority(execution).verify(
            manifest_value,
            expected_product="pandrator",
        )
        artifact = self._release_artifact(task.inputs.get("artifact"))
        if artifact.model_dump(mode="json") != release.artifact.model_dump(mode="json"):
            raise ManagerError(
                "release_plan_changed",
                "The activation artifact differs from the reviewed release plan.",
                http_status=409,
            )
        stage = self._prior(execution, "release:stage")
        staged = execution.context.layout.require_within(
            str(stage.get("staged_path") or ""),
            roots=(execution.context.layout.staging,),
        )
        database = execution.context.layout.data / "pandrator.sqlite3"
        slots = ReleaseSlotManager(
            execution.context.layout,
            execution.store,
        )
        journal = slots.prepare_activation(
            release.manifest,
            staged,
            operation_id=execution.operation.id,
            database=database,
        )
        destination = execution.context.layout.require_within(
            str(journal["destination"]),
            roots=(execution.context.layout.app_versions,),
        )
        validated = validate_release_bundle(
            destination,
            product="pandrator",
            version=release.manifest.payload.version,
        )
        new_specs = {
            spec.service_id: spec for spec in self._release_runtime_specs(execution.context.layout)
        }
        if not PANDRATOR_CORE_SERVICES.issubset(new_specs):
            raise RuntimeError("The activated application did not produce its required services.")
        for service_id in PANDRATOR_SERVICE_STOP_ORDER:
            if service_id not in new_specs and execution.supervisor.spec(service_id) is not None:
                execution.supervisor.unregister(service_id)
        for service_id in PANDRATOR_SERVICE_START_ORDER:
            if service_id in new_specs:
                execution.supervisor.replace_spec(new_specs[service_id])

        stopped = self._prior(
            execution,
            "release:stop-application",
        ).get("services")
        previous_services = stopped if isinstance(stopped, dict) else {}
        requested_running = bool(task.inputs.get("start_after_activation"))
        keep_worker = requested_running or any(
            bool(
                isinstance(value, dict)
                and (value.get("was_running") or value.get("desired_running"))
            )
            for key, value in previous_services.items()
            if key == PANDRATOR_WORKER_SERVICE
        )
        keep_api = (
            keep_worker
            or requested_running
            or any(
                bool(
                    isinstance(value, dict)
                    and (value.get("was_running") or value.get("desired_running"))
                )
                for key, value in previous_services.items()
                if key == PANDRATOR_API_SERVICE
            )
        )
        keep_mcp = PANDRATOR_MCP_SERVICE in new_specs and keep_api

        # Starting the new API both applies its idempotent database migrations
        # and proves the fixed service/protocol/version health contract.
        api = execution.supervisor.start(PANDRATOR_API_SERVICE)
        if keep_worker:
            execution.supervisor.start(PANDRATOR_WORKER_SERVICE)
        mcp_error = None
        if keep_mcp:
            try:
                execution.supervisor.start(PANDRATOR_MCP_SERVICE)
            except Exception as error:
                mcp_error = str(error) or "Pandrator MCP could not be started."
                execution.context.event_sink.emit(
                    "application.mcp_start_failed",
                    {"error": mcp_error, "action": "release-activate"},
                    component_id="pandrator",
                    operation_id=execution.operation.id,
                    service_id=PANDRATOR_MCP_SERVICE,
                )
        if not keep_api:
            execution.supervisor.stop(PANDRATOR_API_SERVICE)
        execution.check_cancelled()
        ownership = {
            "path": str(execution.context.layout.root / "app"),
            "owner_kind": "release",
            "owner_id": "pandrator",
            "evidence": {
                "operation_id": execution.operation.id,
                "version": release.manifest.payload.version,
                "sequence": release.manifest.payload.sequence,
                "manifest_digest": release.manifest.digest,
            },
        }
        release_activation = {
            "product": "pandrator",
            "channel": release.manifest.payload.channel,
            "version": release.manifest.payload.version,
            "sequence": release.manifest.payload.sequence,
            "manifest_digest": release.manifest.digest,
            "slot_path": str(validated.root),
            "envelope": release.envelope,
            "artifact": release.artifact.model_dump(mode="json"),
            "verified_key_ids": list(release.manifest.verified_key_ids),
        }
        return {
            "journal": journal,
            "active_path": str(validated.root),
            "api_health": (api.health.model_dump(mode="json") if api.health is not None else None),
            "kept_running": {
                PANDRATOR_API_SERVICE: keep_api,
                PANDRATOR_MCP_SERVICE: keep_mcp and mcp_error is None,
                PANDRATOR_WORKER_SERVICE: keep_worker,
            },
            "mcp_error": mcp_error,
            "ownership": ownership,
            "release_activation": release_activation,
        }

    def _rollback_activate_application_release(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        del task
        slots = ReleaseSlotManager(execution.context.layout, execution.store)
        journal = result.get("journal")
        if not isinstance(journal, dict):
            journal = None
        if (
            not journal
            and not slots.activation_journal(
                execution.operation.id,
                "pandrator",
            ).is_file()
        ):
            return
        supervisor = execution.supervisor
        if supervisor is not None:
            snapshots = {service.id: service for service in supervisor.snapshot()}
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                selected = snapshots.get(service_id)
                if selected is not None and (
                    selected.process is not None or selected.desired_running
                ):
                    supervisor.stop(service_id)
        slots.rollback_activation(
            operation_id=execution.operation.id,
            product="pandrator",
            result=journal,
        )
        if supervisor is None:
            return
        stopped_result = execution.prior_results.get(
            "release:stop-application",
            {},
        )
        raw_previous_services: object = stopped_result.get("services")
        previous_services: dict[str, Any] = (
            raw_previous_services if isinstance(raw_previous_services, dict) else {}
        )
        for service_id in PANDRATOR_SERVICE_STOP_ORDER:
            current = supervisor.spec(service_id)
            if current is not None:
                supervisor.unregister(service_id)
        for service_id in PANDRATOR_SERVICE_START_ORDER:
            previous = previous_services.get(service_id)
            serialized = previous.get("spec") if isinstance(previous, dict) else None
            if isinstance(serialized, dict):
                supervisor.register(ManagedProcessSpec.model_validate(serialized))

    def _execute_prepare_manager_handoff(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        manifest_value = task.inputs.get("manifest")
        if not isinstance(manifest_value, dict):
            raise ManagerError(
                "invalid_release_operation",
                "The persisted manager release has no signed manifest.",
                http_status=500,
            )
        release = self._authority(execution).verify(
            manifest_value,
            expected_product="pandrator-manager",
        )
        artifact = self._release_artifact(task.inputs.get("artifact"))
        if artifact.model_dump(mode="json") != release.artifact.model_dump(mode="json"):
            raise ManagerError(
                "release_plan_changed",
                "The manager handoff artifact differs from the reviewed plan.",
                http_status=409,
            )
        stage = self._prior(execution, "release:stage")
        staged = execution.context.layout.require_within(
            str(stage.get("staged_path") or ""),
            roots=(execution.context.layout.staging,),
        )
        return prepare_manager_handoff(
            layout=execution.context.layout,
            store=execution.store,
            operation_id=execution.operation.id,
            expected_revision=execution.plan.expected_revision,
            release=release,
            staged_directory=staged,
        )

    def _rollback_prepare_manager_handoff(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        del task
        rollback_prepared_manager_handoff(
            layout=execution.context.layout,
            operation_id=execution.operation.id,
            result=result,
        )
