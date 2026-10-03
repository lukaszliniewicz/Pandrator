"""Built-in typed task handlers for staged component slots and ownership."""

from __future__ import annotations

import hashlib as hashlib
import json
import os
import shutil
import sys as sys
import tempfile as tempfile
import zipfile as zipfile
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit as urlsplit

from dulwich import porcelain as porcelain
from dulwich.repo import Repo as Repo

from ..artifacts import ArtifactDownloader
from ..artifacts import ArtifactSpec as ArtifactSpec
from ..artifacts import SafeExtractor as SafeExtractor
from ..components.audiocpp import (
    AUDIO_CPP_MODEL_REVISION as AUDIO_CPP_MODEL_REVISION,
)
from ..components.audiocpp import (
    AUDIO_CPP_VERSION as AUDIO_CPP_VERSION,
)
from ..components.audiocpp import (
    AudioCppModelPackage as AudioCppModelPackage,
)
from ..components.audiocpp import (
    model_package as model_package,
)
from ..components.audiocpp import (
    server_config as server_config,
)
from ..components.audiocpp import (
    source_markers_for as source_markers_for,
)
from ..components.audiocpp_reuse import reuse_verified_package as reuse_verified_package
from ..components.crispasr import CRISPASR_VERSION as CRISPASR_VERSION
from ..components.runtime_bootstrap import generated_runtime_files
from ..components.slots import (
    active_component_path as active_component_path,
)
from ..components.slots import (
    component_container,
    component_pointer,
)
from ..context import WorkspaceLayout
from ..environments import PIXI_VERSION, PixiBootstrapper
from ..errors import ManagerError
from ..legacy_data import (
    legacy_data_inventory,
)
from ..legacy_data import (
    reconcile_legacy_data as reconcile_legacy_data,
)
from ..legacy_data import (
    rollback_legacy_data as rollback_legacy_data,
)
from ..models import (
    ManagedProcessSpec,
    TaskSpec,
)
from ..models import (
    PreflightCheck as PreflightCheck,
)
from ..network import load_network_configuration
from ..preflight import HostPreflight
from ..processes import CommandRunner
from ..processes import CommandSpec as CommandSpec
from ..releases.authority import ReleaseAuthority as ReleaseAuthority
from ..releases.bundles import (
    release_cache_path as release_cache_path,
)
from ..releases.bundles import (
    validate_release_bundle as validate_release_bundle,
)
from ..releases.handoff import (
    prepare_manager_handoff as prepare_manager_handoff,
)
from ..releases.handoff import (
    rollback_prepared_manager_handoff as rollback_prepared_manager_handoff,
)
from ..releases.models import ReleaseArtifact as ReleaseArtifact
from ..releases.slots import ReleaseSlotManager as ReleaseSlotManager
from ..runtime_specs import (
    PANDRATOR_API_SERVICE as PANDRATOR_API_SERVICE,
)
from ..runtime_specs import (
    PANDRATOR_CORE_SERVICES as PANDRATOR_CORE_SERVICES,
)
from ..runtime_specs import (
    PANDRATOR_MCP_SERVICE,
    PANDRATOR_SERVICE_STOP_ORDER,
    PANDRATOR_WORKER_SERVICE,
    pandrator_runtime_specs,
    runtime_python,
)
from ..runtime_specs import (
    PANDRATOR_SERVICE_START_ORDER as PANDRATOR_SERVICE_START_ORDER,
)
from ..state import ManagerStore
from ..tls import CABundleSelection as CABundleSelection
from ..tls import dulwich_config_with_ca as dulwich_config_with_ca
from ..tls import select_ca_bundle as select_ca_bundle
from ..uninstall import (
    prepare_uninstall_handoff,
    rollback_prepared_uninstall,
)
from .contracts import OperationTaskContext as OperationTaskContext
from .contracts import UnsupportedTask as UnsupportedTask
from .release_tasks import ReleaseTasks
from .source_errors import _is_tls_verification_error as _is_tls_verification_error
from .source_errors import _source_acquisition_error as _source_acquisition_error
from .source_tasks import ComponentSourceTasks as ComponentSourceTasks
from .staging_tasks import ComponentStagingTasks
from .task_files import _atomic_json as _atomic_json
from .task_files import _atomic_text as _atomic_text
from .uninstall_tasks import UninstallTasks


