"""Built-in typed task handlers for staged component slots and ownership."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from dulwich import porcelain
from dulwich.repo import Repo

from ..artifacts import ArtifactDownloader, ArtifactSpec, SafeExtractor
from ..components import ComponentRegistry
from ..components.crispasr import CRISPASR_VERSION
from ..components.runtime_bootstrap import generated_runtime_files
from ..components.slots import (
    component_container,
    component_pointer,
)
from ..context import ManagerContext
from ..environments import PIXI_VERSION, PixiBootstrapper
from ..errors import ManagerError
from ..legacy_data import (
    legacy_data_inventory,
    reconcile_legacy_data,
    rollback_legacy_data,
)
from ..models import (
    ManagedProcessSpec,
    OperationPlan,
    OperationRecord,
    PreflightCheck,
    TaskSpec,
)
from ..network import load_network_configuration
from ..preflight import HostPreflight
from ..processes import CommandRunner, CommandSpec
from ..releases.authority import ReleaseAuthority
from ..releases.bundles import (
    release_cache_path,
    validate_release_bundle,
)
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
from ..state import ManagerStore
from ..supervisor import ProcessSupervisor
from ..tls import CABundleSelection, dulwich_config_with_ca
from ..uninstall import (
    prepare_uninstall_handoff,
    rollback_prepared_uninstall,
)


class UnsupportedTask(RuntimeError):
    pass


def _is_tls_verification_error(error: BaseException) -> bool:
    inspected: set[int] = set()
    pending: list[BaseException] = [error]
    while pending and len(inspected) < 16:
        selected = pending.pop()
        if id(selected) in inspected:
            continue
        inspected.add(id(selected))
        message = str(selected).casefold()
        if any(
            marker in message
            for marker in (
                "certificate_verify_failed",
                "certificate verify failed",
                "unable to get local issuer certificate",
                "self-signed certificate",
                "hostname mismatch",
            )
        ):
            return True
        for nested in (selected.__cause__, selected.__context__):
            if isinstance(nested, BaseException):
                pending.append(nested)
        pending.extend(
            item for item in selected.args if isinstance(item, BaseException)
        )
    return False


def _source_acquisition_error(
    *,
    error: Exception,
    label: str,
    repo_url: str,
    ca_bundle: CABundleSelection,
) -> ManagerError:
    host = str(urlsplit(repo_url).hostname or "the source host")
    details = {
        "host": host,
        "ca_bundle_source": ca_bundle.source,
        "error_type": type(error).__name__,
    }
    if _is_tls_verification_error(error):
        return ManagerError(
            "source_tls_verification_failed",
            f"Pandrator could not verify the TLS certificate while downloading "
            f"{label} from {host}. Check the computer's date and time and any "
            "HTTPS-inspecting proxy, then download the diagnostic bundle if "
            "the problem continues.",
            details,
            502,
        )
    return ManagerError(
        "source_download_failed",
        f"Pandrator could not download {label} from {host}. Check the internet "
        "and proxy connection, then download the diagnostic bundle if the "
        "problem continues.",
        details,
        502,
    )


@dataclass(slots=True)
class OperationTaskContext:
    context: ManagerContext
    store: ManagerStore
    registry: ComponentRegistry
    supervisor: ProcessSupervisor | None
    operation: OperationRecord
    plan: OperationPlan
    prior_results: dict[str, dict]
    cancellation: object
    release_authority: ReleaseAuthority | None = None
    service_spec_factory: (
        Callable[[str, object], ManagedProcessSpec | None] | None
    ) = None

    def check_cancelled(self) -> None:
        self.cancellation.raise_if_requested()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt" and content.startswith("#!"):
            path.chmod(path.stat().st_mode | 0o755)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class FilesystemTaskHandler:
    """Executes only manager-generated task kinds; no raw API command exists."""

    supported_kinds = frozenset(
        {
            "stage_component",
            "stage_crispasr",
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
    def _definition(execution: OperationTaskContext, task: TaskSpec):
        if not task.component_id:
            raise ValueError(f"Task {task.id} has no component owner.")
        return execution.registry.definition(task.component_id)

    def _staging_source(
        self,
        execution: OperationTaskContext,
        component_id: str,
    ) -> Path:
        target = (
            execution.context.layout.staging
            / execution.operation.id
            / component_id
            / "source"
        )
        return execution.context.layout.require_within(
            target,
            roots=(execution.context.layout.staging,),
        )

    def _execute_stage_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        execution.check_cancelled()
        definition = self._definition(execution, task)
        if not definition.repo_url:
            raise UnsupportedTask(
                f"{definition.label} has no signed or repository-backed "
                "installation source in this manager release."
            )
        target = self._staging_source(execution, definition.id)
        if target.is_dir():
            self._prepare_runtime_adapter(target, definition.id)
        if target.is_dir() and self._markers_present(
            target,
            definition.source_markers,
        ):
            self._ensure_source_revision(target, definition)
            return {
                "staged_path": str(target),
                "revision": self._revision(target),
                "reused": True,
            }
        if target.exists():
            execution.context.layout.require_within(
                target,
                roots=(execution.context.layout.staging,),
            )
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        git_config, ca_bundle = dulwich_config_with_ca(
            execution.context.environment
        )
        try:
            cloned = porcelain.clone(
                definition.repo_url,
                str(target),
                checkout=True,
                config=git_config,
            )
        except Exception as error:
            raise _source_acquisition_error(
                error=error,
                label=definition.label,
                repo_url=definition.repo_url,
                ca_bundle=ca_bundle,
            ) from error
        # Dulwich can retain pack files on Windows. Close before the
        # path-based revision reset so a failed checkout cannot leak handles.
        try:
            cloned.close()
        except Exception as error:
            raise RuntimeError(
                f"{definition.label} source repository could not be closed."
            ) from error
        if definition.source_revision:
            self._ensure_source_revision(target, definition)
        self._prepare_runtime_adapter(target, definition.id)
        execution.check_cancelled()
        return {
            "staged_path": str(target),
            "revision": self._revision(target),
            "reused": False,
        }

    @staticmethod
    def _prepare_runtime_adapter(target: Path, component_id: str) -> None:
        for relative, content in generated_runtime_files(component_id).items():
            _atomic_text(target / relative, content)

    @classmethod
    def _ensure_source_revision(cls, target: Path, definition) -> None:
        """Ensure a reusable Git source tree is at its requested revision."""

        requested = definition.source_revision
        if not requested:
            return
        if cls._revision(target).casefold().startswith(requested.casefold()):
            return
        try:
            porcelain.reset(
                str(target),
                mode="hard",
                treeish=requested,
            )
        except Exception as error:
            raise RuntimeError(
                f"{definition.label} source revision "
                f"{requested} could not be checked out."
            ) from error
        selected = cls._revision(target)
        if not selected.casefold().startswith(requested.casefold()):
            raise RuntimeError(
                f"{definition.label} source revision {requested} was not "
                f"checked out (selected {selected})."
            )

    def _execute_stage_crispasr(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        """Acquire, extract, and probe the exact pinned native runtime."""

        execution.check_cancelled()
        definition = self._definition(execution, task)
        raw_asset = task.inputs.get("asset")
        if not isinstance(raw_asset, dict):
            raise UnsupportedTask("The CrispASR asset contract is missing.")
        try:
            filename = str(raw_asset["filename"])
            specification = ArtifactSpec(
                url=str(raw_asset["url"]),
                sha256=str(raw_asset["sha256"]),
                filename=filename,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise UnsupportedTask(
                "The CrispASR asset contract is invalid."
            ) from error
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or Path(filename).name != filename
        ):
            raise UnsupportedTask(
                "The CrispASR asset filename is not a safe cache basename."
            )

        target = self._staging_source(execution, definition.id)
        executable_name = "crispasr.exe" if os.name == "nt" else "crispasr"
        existing_executable = target / executable_name
        if (
            target.is_dir()
            and (target / "install.json").is_file()
            and existing_executable.is_file()
        ):
            return {
                "staged_path": str(target),
                "revision": (
                    f"crispasr-{CRISPASR_VERSION}-"
                    f"{task.inputs.get('effective_compute') or 'cpu'}"
                ),
                "reused": True,
            }

        staging_root = target.parent
        execution.context.layout.require_within(
            staging_root,
            roots=(execution.context.layout.staging,),
        )
        if staging_root.exists():
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True, exist_ok=True)

        cache_path = (
            execution.context.layout.cache
            / "artifacts"
            / "crispasr"
            / filename
        )
        selected = ArtifactDownloader(
            cancellation=execution.cancellation,
            environment=execution.context.environment,
        ).download(specification, cache_path)
        unpacked = staging_root / "unpacked"
        SafeExtractor().extract(selected, unpacked)
        executable = next(
            (
                candidate
                for candidate in unpacked.rglob(executable_name)
                if candidate.is_file()
            ),
            None,
        )
        if executable is None:
            raise RuntimeError(
                f"The verified CrispASR archive did not contain {executable_name}."
            )
        shutil.copytree(executable.parent, target)
        staged_executable = target / executable_name
        if os.name != "nt":
            staged_executable.chmod(staged_executable.stat().st_mode | 0o755)
        probe = CommandRunner(
            cancellation=execution.cancellation,
            base_environment=execution.context.environment,
        ).run(
            CommandSpec(
                argv=(str(staged_executable), "--version"),
                cwd=target,
                timeout_seconds=30,
                label="crispasr-version",
            )
        )
        version_output = "\n".join((probe.stdout, probe.stderr))
        if f"version       : {CRISPASR_VERSION}" not in version_output:
            raise RuntimeError(
                "The verified CrispASR binary reported an unexpected version."
            )
        _atomic_json(
            target / "install.json",
            {
                "version": CRISPASR_VERSION,
                "requested_backend": str(
                    task.inputs.get("requested_compute") or "auto"
                ),
                "effective_backend": str(
                    task.inputs.get("effective_compute") or "cpu"
                ),
                "runtime_variant": str(
                    raw_asset.get("runtime_variant") or "cpu"
                ),
                "compiled_backends": list(
                    raw_asset.get("compiled_backends") or ()
                ),
                "asset": filename,
                "sha256": specification.sha256,
                "default_model": (
                    (task.inputs.get("resolved") or {})
                    .get("options", {})
                    .get("engine", "moss-transcribe-diarize-0.9b")
                ),
                "default_quantization": (
                    (task.inputs.get("resolved") or {}).get("quantization")
                    or "q8_0"
                ),
            },
        )
        shutil.rmtree(unpacked)
        execution.check_cancelled()
        return {
            "staged_path": str(target),
            "revision": (
                f"crispasr-{CRISPASR_VERSION}-"
                f"{task.inputs.get('effective_compute') or 'cpu'}"
            ),
            "reused": False,
            "asset_path": str(selected),
        }

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
        return {
            "checks": [
                check.model_dump(mode="json")
                for check in checks
            ]
        }

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
            raise RuntimeError(
                f"Missing successful prerequisite result: {task_id}"
            ) from None

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
        planned_artifact = self._release_artifact(
            task.inputs.get("artifact")
        )
        if (
            planned_artifact.model_dump(mode="json")
            != release.artifact.model_dump(mode="json")
        ):
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
            "verified_key_ids": list(
                release.manifest.verified_key_ids
            ),
            "artifact": release.artifact.model_dump(mode="json"),
            "checks": [
                check.model_dump(mode="json")
                for check in selected_checks
            ],
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
        ).download(
            specification,
            destination,
            offline=bool(task.inputs.get("offline")),
        )
        return {
            "artifact_path": str(selected),
            "artifact": artifact.model_dump(mode="json"),
            "cache_reused": selected.is_file(),
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
        target = (
            execution.context.layout.staging
            / execution.operation.id
            / "release"
            / "bundle"
        )
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
            raise UnsupportedTask(
                "Application release activation requires the process supervisor."
            )
        snapshots = {
            service.id: service
            for service in execution.supervisor.snapshot()
        }
        services: dict[str, dict] = {}
        for service_id in PANDRATOR_SERVICE_STOP_ORDER:
            snapshot = snapshots.get(service_id)
            spec = execution.supervisor.spec(service_id)
            if snapshot is None and spec is None:
                continue
            services[service_id] = {
                "was_running": bool(
                    snapshot is not None and snapshot.process is not None
                ),
                "desired_running": bool(
                    snapshot is not None and snapshot.desired_running
                ),
                "spec": (
                    spec.model_dump(mode="json")
                    if spec is not None
                    else None
                ),
            }
            if snapshot is not None and (
                snapshot.process is not None
                or snapshot.desired_running
            ):
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
            raise UnsupportedTask(
                "Application release activation requires the process supervisor."
            )
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
        if (
            artifact.model_dump(mode="json")
            != release.artifact.model_dump(mode="json")
        ):
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
            spec.service_id: spec
            for spec in pandrator_runtime_specs(execution.context.layout)
        }
        if not PANDRATOR_CORE_SERVICES.issubset(new_specs):
            raise RuntimeError(
                "The activated application did not produce its required services."
            )
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
        requested_running = bool(
            task.inputs.get("start_after_activation")
        )
        keep_worker = requested_running or any(
            bool(
                isinstance(value, dict)
                and (
                    value.get("was_running")
                    or value.get("desired_running")
                )
            )
            for key, value in previous_services.items()
            if key == PANDRATOR_WORKER_SERVICE
        )
        keep_api = keep_worker or requested_running or any(
            bool(
                isinstance(value, dict)
                and (
                    value.get("was_running")
                    or value.get("desired_running")
                )
            )
            for key, value in previous_services.items()
            if key == PANDRATOR_API_SERVICE
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
            "verified_key_ids": list(
                release.manifest.verified_key_ids
            ),
        }
        return {
            "journal": journal,
            "active_path": str(validated.root),
            "api_health": (
                api.health.model_dump(mode="json")
                if api.health is not None
                else None
            ),
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
        supervisor = execution.supervisor
        if supervisor is not None:
            snapshots = {
                service.id: service for service in supervisor.snapshot()
            }
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                selected = snapshots.get(service_id)
                if selected is not None and (
                    selected.process is not None
                    or selected.desired_running
                ):
                    supervisor.stop(service_id)
        ReleaseSlotManager(
            execution.context.layout,
            execution.store,
        ).rollback_activation(
            operation_id=execution.operation.id,
            product="pandrator",
            result=(
                result.get("journal")
                if isinstance(result.get("journal"), dict)
                else None
            ),
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
            serialized = (
                previous.get("spec")
                if isinstance(previous, dict)
                else None
            )
            if isinstance(serialized, dict):
                supervisor.register(
                    ManagedProcessSpec.model_validate(serialized)
                )

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
        if (
            artifact.model_dump(mode="json")
            != release.artifact.model_dump(mode="json")
        ):
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
        before = {
            service.id: service.model_dump(mode="json")
            for service in supervisor.snapshot()
        }
        stopped = supervisor.stop_all()
        after = {service.id: service for service in supervisor.snapshot()}
        still_live = [
            service_id
            for service_id, service in after.items()
            if service.process is not None
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
                (
                    service
                    for service in supervisor.snapshot()
                    if service.id == service_id
                ),
                None,
            )
            if current is None or current.process is None:
                supervisor.start(service_id)

    @staticmethod
    def _link_like(path: Path) -> bool:
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(
            junction is not None and junction()
        )

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
                    raise RuntimeError(
                        f"Export verification failed at {bad_member}."
                    )
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
        if (
            isinstance(impact, dict)
            and impact.get("export_data") == str(destination)
        ):
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
        if (
            purge_data != bool(impact.get("purge_data"))
            or export_data != impact.get("export_data")
        ):
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
        return prepare_uninstall_handoff(
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
        rollback_prepared_uninstall(
            layout=execution.context.layout,
            operation_id=execution.operation.id,
        )

    def _execute_ensure_runtime_tool(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        tool = str(task.inputs.get("tool") or "")
        version = str(task.inputs.get("version") or "")
        if tool != "pixi" or version != PIXI_VERSION:
            raise UnsupportedTask(
                f"Unsupported runtime tool requirement: {tool} {version}"
            )
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
                bool(state.options.get("offline"))
                for state in execution.plan.desired.values()
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

    @staticmethod
    def _revision(repository: Path) -> str:
        try:
            with Repo(str(repository)) as selected:
                return selected.head().decode("ascii")
        except Exception:
            return "unversioned"

    @staticmethod
    def _markers_present(root: Path, markers: tuple[str, ...]) -> bool:
        return bool(markers) and all((root / marker).exists() for marker in markers)

    def _stage_result(
        self,
        execution: OperationTaskContext,
        component_id: str,
    ) -> dict:
        key = f"{component_id}:stage"
        try:
            return execution.prior_results[key]
        except KeyError:
            raise RuntimeError(f"Missing staged result for {component_id}.") from None

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
        missing = [
            marker
            for marker in definition.source_markers
            if not (root / marker).exists()
        ]
        if missing:
            raise RuntimeError(
                f"{definition.label} staging is incomplete; missing "
                + ", ".join(missing)
            )
        return {
            "verified_path": str(root),
            "markers": list(definition.source_markers),
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
        safe_revision = "".join(
            character
            for character in revision
            if character.isalnum() or character in "._-"
        )[:80] or execution.operation.id
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
                journal = json.loads(
                    activation_journal.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"{definition.label} activation journal is invalid."
                ) from error
            if (
                not isinstance(journal, dict)
                or journal.get("component_id") != definition.id
                or journal.get("destination") != str(destination)
            ):
                raise RuntimeError(
                    f"{definition.label} activation journal does not match "
                    "the planned slot."
                )
            previous_pointer = journal.get("previous_pointer")
            created_slot = bool(journal.get("created_slot"))
        else:
            try:
                previous_pointer = json.loads(
                    pointer.read_text(encoding="utf-8")
                )
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
        if destination.is_dir():
            if not self._markers_present(destination, definition.source_markers):
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
                "markers": list(definition.source_markers),
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
                journal = json.loads(
                    journal_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"{definition.label} activation rollback journal is invalid."
                ) from error
            if (
                not isinstance(journal, dict)
                or journal.get("component_id") != definition.id
                or not isinstance(journal.get("destination"), str)
            ):
                raise RuntimeError(
                    f"{definition.label} activation rollback journal is invalid."
                )
            result = {
                "active_path": journal.get("destination"),
                "created_slot": bool(journal.get("created_slot")),
                "previous_pointer": journal.get("previous_pointer"),
            }
        removal_guard = nullcontext()
        if definition.service_key and execution.supervisor is not None:
            removal_guard = execution.supervisor.component_slot_removal_guard(
                definition.id
            )
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
            raise UnsupportedTask(
                f"{definition.label} service validation is unavailable."
            )
        desired = execution.plan.desired[definition.id]
        resolved = execution.registry.driver(definition.id).resolve(
            execution.context,
            definition,
            desired,
        )
        spec = execution.service_spec_factory(definition.id, resolved)
        if spec is None:
            raise UnsupportedTask(
                f"{definition.label} has no managed runtime specification."
            )
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
                previous_spec.model_dump(mode="json")
                if previous_spec is not None
                else None
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
            execution.supervisor.replace_spec(
                ManagedProcessSpec.model_validate(previous)
            )
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
                crispasr.quantization
                or crispasr.options.get("quantization")
                or ""
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
            selected_ids = {
                specification.service_id for specification in specifications
            }
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
                service.health.model_dump(mode="json")
                if service.health is not None
                else None
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
        snapshots = {
            service.id: service
            for service in execution.supervisor.snapshot()
        }
        previous = snapshots.get(definition.service_key)
        was_running = bool(previous is not None and previous.process is not None)
        desired_running = bool(
            previous is not None and previous.desired_running
        )
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
        owned = [
            Path(record["path"])
            for record in ownership_records
        ]
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
            for item in legacy_data_inventory(
                execution.context.layout
            ).items
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
                execution.context.layout.contains(parent, candidate)
                and parent != candidate
                for parent in canonical_owned[:index]
            )
        ]
        backup_root = (
            execution.context.layout.backups
            / execution.operation.id
            / definition.id
        )
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
                moved.append(
                    {"source": str(source), "backup": str(destination)}
                )
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
                    execution.context.layout.contains(parent, candidate)
                    and parent != candidate
                    for parent in canonical_owned[:index]
                )
            ]
            backup_root = (
                execution.context.layout.backups
                / execution.operation.id
                / definition.id
            )
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
        operation_staging = (
            execution.context.layout.staging / execution.operation.id
        )
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