class FilesystemTaskHandler(ReleaseTasks, UninstallTasks, ComponentStagingTasks):
    """Executes only manager-generated task kinds; no raw API command exists."""

    @staticmethod
    def _release_runtime_specs(layout: WorkspaceLayout) -> tuple[ManagedProcessSpec, ...]:
        return pandrator_runtime_specs(layout)

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

    @staticmethod
    def _source_acquisition_error(
        *,
        error: Exception,
        label: str,
        repo_url: str,
        ca_bundle: CABundleSelection,
    ) -> ManagerError:
        return _source_acquisition_error(
            error=error,
            label=label,
            repo_url=repo_url,
            ca_bundle=ca_bundle,
        )

    @staticmethod
    def _runtime_python(layout: WorkspaceLayout) -> Path:
        return runtime_python(layout)

    supported_kinds = frozenset(
        {
            "stage_component",
            "stage_crispasr",
            "stage_audio_cpp",
            "verify_component",
            "activate_component",
            "validate_service",
            "stop_service",
            "remove_owned_component",
            "preflight_operation",
            "ensure_runtime_tool",
            "verify_release",
            "download_release",
            "stage_release",
            "stop_application_release",
            "reconcile_legacy_data",
            "activate_application_release",
            "prepare_manager_handoff",
            "start_application",
            "stop_all_services",
            "export_uninstall_data",
            "prepare_uninstall_handoff",
        }
    )

    def execute(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        if task.kind not in self.supported_kinds:
            raise UnsupportedTask(f"Unsupported operation task kind: {task.kind}")
        return getattr(self, f"_execute_{task.kind}")(execution, task)

    def rollback(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        method = getattr(self, f"_rollback_{task.kind}", None)
        if method is not None:
            method(execution, task, result)

    @staticmethod
    def _prepare_runtime_adapter(target: Path, component_id: str) -> None:
        for relative, content in generated_runtime_files(component_id).items():
            _atomic_text(target / relative, content)

    def _execute_preflight_operation(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        del task
        checks = HostPreflight(
            execution.context,
            execution.registry,
        ).evaluate(
            desired=execution.plan.desired,
            tasks=execution.plan.tasks,
        )
        HostPreflight.require_success(checks)
        return {"checks": [check.model_dump(mode="json") for check in checks]}

    def _execute_ensure_runtime_tool(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        tool = str(task.inputs.get("tool") or "")
        version = str(task.inputs.get("version") or "")
        if tool != "pixi" or version != PIXI_VERSION:
            raise UnsupportedTask(f"Unsupported runtime tool requirement: {tool} {version}")
        bootstrapper = PixiBootstrapper(
            execution.context,
            runner=CommandRunner(
                cancellation=execution.cancellation,
                base_environment=execution.context.environment,
            ),
            downloader=ArtifactDownloader(
                cancellation=execution.cancellation,
                environment=execution.context.environment,
            ),
        )
        target = bootstrapper.target.resolve(strict=False)
        manager_owned = any(
            record["owner_kind"] == "runtime_tool"
            and record["owner_id"] == "pixi"
            and Path(record["path"]).resolve(strict=False) == target
            for record in execution.store.owned_paths()
        )
        return bootstrapper.ensure(
            execution.context.layout.staging / execution.operation.id,
            execution.context.layout.backups / execution.operation.id,
            replace_existing=manager_owned,
            offline=any(
                bool(state.options.get("offline")) for state in execution.plan.desired.values()
            ),
        )

    def _rollback_ensure_runtime_tool(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        del result
        if str(task.inputs.get("tool") or "") != "pixi":
            return
        PixiBootstrapper(execution.context).rollback(
            execution.context.layout.staging / execution.operation.id
        )

    def _execute_verify_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        definition = self._definition(execution, task)
        stage = self._stage_result(execution, definition.id)
        root = Path(stage["staged_path"]).resolve(strict=False)
        execution.context.layout.require_within(
            root,
            roots=(execution.context.layout.staging,),
        )
        source_markers = self._source_markers(execution, definition)
        missing = [marker for marker in source_markers if not (root / marker).exists()]
        if missing:
            raise RuntimeError(
                f"{definition.label} staging is incomplete; missing " + ", ".join(missing)
            )
        return {
            "verified_path": str(root),
            "markers": list(source_markers),
            "revision": stage["revision"],
        }

    def _execute_activate_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        execution.check_cancelled()
        definition = self._definition(execution, task)
        stage = self._stage_result(execution, definition.id)
        staged = Path(stage["staged_path"]).resolve(strict=False)
        execution.context.layout.require_within(
            staged,
            roots=(execution.context.layout.staging,),
        )
        revision = str(stage["revision"] or execution.operation.id)
        safe_revision = (
            "".join(
                character for character in revision if character.isalnum() or character in "._-"
            )[:80]
            or execution.operation.id
        )
        container = component_container(execution.context.layout, definition.id)
        versions = container / "versions"
        destination = versions / safe_revision
        versions.mkdir(parents=True, exist_ok=True)
        execution.context.layout.require_within(
            destination,
            roots=(versions,),
        )
        pointer = component_pointer(execution.context.layout, definition.id)
        activation_journal = staged.parent / "activation.json"
        execution.context.layout.require_within(
            activation_journal,
            roots=(execution.context.layout.staging,),
        )
        if activation_journal.is_file():
            try:
                journal = json.loads(activation_journal.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(f"{definition.label} activation journal is invalid.") from error
            if (
                not isinstance(journal, dict)
                or journal.get("component_id") != definition.id
                or journal.get("destination") != str(destination)
            ):
                raise RuntimeError(
                    f"{definition.label} activation journal does not match the planned slot."
                )
            previous_pointer = journal.get("previous_pointer")
            created_slot = bool(journal.get("created_slot"))
        else:
            try:
                previous_pointer = json.loads(pointer.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                previous_pointer = None
            created_slot = not destination.is_dir()
            _atomic_json(
                activation_journal,
                {
                    "component_id": definition.id,
                    "destination": str(destination),
                    "created_slot": created_slot,
                    "previous_pointer": previous_pointer,
                },
            )
        source_markers = self._source_markers(execution, definition)
        if destination.is_dir():
            if not self._markers_present(
                destination,
                source_markers,
            ):
                raise RuntimeError(
                    f"Existing {definition.label} slot {safe_revision} is incomplete."
                )
            if staged.exists():
                shutil.rmtree(staged)
        else:
            os.replace(staged, destination)
        pointer_payload = {
            "component_id": definition.id,
            "version": safe_revision,
            "path": str(destination),
            "activated_by": execution.operation.id,
        }
        _atomic_json(pointer, pointer_payload)
        ownership = {
            "path": str(container),
            "owner_kind": "component",
            "owner_id": definition.id,
            "evidence": {
                "operation_id": execution.operation.id,
                "revision": safe_revision,
                "markers": list(source_markers),
            },
        }
        return {
            "pointer": str(pointer),
            "active_path": str(destination),
            "revision": safe_revision,
            "created_slot": created_slot,
            "previous_pointer": previous_pointer,
            "ownership": ownership,
        }

    def _rollback_activate_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        definition = self._definition(execution, task)
        if not result:
            journal_path = (
                execution.context.layout.staging
                / execution.operation.id
                / definition.id
                / "activation.json"
            )
            execution.context.layout.require_within(
                journal_path,
                roots=(execution.context.layout.staging,),
            )
            if not journal_path.is_file():
                return
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"{definition.label} activation rollback journal is invalid."
                ) from error
            if (
                not isinstance(journal, dict)
                or journal.get("component_id") != definition.id
                or not isinstance(journal.get("destination"), str)
            ):
                raise RuntimeError(f"{definition.label} activation rollback journal is invalid.")
            result = {
                "active_path": journal.get("destination"),
                "created_slot": bool(journal.get("created_slot")),
                "previous_pointer": journal.get("previous_pointer"),
            }
        removal_guard = nullcontext()
        if definition.service_key and execution.supervisor is not None:
            removal_guard = execution.supervisor.component_slot_removal_guard(definition.id)
        with removal_guard:
            pointer = component_pointer(execution.context.layout, definition.id)
            previous = result.get("previous_pointer")
            if isinstance(previous, dict):
                _atomic_json(pointer, previous)
            else:
                try:
                    pointer.unlink()
                except FileNotFoundError:
                    pass
            if result.get("created_slot"):
                active = Path(str(result.get("active_path") or ""))
                if active.exists():
                    container = component_container(
                        execution.context.layout,
                        definition.id,
                    )
                    execution.context.layout.require_within(
                        active,
                        roots=(container / "versions",),
                    )
                    shutil.rmtree(active)

    def _execute_validate_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        definition = self._definition(execution, task)
        if execution.supervisor is None or execution.service_spec_factory is None:
            raise UnsupportedTask(f"{definition.label} service validation is unavailable.")
        desired = execution.plan.desired[definition.id]
        resolved = execution.registry.driver(definition.id).resolve(
            execution.context,
            definition,
            desired,
        )
        spec = execution.service_spec_factory(definition.id, resolved)
        if spec is None:
            raise UnsupportedTask(f"{definition.label} has no managed runtime specification.")
        previous_spec = execution.supervisor.replace_spec(spec)
        try:
            service = execution.supervisor.start(spec.service_id)
        except Exception:
            if previous_spec is not None:
                execution.supervisor.replace_spec(previous_spec)
            else:
                execution.supervisor.unregister(spec.service_id)
            raise
        stop_result = execution.prior_results.get(f"{definition.id}:stop", {})
        keep_running = bool(
            desired.options.get("start_after_install", False)
            or stop_result.get("was_running", False)
            or stop_result.get("desired_running", False)
        )
        if not keep_running:
            execution.supervisor.stop(spec.service_id)
        return {
            "service_id": spec.service_id,
            "health": service.health.model_dump(mode="json") if service.health else None,
            "kept_running": keep_running,
            "previous_spec": (
                previous_spec.model_dump(mode="json") if previous_spec is not None else None
            ),
        }

    def _rollback_validate_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        if execution.supervisor is None or not result.get("service_id"):
            return
        service_id = str(result["service_id"])
        if result.get("kept_running"):
            execution.supervisor.stop(service_id)
        previous = result.get("previous_spec")
        if isinstance(previous, dict):
            execution.supervisor.replace_spec(ManagedProcessSpec.model_validate(previous))
        else:
            execution.supervisor.unregister(service_id)

    def _execute_start_application(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        if execution.supervisor is None:
            return {
                "started": False,
                "error": "The process supervisor is unavailable.",
            }
        preferences: dict[str, str] = {}
        crispasr = execution.plan.desired.get("crispasr")
        if crispasr is not None and crispasr.present:
            engine = str(crispasr.options.get("engine") or "").strip()
            quantization = str(
                crispasr.quantization or crispasr.options.get("quantization") or ""
            ).strip()
            if engine:
                preferences["CRISPASR_DEFAULT_ENGINE"] = engine
            if quantization:
                preferences["CRISPASR_DEFAULT_QUANTIZATION"] = quantization
        try:
            exposure = load_network_configuration(
                execution.context.layout,
                environment=execution.context.environment,
            ).application
            specifications = pandrator_runtime_specs(
                execution.context.layout,
                exposure=exposure,
                preferences=preferences,
            )
            running = {
                service.id
                for service in execution.supervisor.snapshot()
                if service.process is not None
            }
            # A component update activates a new application slot before this
            # task refreshes the launch contracts. ProcessSupervisor correctly
            # refuses to replace a running contract, so quiesce every
            # application-owned service first.
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                if service_id in running:
                    execution.supervisor.stop(service_id)
            selected_ids = {specification.service_id for specification in specifications}
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                if (
                    service_id not in selected_ids
                    and execution.supervisor.spec(service_id) is not None
                ):
                    execution.supervisor.unregister(service_id)
            for specification in specifications:
                execution.supervisor.replace_spec(specification)
            service = execution.supervisor.start(PANDRATOR_WORKER_SERVICE)
            mcp_error = None
            if PANDRATOR_MCP_SERVICE in selected_ids:
                try:
                    execution.supervisor.start(PANDRATOR_MCP_SERVICE)
                except Exception as error:
                    mcp_error = str(error) or "Pandrator MCP could not be started."
                    execution.context.event_sink.emit(
                        "application.mcp_start_failed",
                        {"error": mcp_error, "action": "install"},
                        component_id="pandrator",
                        operation_id=execution.operation.id,
                        service_id=PANDRATOR_MCP_SERVICE,
                    )
        except Exception as error:
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                try:
                    if execution.supervisor.spec(service_id) is not None:
                        execution.supervisor.stop(service_id)
                except Exception:
                    pass
            execution.context.event_sink.emit(
                "application.autostart_failed",
                {"error": str(error)},
                component_id="pandrator",
                operation_id=execution.operation.id,
            )
            return {"started": False, "error": str(error)}
        execution.context.event_sink.emit(
            "application.started",
            {"action": "install"},
            component_id="pandrator",
            operation_id=execution.operation.id,
        )
        return {
            "started": True,
            "service_id": service.id,
            "health": (
                service.health.model_dump(mode="json") if service.health is not None else None
            ),
            "mcp_error": mcp_error,
        }

    def _rollback_start_application(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        if execution.supervisor is None or not result.get("started"):
            return
        for service_id in PANDRATOR_SERVICE_STOP_ORDER:
            if execution.supervisor.spec(service_id) is not None:
                execution.supervisor.stop(service_id)

    def _execute_stop_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        definition = self._definition(execution, task)
        if not definition.service_key or execution.supervisor is None:
            return {
                "service_id": None,
                "was_running": False,
                "desired_running": False,
            }
        snapshots = {service.id: service for service in execution.supervisor.snapshot()}
        previous = snapshots.get(definition.service_key)
        was_running = bool(previous is not None and previous.process is not None)
        desired_running = bool(previous is not None and previous.desired_running)
        if previous is not None:
            execution.supervisor.stop(definition.service_key)
        return {
            "service_id": definition.service_key,
            "was_running": was_running,
            "desired_running": desired_running,
        }

    def _rollback_stop_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        if (
            (result.get("was_running") or result.get("desired_running"))
            and result.get("service_id")
            and execution.supervisor is not None
        ):
            execution.supervisor.start(str(result["service_id"]))

    def _execute_remove_owned_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        definition = self._definition(execution, task)
        ownership_records = [
            record
            for record in execution.store.owned_paths()
            if record["owner_id"] == definition.id
            and record["owner_kind"] in {"component", "legacy_component"}
        ]
        owned = [Path(record["path"]) for record in ownership_records]
        container = component_container(execution.context.layout, definition.id)
        if container.exists() and container not in owned:
            owned.append(container)
        if not owned:
            raise RuntimeError(
                f"Refusing to remove {definition.label}: no positive ownership "
                "manifest is available. Import or repair the legacy installation first."
            )
        legacy_roots = [
            Path(record["path"]).expanduser().resolve(strict=False)
            for record in ownership_records
            if record["owner_kind"] == "legacy_component"
        ]
        embedded_data = [
            str(item.source)
            for item in legacy_data_inventory(execution.context.layout).items
            if any(
                item.source.resolve(strict=False) == root
                or execution.context.layout.contains(root, item.source)
                for root in legacy_roots
            )
        ]
        if embedded_data:
            raise ManagerError(
                "legacy_data_reconciliation_required",
                "This legacy component still contains mutable data. Use a "
                "reviewed application migration or whole-product uninstall "
                "so the manager can preserve it before removing the source.",
                {
                    "component_id": definition.id,
                    "paths": embedded_data,
                },
                409,
            )
        # A component-container ownership record supersedes any child records.
        # Moving both would make execution order-dependent and break rollback.
        canonical_owned = sorted(
            {path.expanduser().resolve(strict=False) for path in owned},
            key=lambda path: (len(path.parts), str(path).casefold()),
        )
        owned = [
            candidate
            for index, candidate in enumerate(canonical_owned)
            if not any(
                execution.context.layout.contains(parent, candidate) and parent != candidate
                for parent in canonical_owned[:index]
            )
        ]
        backup_root = execution.context.layout.backups / execution.operation.id / definition.id
        moved: list[dict[str, str]] = []
        for index, source in enumerate(owned):
            destination = backup_root / f"{index}-{source.name}"
            execution.context.layout.require_within(
                destination,
                roots=(execution.context.layout.backups,),
            )
            if destination.exists():
                if source.exists():
                    raise RuntimeError(
                        f"Both the owned path and its operation backup exist: {source}"
                    )
                moved.append({"source": str(source), "backup": str(destination)})
                continue
            if not source.exists():
                continue
            execution.context.layout.require_within(
                source,
                roots=(execution.context.layout.root,),
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append({"source": str(source), "backup": str(destination)})
        return {"moved": moved, "backup_root": str(backup_root)}

    def _rollback_remove_owned_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        definition = self._definition(execution, task)
        moved = list(result.get("moved") or [])
        if not moved:
            owned = [
                Path(record["path"]).expanduser().resolve(strict=False)
                for record in execution.store.owned_paths()
                if record["owner_id"] == definition.id
                and record["owner_kind"] in {"component", "legacy_component"}
            ]
            container = component_container(
                execution.context.layout,
                definition.id,
            )
            if container not in owned:
                owned.append(container)
            canonical_owned = sorted(
                set(owned),
                key=lambda path: (len(path.parts), str(path).casefold()),
            )
            owned = [
                candidate
                for index, candidate in enumerate(canonical_owned)
                if not any(
                    execution.context.layout.contains(parent, candidate) and parent != candidate
                    for parent in canonical_owned[:index]
                )
            ]
            backup_root = execution.context.layout.backups / execution.operation.id / definition.id
            moved = [
                {
                    "source": str(source),
                    "backup": str(backup_root / f"{index}-{source.name}"),
                }
                for index, source in enumerate(owned)
                if (backup_root / f"{index}-{source.name}").exists()
            ]
        for record in reversed(moved):
            source = Path(record["source"])
            backup = Path(record["backup"])
            if not backup.exists():
                continue
            execution.context.layout.require_within(
                backup,
                roots=(execution.context.layout.backups,),
            )
            execution.context.layout.require_within(
                source,
                roots=(execution.context.layout.root,),
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, source)

    def finalize(
        self,
        execution: OperationTaskContext,
        *,
        succeeded: bool,
    ) -> None:
        operation_staging = execution.context.layout.staging / execution.operation.id
        if operation_staging.exists():
            execution.context.layout.require_within(
                operation_staging,
                roots=(execution.context.layout.staging,),
            )
            shutil.rmtree(operation_staging)
        if succeeded:
            backup = execution.context.layout.backups / execution.operation.id
            if backup.exists():
                execution.context.layout.require_within(
                    backup,
                    roots=(execution.context.layout.backups,),
                )
                shutil.rmtree(backup)
